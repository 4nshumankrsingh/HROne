"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience; real env vars win)

Everything lives in this one file, in labelled sections:
  1. Config & database
  2. Constants & type aliases
  3. Time, rounding and shift helpers (pure functions, unit-tested in dev/)
  4. Serialisers (Mongo document -> API JSON)
  5. Startup (index creation)
  6. Models and endpoints (added phase by phase)
"""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import AfterValidator, Field, StringConstraints
from pymongo import ASCENDING, MongoClient

# --------------------------------------------------------------------------- #
# 1. Config & database
# --------------------------------------------------------------------------- #
load_dotenv()  # does not override variables already set in the environment

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones.
client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]
employees_col = db["employees"]
logs_col = db["attendance_logs"]

# --------------------------------------------------------------------------- #
# 2. Constants & type aliases
# --------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

GRACE_SECONDS = 10 * 60            # R2: late only if strictly more than this
OVERTIME_MIN_MINUTES = 30          # R3: overtime below this counts as 0
HALF_DAY_THRESHOLD = Decimal("4.50")  # R5
MAX_SHIFT_SECONDS = 24 * 3600      # punch-out / correction window

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")   # R6
ALL_STATUSES = PRESENCE_STATUSES + ("ABSENT", "LEAVE")


def _check_real_date(value: str) -> str:
    """'2026-02-30' matches the pattern but is not a real date -> reject."""
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError("not a valid calendar date")
    return value


# Reusable validated types for request bodies and query parameters.
# Epoch milliseconds: strict int (rejects floats/strings/bools), seconds-looking values rejected by `ge`.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
DateStr = Annotated[
    str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$"), AfterValidator(_check_real_date)
]
MonthStr = Annotated[str, StringConstraints(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
HHMM = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


# --------------------------------------------------------------------------- #
# 3. Time, rounding and shift helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC-aware datetime."""
    return EPOCH + timedelta(milliseconds=ms)


def dt_to_ms(dt: datetime) -> int:
    """Datetime -> epoch milliseconds, using integer maths (no float rounding)."""
    if dt.tzinfo is None:            # defensive: treat naive values as UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def truncate_seconds(dt: datetime) -> datetime:
    """R1: drop sub-second precision before any calculation."""
    return dt.replace(microsecond=0)


def now_utc() -> datetime:
    return truncate_seconds(datetime.now(UTC))


def round_half_up(value, places: int = 2) -> float:
    """R4/R8: half-up rounding. Python's round() and Mongo's $round are banker's."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def parse_hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    """R1: shift_end <= shift_start means the shift ends on the next day."""
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date a punch-in belongs to."""
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end):
        eh, em = parse_hhmm(shift_end)
        if (ist.hour, ist.minute, ist.second) < (eh, em, 0):
            day -= timedelta(days=1)      # early-morning punch belongs to yesterday's shift
    return day.isoformat()


def _ist_instant(date_str: str, hhmm: str, add_days: int = 0) -> datetime:
    h, m = parse_hhmm(hhmm)
    d = date.fromisoformat(date_str) + timedelta(days=add_days)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def shift_start_instant(date_str: str, shift_start: str) -> datetime:
    return _ist_instant(date_str, shift_start)


def shift_end_instant(date_str: str, shift_start: str, shift_end: str) -> datetime:
    return _ist_instant(date_str, shift_end, 1 if is_overnight(shift_start, shift_end) else 0)


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor(minutes since start)."""
    delta = int((truncate_seconds(punch_in) - shift_start_instant(date_str, shift_start)).total_seconds())
    return delta // 60 if delta > GRACE_SECONDS else 0


def compute_overtime_minutes(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: floor(minutes after shift end), counted only if >= 30."""
    delta = int((truncate_seconds(punch_out) - shift_end_instant(date_str, shift_start, shift_end)).total_seconds())
    minutes = delta // 60 if delta > 0 else 0
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, rounded to 2 decimals half-up."""
    secs = int((truncate_seconds(punch_out) - truncate_seconds(punch_in)).total_seconds())
    return round_half_up(Decimal(secs) / Decimal(3600), 2)


def is_half_day(work_hours: float) -> bool:
    """R5: the *rounded* work hours are below 4.50."""
    return Decimal(str(work_hours)) < HALF_DAY_THRESHOLD


def derive_fields(
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    date_str: str,
    shift_start: str,
    shift_end: str,
) -> dict[str, Any]:
    """All four derived fields for a record, following the table in DATA_MODEL.md."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE_STATUSES or punch_in is None:
        return out
    out["late_minutes"] = compute_late_minutes(punch_in, date_str, shift_start)
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime_minutes(punch_out, date_str, shift_start, shift_end)
        out["half_day"] = is_half_day(wh)
    return out


# --------------------------------------------------------------------------- #
# 4. Serialisers: MongoDB document -> API JSON
# --------------------------------------------------------------------------- #
def _ms_or_none(dt: Optional[datetime]) -> Optional[int]:
    return dt_to_ms(dt) if dt is not None else None


def history_to_api(entries: Optional[list]) -> list[dict]:
    out = []
    for e in entries or []:
        changes = {}
        for field, ch in (e.get("changes") or {}).items():
            if field in ("punch_in", "punch_out"):
                ch = {"from": _ms_or_none(ch.get("from")), "to": _ms_or_none(ch.get("to"))}
            changes[field] = ch
        out.append({"at": dt_to_ms(e["at"]), "by": e.get("by"), "reason": e.get("reason"), "changes": changes})
    return out


def record_to_api(doc: dict) -> dict:
    """Attendance document -> API record. Tolerates legacy docs (missing fields) and never leaks _id."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _ms_or_none(doc.get("punch_in")),
        "punch_out": _ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history_to_api(doc.get("history")),
    }


def employee_to_api(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


# --------------------------------------------------------------------------- #
# 5. Startup: indexes (idempotent: create_index is a no-op if it already exists)
# --------------------------------------------------------------------------- #
def create_indexes() -> None:
    # emp_code is the business key: uniqueness is enforced by the database, not by check-then-insert.
    employees_col.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    # One record per (emp_code, date): the guard that makes concurrent punch-ins race-safe.
    logs_col.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date")
    # More indexes are added in later phases, driven by explain() output.


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

# --------------------------------------------------------------------------- #
# 6. Models and endpoints  (added in the next phases)
# --------------------------------------------------------------------------- #
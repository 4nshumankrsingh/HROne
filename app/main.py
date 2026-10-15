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
from typing import Annotated, Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Config & database
# --------------------------------------------------------------------------- #
load_dotenv()  # does not override variables already set in the environment

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=10_000)
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
    str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(_check_real_date)
]
MonthStr = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
HHMM = Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]


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
    # GET /employees: department filter + emp_code sort.
    employees_col.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    # punch-out: latest record of an employee with punch_in <= t.
    logs_col.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")
    # GET /attendance: sort (date desc, emp_code asc), with or without a status filter.
    logs_col.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
    logs_col.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp"
    )
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

class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, StringConstraints(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    email: Annotated[str, StringConstraints(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


# --- system ----------------------------------------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# --- employees -------------------------------------------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees_col.insert_one(doc)          # uniqueness enforced by the unique index
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_to_api(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = employees_col.count_documents(q)
    skip = (page - 1) * page_size
    items: list[dict] = []
    if skip < total:
        cur = employees_col.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
        items = [employee_to_api(d) for d in cur]
    return {"items": items, "total": total, "page": page, "page_size": page_size}

# --- attendance: punch-in --------------------------------------------------
class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = employees_col.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = truncate_seconds(ms_to_dt(body.punched_at)) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    derived = derive_fields(body.status, ts, None, day, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derived,
        "history": [],
    }
    try:
        logs_col.insert_one(doc)               # unique (emp_code, date) index decides the race
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_to_api(doc)

# --- attendance: punch-out -------------------------------------------------
class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = employees_col.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = truncate_seconds(ms_to_dt(body.punched_at)) if body.punched_at is not None else now_utc()

    # Most recent record whose punch_in is at or before punched_at (works across midnight).
    rec = logs_col.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = rec["punch_in"]
    if ts <= pin or (ts - pin).total_seconds() > MAX_SHIFT_SECONDS:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")

    derived = derive_fields(rec["status"], pin, ts, rec["date"], emp["shift_start"], emp["shift_end"])
    # Compare-and-set: only close the record if it is still open and unchanged.
    res = logs_col.update_one(
        {"_id": rec["_id"], "punch_out": None, "punch_in": pin},
        {"$set": {"punch_out": ts, **derived}},
    )
    if res.matched_count == 0:
        raise HTTPException(409, "already punched out")
    rec.update({"punch_out": ts, **derived})
    return record_to_api(rec)

# --- attendance: list ------------------------------------------------------
StatusLit = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def build_attendance_filter(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]
) -> dict[str, Any]:
    q: dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        rng: dict[str, str] = {}
        if date_from is not None:
            rng["$gte"] = date_from
        if date_to is not None:
            rng["$lte"] = date_to
        q["date"] = rng
    if status is not None:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[StatusLit] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q = build_attendance_filter(emp_code, date_from, date_to, status)
    total = logs_col.count_documents(q)
    skip = (page - 1) * page_size
    items: list[dict] = []
    if skip < total:
        cur = logs_col.find(q).sort(ATTENDANCE_SORT).skip(skip).limit(page_size)
        items = [record_to_api(d) for d in cur]
    return {"items": items, "total": total, "page": page, "page_size": page_size}
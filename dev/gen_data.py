"""Test-data generator (local only; not part of the submission API).

    python dev/gen_data.py tiny  [--db attendance_gen]     # hand-checkable scenario (see test_oracle.py)
    python dev/gen_data.py big   [--db attendance_gen] [--employees 1300] [--seed 7]   # ~100k records

Writes straight into MongoDB (like the grader does) and creates no indexes: start the app against
the database afterwards and it creates them at startup. Refuses to touch a DB not named *_test / *_gen.
"""
import argparse
import pathlib
import random
import sys
from datetime import date, datetime, timedelta, timezone

from pymongo import MongoClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))   # so `python dev/gen_data.py` finds app/
from app.main import MONGO_URI, derive_fields, shift_end_instant, shift_start_instant

UTC = timezone.utc
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
CREATED = datetime(2026, 1, 5, 4, 30, tzinfo=UTC)


def _emp(code, name, dept, joined, ss="09:30", se="18:30"):
    return {"emp_code": code, "name": name, "email": f"{name.split()[0].lower()}@example.com",
            "department": dept, "shift_start": ss, "shift_end": se, "joined_on": joined, "created_at": CREATED}


def _log(code, day, status, late=0, wh=None, ot=0, half=False, ss="09:30"):
    """A record with the given *stored* derived values (analytics read stored values)."""
    doc = {"emp_code": code, "date": day, "status": status, "punch_in": None, "punch_out": None,
           "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
    if status in PRESENCE:
        pin = (shift_start_instant(day, ss) + timedelta(minutes=late)).astimezone(UTC)
        doc.update(punch_in=pin, late_minutes=late, overtime_minutes=ot, half_day=half)
        if wh is not None:
            doc.update(punch_out=pin + timedelta(seconds=round(wh * 3600)), work_hours=wh)
    return doc


def tiny_dataset():
    """July 2026 (1st is a Wednesday; 23 working days). Covers: zero-log employee, mid-month joiner,
    employee joining after month end, department whose only member joins later, weekend record,
    half day, LEAVE, ABSENT, ON_DUTY, open record, tied late totals, orphan log."""
    emps = [
        _emp("EMP0001", "Asha Rao", "Engineering", "2026-01-05"),
        _emp("EMP0002", "Ben Joiner", "Engineering", "2026-07-15"),
        _emp("EMP0003", "Cara NoLogs", "Engineering", "2026-01-05"),
        _emp("EMP0004", "Dev Future", "Engineering", "2026-08-10"),
        _emp("EMP0005", "Esha Sales", "Sales", "2026-01-05"),
        _emp("EMP0006", "Farid Sales", "Sales", "2026-01-05"),
        _emp("EMP0007", "Gita Sales", "Sales", "2026-01-05"),
        _emp("EMP0008", "Hari Sales", "Sales", "2026-01-05"),
        _emp("EMP0009", "Ira Ops", "Ops", "2026-08-01"),
    ]
    L = _log
    logs = [
        L("EMP0001", "2026-07-01", "PRESENT", 0, 9.0),
        L("EMP0001", "2026-07-02", "PRESENT", 15, 8.5, ot=30),
        L("EMP0001", "2026-07-03", "WFH", 0, 4.0, half=True),
        L("EMP0001", "2026-07-04", "PRESENT", 20, 3.0, half=True),          # Saturday
        L("EMP0001", "2026-07-06", "LEAVE"),
        L("EMP0001", "2026-07-07", "ABSENT"),
        L("EMP0001", "2026-07-08", "ON_DUTY", 0, 8.0, ot=45),
        L("EMP0001", "2026-07-09", "PRESENT", 0, None),                      # open record
        L("EMP0002", "2026-07-15", "PRESENT", 12, 9.0),
        L("EMP0002", "2026-07-16", "PRESENT", 0, 9.0),
        L("EMP0005", "2026-07-01", "PRESENT", 40, 8.0), L("EMP0005", "2026-07-02", "PRESENT", 20, 8.0),
        L("EMP0006", "2026-07-01", "PRESENT", 30, 8.0), L("EMP0006", "2026-07-02", "PRESENT", 30, 8.0),
        L("EMP0007", "2026-07-01", "PRESENT", 45, 8.0),
        L("EMP0008", "2026-07-01", "PRESENT", 12, 8.0),
        L("EMP9999", "2026-07-01", "PRESENT", 500, 8.0),                     # orphan: no such employee
    ]
    return emps, logs


def big_dataset(n_emp=1300, seed=7):
    rng = random.Random(seed)
    depts = ["Engineering", "Sales", "Support", "HR", "Finance", "Ops", "Marketing", "Legal"]
    emps = []
    for i in range(1, n_emp + 1):
        r = rng.random()
        ss, se = ("09:30", "18:30") if r < 0.88 else (("10:00", "19:00") if r < 0.94 else ("22:00", "06:00"))
        joined = (date(2026, 1, 1) + timedelta(days=rng.randint(0, 230))).isoformat()
        emps.append(_emp(f"EMP{i:04d}", f"Person{i} X", rng.choice(depts), joined, ss, se))
    days = [date(2026, 4, 1) + timedelta(d) for d in range(153)]              # Apr 1 .. Aug 31
    logs = []
    for e in emps:
        joined = date.fromisoformat(e["joined_on"])
        ss, se = e["shift_start"], e["shift_end"]
        for d in days:
            if d < joined:
                continue
            weekend = d.weekday() >= 5
            if weekend and rng.random() > 0.02:
                continue
            if not weekend and rng.random() < 0.03:                         # a weekday with no record
                continue
            ds = d.isoformat()
            status = rng.choices(["PRESENT", "WFH", "ON_DUTY", "LEAVE", "ABSENT"], [70, 12, 4, 8, 6])[0]
            if status not in PRESENCE:
                logs.append(_log(e["emp_code"], ds, status))
                continue
            pin = shift_start_instant(ds, ss) + timedelta(seconds=int(rng.gauss(120, 720)))
            if rng.random() < 0.04:
                pout = None                                                  # open record
            elif rng.random() < 0.06:
                pout = pin + timedelta(hours=rng.uniform(2, 4.4))            # half day
            else:
                pout = shift_end_instant(ds, ss, se) + timedelta(seconds=int(rng.gauss(600, 1500)))
                if pout <= pin:
                    pout = pin + timedelta(hours=1)
            pin, pout = pin.astimezone(UTC), (pout.astimezone(UTC) if pout else None)
            doc = {"emp_code": e["emp_code"], "date": ds, "status": status, "punch_in": pin, "punch_out": pout,
                   **derive_fields(status, pin, pout, ds, ss, se), "history": []}
            logs.append(doc)
    return emps, logs


def load(db_name, emps, logs):
    if not db_name.endswith(("_test", "_gen")):
        raise SystemExit("refusing to overwrite a database whose name does not end in _test or _gen")
    db = MongoClient(MONGO_URI, tz_aware=True)[db_name]
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    db.employees.insert_many(emps)
    for i in range(0, len(logs), 5000):
        db.attendance_logs.insert_many(logs[i:i + 5000])
    print(f"loaded {len(emps)} employees and {len(logs)} attendance records into '{db_name}'")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["tiny", "big"])
    ap.add_argument("--db", default="attendance_gen")
    ap.add_argument("--employees", type=int, default=1300)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    data = tiny_dataset() if a.kind == "tiny" else big_dataset(a.employees, a.seed)
    load(a.db, *data)
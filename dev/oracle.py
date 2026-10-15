"""Slow, obviously-correct reference implementations of the analytics rules (R5-R9).

Pure Python over plain lists of dicts shaped like the two collections. Used by the tests to
cross-check the MongoDB pipelines. Nothing here is imported by app/main.py.
"""
from calendar import monthrange
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}

# Trend: is the 7-day moving average taken over the *reported* (4-decimal) rates, or over the
# unrounded ones? The contract says "mean of the non-null attendance_rate values", so we use the
# reported ones. Flip this if the contract is clarified the other way.
MOVING_AVG_OF_ROUNDED_RATES = True


def q(value, places: int) -> float:
    """Half-up rounding of a Decimal/int/float to `places` decimals (R8)."""
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _bounds(month: str) -> tuple[date, date]:
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, 1), date(y, m, monthrange(y, m)[1])


def _weekday(d: date) -> bool:
    return d.weekday() < 5


def _weekdays_between(a: date, b: date) -> int:
    n, d = 0, a
    while d <= b:
        n += _weekday(d)
        d += timedelta(days=1)
    return n


def _weight(log: dict) -> Decimal:
    """Presence value of one record: 1, or 0.5 for a half day, or 0 if not a presence status."""
    if log["status"] not in PRESENCE:
        return Decimal(0)
    return Decimal("0.5") if log.get("half_day") else Decimal(1)


def _month_logs(logs: list, month: str, codes) -> list:
    return [l for l in logs if l["date"].startswith(month) and l["emp_code"] in codes]


def _present_days(recs: list) -> Decimal:
    """R5-R7: presence weight of the records that fall on Mon-Fri."""
    return sum((_weight(r) for r in recs if _weekday(date.fromisoformat(r["date"]))), Decimal(0))


def ref_employee_monthly(employees: list, logs: list, emp_code: str, month: str) -> Optional[dict]:
    emp = next((e for e in employees if e["emp_code"] == emp_code), None)
    if emp is None:
        return None
    first, last = _bounds(month)
    start = max(first, date.fromisoformat(emp["joined_on"]))
    working_days = _weekdays_between(start, last) if start <= last else 0
    recs = _month_logs(logs, month, {emp_code})
    present = _present_days(recs)
    late = [r for r in recs if (r.get("late_minutes") or 0) > 0]
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": q(present, 2),
        "leave_days": sum(1 for r in recs if r["status"] == "LEAVE"),
        "late_count": len(late),
        "total_late_minutes": sum(r["late_minutes"] for r in late),
        "total_overtime_minutes": sum((r.get("overtime_minutes") or 0) for r in recs),
        "attendance_pct": None if working_days == 0 else q(present * 100 / Decimal(working_days), 2),
    }


def ref_department_summary(employees: list, logs: list, month: str, department: Optional[str] = None) -> dict:
    _, last = _bounds(month)
    joined = [e for e in employees if date.fromisoformat(e["joined_on"]) <= last
              and (department is None or e["department"] == department)]
    items = []
    for dept in sorted({e["department"] for e in joined}):
        members = {e["emp_code"] for e in joined if e["department"] == dept}
        recs = _month_logs(logs, month, members)
        hours = [Decimal(str(r["work_hours"])) for r in recs if r["status"] in PRESENCE and r.get("work_hours") is not None]
        late = [r for r in recs if (r.get("late_minutes") or 0) > 0]
        items.append({
            "department": dept,
            "headcount": len(members),
            "present_days": q(_present_days(recs), 2),
            "avg_work_hours": q(sum(hours) / len(hours), 2) if hours else None,
            "late_count": len(late),
            "total_late_minutes": sum(r["late_minutes"] for r in late),
            "leave_count": sum(1 for r in recs if r["status"] == "LEAVE"),
            "on_duty_count": sum(1 for r in recs if r["status"] == "ON_DUTY"),
        })
    return {"month": month, "items": items}


def ref_late_leaderboard(employees: list, logs: list, month: str, limit: int = 10,
                         department: Optional[str] = None) -> dict:
    by_code = {e["emp_code"]: e for e in employees}
    totals: dict[str, list] = {}
    for r in logs:
        e = by_code.get(r["emp_code"])
        if e is None or not r["date"].startswith(month):
            continue
        if department is not None and e["department"] != department:
            continue
        late = r.get("late_minutes") or 0
        t = totals.setdefault(r["emp_code"], [0, 0])
        t[0] += late
        t[1] += 1 if late > 0 else 0
    ordered = sorted(((c, t) for c, t in totals.items() if t[0] > 0), key=lambda x: (-x[1][0], x[0]))
    rows, rank, prev = [], 0, None
    for i, (code, (total, count)) in enumerate(ordered, start=1):
        if total != prev:
            rank, prev = i, total          # standard competition ranking: 1, 2, 2, 4
        if rank <= limit:                   # limit applies after ranking
            e = by_code[code]
            rows.append({"rank": rank, "emp_code": code, "name": e["name"], "department": e["department"],
                         "total_late_minutes": total, "late_count": count})
    return {"month": month, "items": rows}


def ref_department_trend(employees: list, logs: list, department: str, d_from: str, d_to: str) -> Optional[dict]:
    dept = [e for e in employees if e["department"] == department]
    if not dept:
        return None
    codes = {e["emp_code"] for e in dept}
    day, end = date.fromisoformat(d_from), date.fromisoformat(d_to)
    rows, raw_rates = [], []
    while day <= end:
        iso = day.isoformat()
        headcount = sum(1 for e in dept if date.fromisoformat(e["joined_on"]) <= day)
        recs = [l for l in logs if l["date"] == iso and l["emp_code"] in codes]
        present = sum((_weight(r) for r in recs), Decimal(0))
        working = _weekday(day)
        raw = present / headcount if working and headcount > 0 else None
        raw_rates.append(raw)
        rows.append({
            "date": iso, "is_working_day": working, "headcount": headcount, "present_count": q(present, 2),
            "late_count": sum(1 for r in recs if (r.get("late_minutes") or 0) > 0),
            "attendance_rate": None if raw is None else q(raw, 4), "moving_avg_7d": None,
        })
        day += timedelta(days=1)
    for i, row in enumerate(rows):
        window = range(max(0, i - 6), i + 1)
        if MOVING_AVG_OF_ROUNDED_RATES:
            vals = [Decimal(str(rows[j]["attendance_rate"])) for j in window if rows[j]["attendance_rate"] is not None]
        else:
            vals = [raw_rates[j] for j in window if raw_rates[j] is not None]
        row["moving_avg_7d"] = q(sum(vals) / len(vals), 4) if vals else None
    return {"department": department, "items": rows}
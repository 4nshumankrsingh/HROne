from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from app.main import IST, UTC, dt_to_ms, logs_col


def ms(y, mo, d, h, mi, s=0, us=0, tz=IST):
    return dt_to_ms(datetime(y, mo, d, h, mi, s, us, tzinfo=tz))


def mk_emp(client, code="EMP0001", **over):
    body = dict(emp_code=code, name="Asha", email="a@b.co", department="Eng", joined_on="2026-01-05")
    assert client.post("/employees", json={**body, **over}).status_code == 201


def punch(client, code="EMP0001", **over):
    return client.post("/attendance/punch-in", json={"emp_code": code, **over})


def test_normal_punch_in(client):
    mk_emp(client)
    r = punch(client, punched_at=ms(2026, 7, 6, 9, 28))
    assert r.status_code == 201
    b = r.json()
    assert b == {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
                 "punch_in": ms(2026, 7, 6, 9, 28), "punch_out": None, "work_hours": None,
                 "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}


@pytest.mark.parametrize("h,m,s,late", [(9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45), (10, 5, 0, 35)])
def test_late_minutes(client, h, m, s, late):
    mk_emp(client)
    assert punch(client, punched_at=ms(2026, 7, 6, h, m, s)).json()["late_minutes"] == late


def test_subseconds_truncated_and_stored(client):
    mk_emp(client)
    b = punch(client, punched_at=ms(2026, 7, 6, 9, 40, 0, 900_000)).json()
    assert b["punch_in"] == ms(2026, 7, 6, 9, 40, 0) and b["late_minutes"] == 0
    stored = logs_col.find_one({"emp_code": "EMP0001"})
    assert stored["punch_in"].microsecond == 0


def test_date_is_ist_calendar_date(client):
    mk_emp(client)
    # 20:00 UTC on the 6th == 01:30 IST on the 7th
    assert punch(client, punched_at=ms(2026, 7, 6, 20, 0, tz=UTC)).json()["date"] == "2026-07-07"


def test_overnight_early_morning_punch_belongs_to_previous_day(client):
    mk_emp(client, shift_start="22:00", shift_end="06:00")
    b = punch(client, punched_at=ms(2026, 7, 7, 5, 30)).json()
    assert b["date"] == "2026-07-06" and b["late_minutes"] == 450


def test_overnight_punch_before_midnight(client):
    mk_emp(client, shift_start="22:00", shift_end="06:00")
    b = punch(client, punched_at=ms(2026, 7, 6, 21, 55)).json()
    assert b["date"] == "2026-07-06" and b["late_minutes"] == 0


@pytest.mark.parametrize("status", ["PRESENT", "WFH", "ON_DUTY"])
def test_allowed_statuses(client, status):
    mk_emp(client)
    assert punch(client, status=status, punched_at=ms(2026, 7, 6, 9, 0)).json()["status"] == status


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE", "present", "", None])
def test_disallowed_statuses_422(client, status):
    mk_emp(client)
    assert punch(client, status=status, punched_at=ms(2026, 7, 6, 9, 0)).status_code == 422


@pytest.mark.parametrize("bad", [1783312500, 1783312500.5, "1783312500000", True, 0, -5, 10 ** 20, 4_102_444_800_001])
def test_bad_punched_at_422(client, bad):
    mk_emp(client)
    assert punch(client, punched_at=bad).status_code == 422


def test_unknown_employee_404(client):
    assert punch(client, code="EMP9999").status_code == 404


def test_missing_emp_code_422(client):
    assert client.post("/attendance/punch-in", json={}).status_code == 422


def test_default_punched_at_is_now(client):
    mk_emp(client)
    b = punch(client).json()
    now_ms = dt_to_ms(datetime.now(UTC))
    assert abs(b["punch_in"] - now_ms) < 5000 and b["punch_in"] % 1000 == 0


def test_duplicate_same_date_409_even_with_different_time(client):
    mk_emp(client)
    assert punch(client, punched_at=ms(2026, 7, 6, 9, 0)).status_code == 201
    assert punch(client, punched_at=ms(2026, 7, 6, 11, 0)).status_code == 409


def test_different_dates_and_employees_ok(client):
    mk_emp(client)
    mk_emp(client, code="EMP0002")
    assert punch(client, punched_at=ms(2026, 7, 6, 9, 0)).status_code == 201
    assert punch(client, punched_at=ms(2026, 7, 7, 9, 0)).status_code == 201
    assert punch(client, code="EMP0002", punched_at=ms(2026, 7, 6, 9, 0)).status_code == 201


def test_client_cannot_set_derived_fields(client):
    mk_emp(client)
    b = punch(client, punched_at=ms(2026, 7, 6, 9, 0), late_minutes=99, work_hours=5, half_day=True).json()
    assert b["late_minutes"] == 0 and b["work_hours"] is None and b["half_day"] is False


def test_parallel_punch_ins_yield_one_201(client):
    mk_emp(client)
    t = ms(2026, 7, 6, 9, 0)
    with ThreadPoolExecutor(16) as ex:
        codes = list(ex.map(lambda _: punch(client, punched_at=t).status_code, range(16)))
    assert codes.count(201) == 1 and codes.count(409) == 15
    assert logs_col.count_documents({"emp_code": "EMP0001"}) == 1
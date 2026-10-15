from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from app.main import IST, dt_to_ms, logs_col


def ms(y, mo, d, h, mi, s=0):
    return dt_to_ms(datetime(y, mo, d, h, mi, s, tzinfo=IST))


def mk_emp(client, code="EMP0001", **over):
    body = dict(emp_code=code, name="Asha", email="a@b.co", department="Eng", joined_on="2026-01-05")
    assert client.post("/employees", json={**body, **over}).status_code == 201


def day(client, t_in, t_out=None, code="EMP0001", **over):
    assert client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": t_in, **over}).status_code == 201
    if t_out is not None:
        assert client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": t_out}).status_code == 200


def patch(client, date="2026-07-07", code="EMP0001", **body):
    body.setdefault("reason", "biometric glitch")
    body.setdefault("regularized_by", "hr.admin")
    return client.patch(f"/attendance/{code}/{date}", json=body)


@pytest.fixture()
def late_day(client):
    """Sample doc 8: punched in 10:05 (late 35), out 18:30."""
    mk_emp(client)
    day(client, ms(2026, 7, 7, 10, 5), ms(2026, 7, 7, 18, 30))
    return client


def test_correct_punch_in_recomputes_and_logs_history(late_day):
    r = patch(late_day, punch_in=ms(2026, 7, 7, 9, 28))
    assert r.status_code == 200
    b = r.json()
    assert b["punch_in"] == ms(2026, 7, 7, 9, 28) and b["late_minutes"] == 0 and b["work_hours"] == 9.03
    assert len(b["history"]) == 1
    h = b["history"][0]
    assert h["by"] == "hr.admin" and h["reason"] == "biometric glitch" and isinstance(h["at"], int)
    assert h["changes"] == {
        "punch_in": {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 8.42, "to": 9.03},
    }
    stored = logs_col.find_one({"emp_code": "EMP0001"})
    assert stored["late_minutes"] == 0 and stored["work_hours"] == 9.03 and len(stored["history"]) == 1


def test_history_is_appended_never_overwritten(late_day):
    patch(late_day, punch_in=ms(2026, 7, 7, 9, 28), reason="first fix")
    b = patch(late_day, punch_out=ms(2026, 7, 7, 19, 15), reason="second fix", regularized_by="boss").json()
    assert [h["reason"] for h in b["history"]] == ["first fix", "second fix"]
    assert b["history"][1]["by"] == "boss"
    assert b["overtime_minutes"] == 45 and b["work_hours"] == 9.78


def test_changes_contain_only_fields_that_changed(late_day):
    b = patch(late_day, punch_out=ms(2026, 7, 7, 18, 40)).json()     # overtime stays 0
    assert set(b["history"][0]["changes"]) == {"punch_out", "work_hours"}


def test_absent_clears_times_and_resets_derived(late_day):
    b = patch(late_day, status="ABSENT").json()
    assert (b["status"], b["punch_in"], b["punch_out"], b["work_hours"], b["late_minutes"],
            b["overtime_minutes"], b["half_day"]) == ("ABSENT", None, None, None, 0, 0, False)
    assert set(b["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes"}


def test_leave_with_punch_time_422(late_day):
    assert patch(late_day, status="LEAVE", punch_in=ms(2026, 7, 7, 9, 0)).status_code == 422
    assert patch(late_day, status="ABSENT", punch_out=ms(2026, 7, 7, 18, 0)).status_code == 422


def test_absent_record_cannot_get_times_without_presence_status(client):
    mk_emp(client)
    day(client, ms(2026, 7, 7, 9, 0))
    patch(client, status="ABSENT")
    assert patch(client, punch_in=ms(2026, 7, 7, 9, 0)).status_code == 422


def test_absent_to_present_requires_punch_in(client):
    mk_emp(client)
    day(client, ms(2026, 7, 7, 9, 0))
    patch(client, status="ABSENT")
    assert patch(client, status="PRESENT").status_code == 422
    b = patch(client, status="WFH", punch_in=ms(2026, 7, 7, 9, 45)).json()
    assert b["status"] == "WFH" and b["late_minutes"] == 15 and b["punch_out"] is None


def test_status_change_only(late_day):
    b = patch(late_day, status="WFH").json()
    assert b["status"] == "WFH" and set(b["history"][0]["changes"]) == {"status"}


def test_punch_in_must_stay_on_attendance_date(late_day):
    assert patch(late_day, punch_in=ms(2026, 7, 6, 9, 0)).status_code == 422
    assert patch(late_day, punch_in=ms(2026, 7, 8, 9, 0)).status_code == 422


def test_punch_out_rules(late_day):
    assert patch(late_day, punch_out=ms(2026, 7, 7, 10, 5)).status_code == 422      # == punch_in
    assert patch(late_day, punch_out=ms(2026, 7, 7, 9, 0)).status_code == 422       # before
    assert patch(late_day, punch_out=ms(2026, 7, 8, 10, 5, 1)).status_code == 422   # > 24h
    assert patch(late_day, punch_in=ms(2026, 7, 7, 19, 0)).status_code == 422       # now after stored out


def test_punch_out_on_open_record_sets_derived(client):
    mk_emp(client)
    day(client, ms(2026, 7, 7, 9, 30))
    b = patch(client, punch_out=ms(2026, 7, 7, 12, 30)).json()
    assert b["work_hours"] == 3.0 and b["half_day"] is True and b["overtime_minutes"] == 0


def test_no_change_422(late_day):
    assert patch(late_day, punch_in=ms(2026, 7, 7, 10, 5)).status_code == 422
    assert patch(late_day, status="PRESENT").status_code == 422
    assert patch(late_day).status_code == 422
    assert logs_col.find_one({"emp_code": "EMP0001"})["history"] == []


def test_404s(late_day):
    assert patch(late_day, code="EMP9999", status="WFH").status_code == 404
    assert patch(late_day, date="2026-07-09", status="WFH").status_code == 404


@pytest.mark.parametrize("body", [
    {"reason": "abc"}, {"reason": "x" * 201}, {"regularized_by": ""}, {"regularized_by": "x" * 51},
    {"punch_in": None}, {"status": None}, {"punch_in": 1783312500}, {"status": "BOGUS"},
])
def test_invalid_body_422(late_day, body):
    assert patch(late_day, **{"status": "WFH", **body}).status_code == 422


def test_missing_required_fields_422(late_day):
    assert late_day.patch("/attendance/EMP0001/2026-07-07", json={"status": "WFH"}).status_code == 422


def test_bad_date_in_path_422(late_day):
    assert patch(late_day, date="2026-02-30", status="WFH").status_code == 422
    assert patch(late_day, date="07-07-2026", status="WFH").status_code == 422


def test_client_cannot_set_derived_fields(late_day):
    b = patch(late_day, status="WFH", late_minutes=99, work_hours=1.0, half_day=True).json()
    assert b["late_minutes"] == 35 and b["work_hours"] == 8.42 and b["half_day"] is False


def test_subseconds_truncated(late_day):
    b = patch(late_day, punch_in=ms(2026, 7, 7, 9, 28) + 750).json()
    assert b["punch_in"] == ms(2026, 7, 7, 9, 28)


def test_legacy_doc_without_history_or_half_day(client):
    mk_emp(client)
    logs_col.insert_one({"emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT",
                         "punch_in": datetime.fromisoformat("2026-07-14T04:02:00+00:00"),
                         "punch_out": datetime.fromisoformat("2026-07-14T13:01:00+00:00"),
                         "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    b = patch(client, date="2026-07-14", status="WFH").json()
    assert len(b["history"]) == 1 and b["history"][0]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}
    assert len(logs_col.find_one({"emp_code": "EMP0001"})["history"]) == 1


def test_overnight_employee_regularize(client):
    mk_emp(client, shift_start="22:00", shift_end="06:00")
    day(client, ms(2026, 7, 6, 22, 40), ms(2026, 7, 7, 6, 40))
    b = patch(client, date="2026-07-06", punch_in=ms(2026, 7, 6, 21, 55)).json()
    assert b["late_minutes"] == 0 and b["work_hours"] == 8.75 and b["overtime_minutes"] == 40
    # an early-morning punch_in on the 7th still belongs to the 6th's shift
    assert patch(client, date="2026-07-06", punch_in=ms(2026, 7, 7, 5, 0), punch_out=ms(2026, 7, 7, 6, 10)).status_code == 200


def test_concurrent_patches_never_lose_history(late_day):
    times = [ms(2026, 7, 7, 9, m) for m in range(0, 16)]
    with ThreadPoolExecutor(16) as ex:
        codes = list(ex.map(lambda t: patch(late_day, punch_in=t, reason=f"fix {t}").status_code, times))
    ok = codes.count(200)
    assert ok >= 1 and ok + codes.count(409) == 16
    stored = logs_col.find_one({"emp_code": "EMP0001"})
    assert len(stored["history"]) == ok
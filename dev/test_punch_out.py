from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from app.main import IST, UTC, dt_to_ms, logs_col


def ms(y, mo, d, h, mi, s=0, tz=IST):
    return dt_to_ms(datetime(y, mo, d, h, mi, s, tzinfo=tz))


def mk_emp(client, code="EMP0001", **over):
    body = dict(emp_code=code, name="Asha", email="a@b.co", department="Eng", joined_on="2026-01-05")
    assert client.post("/employees", json={**body, **over}).status_code == 201


def pin(client, t, code="EMP0001", **over):
    r = client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": t, **over})
    assert r.status_code == 201, r.text
    return r.json()


def pout(client, t=None, code="EMP0001"):
    body = {"emp_code": code}
    if t is not None:
        body["punched_at"] = t
    return client.post("/attendance/punch-out", json=body)


def test_normal_day_sample_doc_1(client):
    mk_emp(client)
    pin(client, ms(2026, 7, 6, 9, 28))
    r = pout(client, ms(2026, 7, 6, 18, 35))
    assert r.status_code == 200
    b = r.json()
    assert b["work_hours"] == 9.12 and b["overtime_minutes"] == 0 and b["late_minutes"] == 0
    assert b["half_day"] is False and b["punch_out"] == ms(2026, 7, 6, 18, 35) and b["history"] == []


def test_late_with_overtime_sample_doc_2(client):
    mk_emp(client)
    pin(client, ms(2026, 7, 6, 10, 5))
    b = pout(client, ms(2026, 7, 6, 19, 10)).json()
    assert (b["work_hours"], b["late_minutes"], b["overtime_minutes"], b["half_day"]) == (9.08, 35, 40, False)


def test_half_day_wfh(client):
    mk_emp(client)
    pin(client, ms(2026, 7, 7, 9, 30), status="WFH")
    b = pout(client, ms(2026, 7, 7, 13, 0)).json()
    assert b["work_hours"] == 3.5 and b["half_day"] is True and b["status"] == "WFH"


def test_half_day_boundary_uses_rounded_hours(client):
    mk_emp(client)
    pin(client, ms(2026, 7, 7, 9, 0))
    b = pout(client, ms(2026, 7, 7, 13, 29, 42)).json()       # 16182 s = 4.495 h -> 4.50
    assert b["work_hours"] == 4.5 and b["half_day"] is False


def test_overnight_shift_across_midnight_sample_doc_7(client):
    mk_emp(client, shift_start="22:00", shift_end="06:00")
    pin(client, ms(2026, 7, 6, 21, 55))
    b = pout(client, ms(2026, 7, 7, 6, 40)).json()
    assert b["date"] == "2026-07-06" and b["work_hours"] == 8.75 and b["overtime_minutes"] == 40


def test_subseconds_truncated(client):
    mk_emp(client)
    pin(client, ms(2026, 7, 6, 9, 0))
    b = pout(client, ms(2026, 7, 6, 18, 0) + 900).json()
    assert b["punch_out"] == ms(2026, 7, 6, 18, 0)


def test_default_punched_at_is_now(client):
    mk_emp(client)
    pin(client, dt_to_ms(datetime.now(UTC) - timedelta(hours=2)))
    b = pout(client).json()
    assert 1.99 <= b["work_hours"] <= 2.1


def test_unknown_employee_404(client):
    assert pout(client, ms(2026, 7, 6, 18, 0), code="EMP9999").status_code == 404


def test_no_punch_in_404(client):
    mk_emp(client)
    assert pout(client, ms(2026, 7, 6, 18, 0)).status_code == 404


def test_punch_out_before_any_punch_in_404(client):
    mk_emp(client)
    pin(client, ms(2026, 7, 6, 9, 0))
    assert pout(client, ms(2026, 7, 6, 8, 0)).status_code == 404


def test_punch_out_equal_to_punch_in_422(client):
    mk_emp(client)
    pin(client, ms(2026, 7, 6, 9, 0))
    assert pout(client, ms(2026, 7, 6, 9, 0)).status_code == 422


def test_more_than_24h_422_but_exactly_24h_ok(client):
    mk_emp(client)
    pin(client, ms(2026, 7, 6, 9, 0))
    assert pout(client, ms(2026, 7, 7, 9, 0, 1)).status_code == 422
    assert pout(client, ms(2026, 7, 7, 9, 0)).status_code == 200


def test_already_punched_out_409(client):
    mk_emp(client)
    pin(client, ms(2026, 7, 6, 9, 0))
    assert pout(client, ms(2026, 7, 6, 18, 0)).status_code == 200
    assert pout(client, ms(2026, 7, 6, 19, 0)).status_code == 409


def test_bad_body_422(client):
    mk_emp(client)
    for bad in (1783312500, 1.5, "x", True):
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": bad})
        assert r.status_code == 422


def test_picks_most_recent_record_at_or_before_time(client):
    mk_emp(client)
    pin(client, ms(2026, 7, 6, 9, 0))
    pin(client, ms(2026, 7, 7, 9, 0))
    # evening of the 6th closes the 6th's record, not the 7th's
    b = pout(client, ms(2026, 7, 6, 18, 0)).json()
    assert b["date"] == "2026-07-06"
    assert logs_col.find_one({"date": "2026-07-07"})["punch_out"] is None
    # 7th evening closes the 7th
    assert pout(client, ms(2026, 7, 7, 18, 0)).json()["date"] == "2026-07-07"
    # most recent is closed -> 409
    assert pout(client, ms(2026, 7, 7, 19, 0)).status_code == 409


def test_parallel_punch_outs_yield_one_200(client):
    mk_emp(client)
    pin(client, ms(2026, 7, 6, 9, 0))
    t = ms(2026, 7, 6, 18, 0)
    with ThreadPoolExecutor(16) as ex:
        codes = list(ex.map(lambda _: pout(client, t).status_code, range(16)))
    assert codes.count(200) == 1 and codes.count(409) == 15
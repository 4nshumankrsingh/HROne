from datetime import datetime

import pytest

from app.main import UTC, dt_to_ms, logs_col


def rec(emp, date, status="PRESENT", late=0, **extra):
    d = dict(emp_code=emp, date=date, status=status, punch_in=None, punch_out=None, work_hours=None,
             late_minutes=late, overtime_minutes=0, half_day=False, history=[])
    if status in ("PRESENT", "WFH", "ON_DUTY"):
        d["punch_in"] = datetime.fromisoformat(f"{date}T04:00:00").replace(tzinfo=UTC)
    d.update(extra)
    return d


@pytest.fixture()
def data(client):
    logs_col.insert_many([
        rec("EMP0002", "2026-07-06"), rec("EMP0001", "2026-07-06", late=15),
        rec("EMP0001", "2026-07-07", "WFH"), rec("EMP0002", "2026-07-07", "ABSENT"),
        rec("EMP0003", "2026-07-08", "LEAVE"), rec("EMP0001", "2026-07-08"),
        rec("EMP0003", "2026-07-09", "ON_DUTY"),
    ])
    return client


def get(c, **params):
    r = c.get("/attendance", params=params)
    return r


def test_sorted_date_desc_then_emp_code_asc(data):
    b = get(data).json()
    got = [(i["date"], i["emp_code"]) for i in b["items"]]
    assert got == [("2026-07-09", "EMP0003"), ("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0003"),
                   ("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002"),
                   ("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0002")]
    assert b["total"] == 7 and b["page"] == 1 and b["page_size"] == 20


def test_filter_emp_code(data):
    b = get(data, emp_code="EMP0001").json()
    assert b["total"] == 3 and [i["date"] for i in b["items"]] == ["2026-07-08", "2026-07-07", "2026-07-06"]


def test_date_range_inclusive(data):
    b = get(data, date_from="2026-07-07", date_to="2026-07-08").json()
    assert b["total"] == 4 and {i["date"] for i in b["items"]} == {"2026-07-07", "2026-07-08"}
    assert get(data, date_from="2026-07-09").json()["total"] == 1
    assert get(data, date_to="2026-07-06").json()["total"] == 2


def test_status_filter_and_combination(data):
    assert get(data, status="LEAVE").json()["total"] == 1
    b = get(data, emp_code="EMP0001", status="WFH", date_from="2026-07-01", date_to="2026-07-31").json()
    assert b["total"] == 1 and b["items"][0]["date"] == "2026-07-07"


def test_pagination_and_total_reflects_filters(data):
    b = get(data, page=2, page_size=3).json()
    assert b["total"] == 7 and len(b["items"]) == 3 and b["page"] == 2 and b["page_size"] == 3
    b = get(data, page=3, page_size=3).json()
    assert len(b["items"]) == 1
    b = get(data, page=99999999999, emp_code="EMP0001").json()
    assert b["items"] == [] and b["total"] == 3


def test_response_shape_no_id_and_ms(data):
    i = get(data, emp_code="EMP0001", date_from="2026-07-06", date_to="2026-07-06").json()["items"][0]
    assert set(i) == {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes",
                      "overtime_minutes", "half_day", "history"}
    assert i["punch_in"] == dt_to_ms(datetime(2026, 7, 6, 4, 0, tzinfo=UTC)) and i["late_minutes"] == 15


def test_legacy_doc_defaults(client):
    logs_col.insert_one({"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
                         "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=UTC),
                         "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=UTC),
                         "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    i = get(client).json()["items"][0]
    assert i["history"] == [] and i["half_day"] is False


@pytest.mark.parametrize("params", [
    {"date_from": "2026-07-09", "date_to": "2026-07-01"}, {"date_from": "2026-02-30"}, {"date_to": "07-01-2026"},
    {"status": "BOGUS"}, {"status": "present"}, {"page": 0}, {"page_size": 0}, {"page_size": 101},
    {"page_size": -5}, {"page": "x"},
])
def test_422(data, params):
    assert get(data, **params).status_code == 422


def test_empty_collection(client):
    b = get(client).json()
    assert b == {"items": [], "total": 0, "page": 1, "page_size": 20}
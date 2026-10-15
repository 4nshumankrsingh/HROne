from concurrent.futures import ThreadPoolExecutor

import pytest

EMP = dict(emp_code="EMP0001", name="Asha Rao", email="asha@example.com", department="Engineering",
           joined_on="2026-01-05")


def mk(client, **over):
    return client.post("/employees", json={**EMP, **over})


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_create_ok_shape_and_defaults(client):
    r = mk(client)
    assert r.status_code == 201
    b = r.json()
    assert set(b) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
    assert b["shift_start"] == "09:30" and b["shift_end"] == "18:30"
    assert isinstance(b["created_at"], int) and b["created_at"] > 100_000_000_000
    assert "_id" not in b and "id" not in b


def test_create_ignores_client_created_at(client):
    r = mk(client, created_at=1)
    assert r.status_code == 201 and r.json()["created_at"] != 1


def test_duplicate_409(client):
    assert mk(client).status_code == 201
    assert mk(client, name="Other").status_code == 409


@pytest.mark.parametrize("over", [
    {"emp_code": "emp0001"}, {"emp_code": "EMP12"}, {"emp_code": "EMP1234567"}, {"email": "nope"},
    {"email": "a@b"}, {"name": ""}, {"department": ""}, {"joined_on": "2026-02-30"}, {"joined_on": "05-01-2026"},
    {"shift_start": "9:30"}, {"shift_start": "24:00"}, {"shift_end": "09:30"}, {"name": "x" * 101},
])
def test_create_422(client, over):
    assert mk(client, **over).status_code == 422


def test_create_missing_field_422(client):
    body = dict(EMP)
    body.pop("joined_on")
    assert client.post("/employees", json=body).status_code == 422


def test_overnight_shift_accepted(client):
    assert mk(client, shift_start="22:00", shift_end="06:00").status_code == 201


def test_parallel_duplicate_creates_yield_one_201(client):
    with ThreadPoolExecutor(16) as ex:
        codes = list(ex.map(lambda _: mk(client).status_code, range(16)))
    assert codes.count(201) == 1 and codes.count(409) == 15


def seed(client, n=25):
    for i in range(1, n + 1):
        assert mk(client, emp_code=f"EMP{i:04d}", department="A" if i <= 20 else "B").status_code == 201


def test_list_sorted_paginated_and_total(client):
    seed(client)
    r = client.get("/employees").json()
    assert r["total"] == 25 and r["page"] == 1 and r["page_size"] == 20
    assert [e["emp_code"] for e in r["items"]] == [f"EMP{i:04d}" for i in range(1, 21)]
    r2 = client.get("/employees?page=2").json()
    assert [e["emp_code"] for e in r2["items"]] == [f"EMP{i:04d}" for i in range(21, 26)]


def test_list_total_reflects_filter(client):
    seed(client)
    r = client.get("/employees?department=B").json()
    assert r["total"] == 5 and len(r["items"]) == 5
    assert client.get("/employees?department=b").json()["total"] == 0       # case-sensitive


def test_list_page_beyond_end_is_empty(client):
    seed(client, 3)
    r = client.get("/employees?page=99999999999999999999").json()
    assert r["items"] == [] and r["total"] == 3


@pytest.mark.parametrize("qs", ["page=0", "page=-1", "page_size=0", "page_size=101", "page_size=-5", "page=abc"])
def test_list_pagination_422(client, qs):
    assert client.get(f"/employees?{qs}").status_code == 422
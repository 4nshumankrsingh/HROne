"""Test setup: point the app at a throw-away *_test database BEFORE app.main is imported."""
import os

os.environ["MONGO_DB"] = os.getenv("MONGO_TEST_DB", "attendance_test")

import pytest
from fastapi.testclient import TestClient

from app.main import app, employees_col, logs_col, db


@pytest.fixture(scope="session")
def _app_client():
    with TestClient(app) as c:          # runs lifespan once -> creates indexes
        yield c


@pytest.fixture()
def client(_app_client):
    assert db.name.endswith("_test"), "refusing to clean a non-test database"
    employees_col.delete_many({})
    logs_col.delete_many({})
    return _app_client
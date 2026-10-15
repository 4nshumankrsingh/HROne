"""Unit tests for the pure helpers in app/main.py (no database needed)."""
from datetime import datetime, timedelta

import pytest
from pydantic import TypeAdapter, ValidationError

from app.main import (
    IST, UTC, DateStr, EpochMs, MonthStr, attendance_date, compute_late_minutes,
    compute_overtime_minutes, compute_work_hours, derive_fields, dt_to_ms, is_half_day,
    ms_to_dt, record_to_api, round_half_up, truncate_seconds,
)


def ist(y, mo, d, h, mi, s=0, us=0):
    return datetime(y, mo, d, h, mi, s, us, tzinfo=IST)


# ---- time conversion -------------------------------------------------------
def test_ms_roundtrip():
    ms = 1783312500123
    assert dt_to_ms(ms_to_dt(ms)) == ms


def test_dt_to_ms_accepts_naive_as_utc():
    assert dt_to_ms(datetime(1970, 1, 1, 0, 0, 1)) == 1000


def test_truncate_seconds():
    assert truncate_seconds(ist(2026, 7, 6, 9, 40, 0, 900_000)) == ist(2026, 7, 6, 9, 40, 0)


# ---- rounding --------------------------------------------------------------
def test_round_half_up_differs_from_bankers():
    assert round_half_up(0.125, 2) == 0.13      # Python round(0.125, 2) == 0.12
    assert round_half_up(2.675, 2) == 2.68      # float round() gives 2.67
    assert round_half_up(0.0049, 4) == 0.0049


# ---- R1: attendance date ---------------------------------------------------
def test_date_normal_shift():
    assert attendance_date(ist(2026, 7, 6, 9, 28), "09:30", "18:30") == "2026-07-06"


def test_date_overnight_punch_before_midnight():
    assert attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == "2026-07-06"


def test_date_overnight_early_morning_belongs_to_previous_day():
    assert attendance_date(ist(2026, 7, 7, 5, 59, 59), "22:00", "06:00") == "2026-07-06"


def test_date_overnight_at_shift_end_is_today():
    assert attendance_date(ist(2026, 7, 7, 6, 0, 0), "22:00", "06:00") == "2026-07-07"


def test_date_uses_ist_not_utc():
    # 2026-07-06 20:00 UTC is already 2026-07-07 01:30 IST
    assert attendance_date(datetime(2026, 7, 6, 20, 0, tzinfo=UTC), "09:30", "18:30") == "2026-07-07"


# ---- R2: late --------------------------------------------------------------
@pytest.mark.parametrize("h,m,s,expected", [
    (9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45), (9, 30, 0, 0), (9, 0, 0, 0), (10, 5, 0, 35),
])
def test_late(h, m, s, expected):
    assert compute_late_minutes(ist(2026, 7, 6, h, m, s), "2026-07-06", "09:30") == expected


def test_late_truncates_subseconds_first():
    # 09:40:00.900 counts as 09:40:00 -> not late
    assert compute_late_minutes(ist(2026, 7, 6, 9, 40, 0, 900_000), "2026-07-06", "09:30") == 0


def test_late_overnight_shift_early_punch_is_not_late():
    assert compute_late_minutes(ist(2026, 7, 6, 21, 55), "2026-07-06", "22:00") == 0


# ---- R3: overtime ----------------------------------------------------------
@pytest.mark.parametrize("h,m,expected", [(18, 35, 0), (19, 0, 30), (18, 59, 0), (19, 10, 40), (17, 0, 0)])
def test_overtime_normal(h, m, expected):
    assert compute_overtime_minutes(ist(2026, 7, 6, h, m), "2026-07-06", "09:30", "18:30") == expected


def test_overtime_overnight_sample_doc():
    # sample doc 7: out 06:40 next day, shift ends 06:00 -> 40
    assert compute_overtime_minutes(ist(2026, 7, 7, 6, 40), "2026-07-06", "22:00", "06:00") == 40


# ---- R4 / R5: work hours, half day ----------------------------------------
def test_work_hours_sample_docs():
    assert compute_work_hours(ist(2026, 7, 6, 9, 28), ist(2026, 7, 6, 18, 35)) == 9.12
    assert compute_work_hours(ist(2026, 7, 6, 21, 55), ist(2026, 7, 7, 6, 40)) == 8.75


def test_work_hours_half_up_on_exact_tie():
    t0 = ist(2026, 7, 6, 9, 0)
    assert compute_work_hours(t0, t0 + timedelta(seconds=450)) == 0.13      # 0.125 h


def test_half_day_uses_rounded_hours():
    t0 = ist(2026, 7, 6, 9, 0)
    assert compute_work_hours(t0, t0 + timedelta(seconds=16182)) == 4.5     # 4.495 h rounds up
    assert is_half_day(4.5) is False
    assert is_half_day(4.49) is True
    assert is_half_day(3.5) is True


# ---- derive_fields ---------------------------------------------------------
def test_derive_absent_and_leave():
    for st in ("ABSENT", "LEAVE"):
        assert derive_fields(st, None, None, "2026-07-08", "09:30", "18:30") == {
            "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


def test_derive_open_record():
    d = derive_fields("PRESENT", ist(2026, 7, 7, 10, 5), None, "2026-07-07", "09:30", "18:30")
    assert d == {"work_hours": None, "late_minutes": 35, "overtime_minutes": 0, "half_day": False}


def test_derive_closed_record_matches_sample_doc_2():
    d = derive_fields("PRESENT", ist(2026, 7, 6, 10, 5), ist(2026, 7, 6, 19, 10), "2026-07-06", "09:30", "18:30")
    assert d == {"work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40, "half_day": False}


# ---- validated types -------------------------------------------------------
@pytest.mark.parametrize("bad", [1783312500, 1783312500.5, "1783312500000", True, 4_102_444_800_001, None])
def test_epoch_ms_rejects(bad):
    with pytest.raises(ValidationError):
        TypeAdapter(EpochMs).validate_python(bad)


def test_epoch_ms_accepts_valid():
    assert TypeAdapter(EpochMs).validate_python(1783312500000) == 1783312500000


@pytest.mark.parametrize("bad", ["2026-02-30", "2026-13-01", "20260101", "2026-1-1", "abc"])
def test_date_rejects(bad):
    with pytest.raises(ValidationError):
        TypeAdapter(DateStr).validate_python(bad)


def test_month_pattern():
    ta = TypeAdapter(MonthStr)
    assert ta.validate_python("2026-07") == "2026-07"
    for bad in ("2026-13", "2026-00", "2026-7", "26-07"):
        with pytest.raises(ValidationError):
            ta.validate_python(bad)


# ---- serialiser ------------------------------------------------------------
def test_record_to_api_legacy_doc_and_no_id():
    legacy = {"_id": "x", "emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
              "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=UTC),
              "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=UTC),
              "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0}
    r = record_to_api(legacy)
    assert "_id" not in r and r["history"] == [] and r["half_day"] is False
    assert isinstance(r["punch_in"], int)


def test_record_to_api_history_dates_become_ms():
    doc = {"emp_code": "E", "date": "2026-07-07", "status": "PRESENT", "punch_in": None, "punch_out": None,
           "history": [{"at": datetime(2026, 7, 8, 5, 0, tzinfo=UTC), "by": "hr", "reason": "glitch",
                        "changes": {"punch_in": {"from": datetime(2026, 7, 7, 4, 35, tzinfo=UTC),
                                                 "to": datetime(2026, 7, 7, 3, 58, tzinfo=UTC)},
                                    "late_minutes": {"from": 35, "to": 0}}}]}
    h = record_to_api(doc)["history"][0]
    assert isinstance(h["at"], int)
    assert isinstance(h["changes"]["punch_in"]["from"], int)
    assert h["changes"]["late_minutes"] == {"from": 35, "to": 0}
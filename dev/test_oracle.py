"""Checks the reference oracle against numbers worked out BY HAND for the tiny dataset in gen_data.py.

July 2026: the 1st is a Wednesday -> 23 weekdays. EMP0002 joins on the 15th (Wed) -> 13 working days.
"""
from gen_data import big_dataset, tiny_dataset
from oracle import (ref_department_summary, ref_department_trend, ref_employee_monthly,
                    ref_late_leaderboard)

EMPS, LOGS = tiny_dataset()


# ---- employee monthly ------------------------------------------------------
def test_monthly_full_employee():
    # present: Jul 1 (1) + Jul 2 (1) + Jul 3 half (0.5) + Jul 8 on-duty (1) + Jul 9 open (1) = 4.5
    # the Saturday record (Jul 4) is NOT present_days but counts for late (20) and nothing else
    assert ref_employee_monthly(EMPS, LOGS, "EMP0001", "2026-07") == {
        "emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 4.5, "leave_days": 1,
        "late_count": 2, "total_late_minutes": 35, "total_overtime_minutes": 75, "attendance_pct": 19.57}


def test_monthly_mid_month_joiner():
    r = ref_employee_monthly(EMPS, LOGS, "EMP0002", "2026-07")
    assert (r["working_days"], r["present_days"], r["late_count"], r["total_late_minutes"]) == (13, 2.0, 1, 12)
    assert r["attendance_pct"] == 15.38                                   # 2 / 13 * 100


def test_monthly_employee_with_no_logs():
    r = ref_employee_monthly(EMPS, LOGS, "EMP0003", "2026-07")
    assert (r["working_days"], r["present_days"], r["attendance_pct"], r["late_count"]) == (23, 0.0, 0.0, 0)


def test_monthly_not_yet_joined_has_null_pct():
    r = ref_employee_monthly(EMPS, LOGS, "EMP0004", "2026-07")
    assert r["working_days"] == 0 and r["attendance_pct"] is None


def test_monthly_unknown_employee_is_none():
    assert ref_employee_monthly(EMPS, LOGS, "EMP4242", "2026-07") is None


# ---- department summary ----------------------------------------------------
def test_summary_july():
    assert ref_department_summary(EMPS, LOGS, "2026-07") == {"month": "2026-07", "items": [
        # Eng headcount 3: EMP0004 joins in August, EMP0003 has no logs but still counts.
        # avg hours = (9+8.5+4+3+8 + 9+9) / 7 = 7.2142... -> 7.21 (open record has no hours)
        {"department": "Engineering", "headcount": 3, "present_days": 6.5, "avg_work_hours": 7.21,
         "late_count": 3, "total_late_minutes": 47, "leave_count": 1, "on_duty_count": 1},
        # Ops (only member joins Aug 1) is omitted; the orphan log (EMP9999) is ignored.
        {"department": "Sales", "headcount": 4, "present_days": 6.0, "avg_work_hours": 8.0,
         "late_count": 6, "total_late_minutes": 177, "leave_count": 0, "on_duty_count": 0},
    ]}


def test_summary_department_filter_and_august():
    assert [i["department"] for i in ref_department_summary(EMPS, LOGS, "2026-07", "Sales")["items"]] == ["Sales"]
    aug = {i["department"]: i for i in ref_department_summary(EMPS, LOGS, "2026-08")["items"]}
    assert aug["Engineering"]["headcount"] == 4 and aug["Ops"]["headcount"] == 1
    assert aug["Ops"]["avg_work_hours"] is None and aug["Ops"]["present_days"] == 0.0


# ---- leaderboard -----------------------------------------------------------
def rows(**kw):
    return [(r["rank"], r["emp_code"]) for r in ref_late_leaderboard(EMPS, LOGS, "2026-07", **kw)["items"]]


def test_leaderboard_ranks_skip_after_ties():
    # totals: 0005=60, 0006=60, 0007=45, 0001=35, 0002=12, 0008=12  ->  ranks 1,1,3,4,5,5
    assert rows(limit=50) == [(1, "EMP0005"), (1, "EMP0006"), (3, "EMP0007"), (4, "EMP0001"),
                              (5, "EMP0002"), (5, "EMP0008")]


def test_leaderboard_limit_applies_after_ranking():
    assert rows(limit=1) == [(1, "EMP0005"), (1, "EMP0006")]              # more rows than limit
    assert rows(limit=2) == [(1, "EMP0005"), (1, "EMP0006")]              # rank 3 > 2
    assert rows(limit=3)[-1] == (3, "EMP0007")
    assert len(rows(limit=5)) == 6                                         # tie at rank 5 both included


def test_leaderboard_department_ranks_inside_department():
    assert rows(limit=10, department="Sales") == [(1, "EMP0005"), (1, "EMP0006"), (3, "EMP0007"), (4, "EMP0008")]
    assert rows(limit=10, department="Engineering") == [(1, "EMP0001"), (2, "EMP0002")]


def test_leaderboard_row_fields_and_orphans_ignored():
    r = ref_late_leaderboard(EMPS, LOGS, "2026-07")["items"][0]
    assert r == {"rank": 1, "emp_code": "EMP0005", "name": "Esha Sales", "department": "Sales",
                 "total_late_minutes": 60, "late_count": 2}
    assert all(i["emp_code"] != "EMP9999" for i in ref_late_leaderboard(EMPS, LOGS, "2026-07", limit=50)["items"])


# ---- trend -----------------------------------------------------------------
def test_trend_first_nine_days_of_july():
    t = ref_department_trend(EMPS, LOGS, "Engineering", "2026-07-01", "2026-07-09")["items"]
    assert [r["date"][-2:] for r in t] == ["01", "02", "03", "04", "05", "06", "07", "08", "09"]
    assert [r["is_working_day"] for r in t] == [True, True, True, False, False, True, True, True, True]
    assert [r["headcount"] for r in t] == [2] * 9
    assert [r["present_count"] for r in t] == [1.0, 1.0, 0.5, 0.5, 0.0, 0.0, 0.0, 1.0, 1.0]
    assert [r["late_count"] for r in t] == [0, 1, 0, 1, 0, 0, 0, 0, 0]
    assert [r["attendance_rate"] for r in t] == [0.5, 0.5, 0.25, None, None, 0.0, 0.0, 0.5, 0.5]
    assert [r["moving_avg_7d"] for r in t] == [0.5, 0.5, 0.4167, 0.4167, 0.4167, 0.3125, 0.25, 0.25, 0.25]


def test_trend_headcount_grows_when_someone_joins():
    t = ref_department_trend(EMPS, LOGS, "Engineering", "2026-07-14", "2026-07-16")["items"]
    assert [r["headcount"] for r in t] == [2, 3, 3]
    assert [r["attendance_rate"] for r in t] == [0.0, 0.3333, 0.3333]
    assert [r["moving_avg_7d"] for r in t] == [0.0, 0.1667, 0.2222]


def test_trend_no_working_day_gives_null_average():
    t = ref_department_trend(EMPS, LOGS, "Engineering", "2026-07-04", "2026-07-05")["items"]
    assert [r["moving_avg_7d"] for r in t] == [None, None]


def test_trend_unknown_department_is_none():
    assert ref_department_trend(EMPS, LOGS, "Nope", "2026-07-01", "2026-07-02") is None


# ---- generator sanity ------------------------------------------------------
def test_big_dataset_is_well_formed():
    emps, logs = big_dataset(60, seed=1)
    keys = [(l["emp_code"], l["date"]) for l in logs]
    assert len(keys) == len(set(keys))                                     # one record per (emp, date)
    assert {e["emp_code"] for e in emps} >= {l["emp_code"] for l in logs}
    assert any(l["punch_out"] is None and l["status"] == "PRESENT" for l in logs)   # open records
    assert any(l["status"] == "LEAVE" for l in logs) and any(l["half_day"] for l in logs)
    assert any(l["late_minutes"] > 0 for l in logs)
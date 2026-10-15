# REVIEW.md

Defects found in the starter `app/main.py` (helpers and endpoints). Each was reproduced against the starter before being fixed.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Grace check is `int(seconds / 60) > 10`, so anything between 10:00 and 10:59 after the shift start floors to 10 and is treated as on time. R2 says late is strictly more than 10:00. | Shift 09:30, punch-in 09:40:01 returns 0 (expected 10); 09:40:30 also returns 0. | Compare whole seconds against 600, then floor to minutes (`delta > 600` then `delta // 60`). |
| 2 | `compute_late_minutes` | Shift start is built with `punch_in.replace(hour, minute)` on the punch's own calendar date and zone, ignoring the R1 attendance date. Overnight shifts break, results depend on the server time zone, and sub-seconds are not truncated. | Shift 22:00, punch-in 05:30 next morning returns 0 instead of being measured from the previous day's 22:00 start. | `shift_start_instant(attendance_date, IST)`, all datetimes UTC-aware, `truncate_seconds` first. |
| 3 | `compute_work_hours` | Uses Python `round()` on a float: banker's rounding plus float representation error. R4 requires half-up on seconds / 3600. | 450 s (0.125 h) returns 0.12, expected 0.13. | Integer seconds, `Decimal` and `ROUND_HALF_UP` (`round_half_up`). |
| 4 | `compute_overtime` | No 30-minute minimum (R3). | Shift end 18:30, punch-out 18:35 returns 5, expected 0. | Return minutes only when they are at least 30. |
| 5 | `compute_overtime` | Shift end is built on the date string's own day, so overnight shifts (end on the next day) are wrong. It also subtracts an aware end from a naive punch-out. | 22:00-06:00 shift, punch-out 06:40 next day returns 1480 instead of 40. A naive `punch_out` raises `TypeError`. | `shift_end_instant` adds a day for overnight shifts; everything is aware. |
| 6 | `MongoClient(...)` | No `tz_aware`, so PyMongo returns naive datetimes. Mixing them with aware values raises `TypeError`, and naive local times get stored. | Subtracting a value read from the DB from an IST datetime raises `TypeError`. | `tz_aware=True`; instants are UTC-aware throughout. |
| 7 | startup (missing) | No indexes at all: duplicates are possible and every query is a collection scan. | Duplicate documents after parallel requests; explain shows COLLSCAN. | Idempotent `create_indexes()` in the FastAPI lifespan. |
| 8 | `GET /health` | Always returns ok and never touches MongoDB. | Stop MongoDB: still 200. | `ping` the database, 503 on failure. |
| 9 | `create_employee` | Check-then-insert race with no unique index. | 16 parallel POSTs with the same `emp_code` can return several 201s. | Unique index on `emp_code`; catch `DuplicateKeyError` and return 409. |
| 10 | `EmployeeIn` | No validation: accepts `email="bad-email"`, `joined_on="not-a-date"`, any `emp_code`, `shift_start == shift_end`, empty names. | POST with those values returns 201 instead of 422. | `EmployeeCreate` with patterns, length limits, a real-date check and a shift check. |
| 11 | `create_employee` | `created_at` is a naive local `datetime.now()` and is returned as an ISO string, not epoch milliseconds. | Response `created_at` is a string. | UTC-aware time, returned as epoch ms via `employee_to_api`. |
| 12 | `list_employees` | `skip = page * page_size` skips one page too many. | With 6 employees, `GET /employees` returns an empty list. | `skip = (page - 1) * page_size`. |
| 13 | `list_employees` | `total` counts the whole collection and ignores the department filter. | `department=B` shows `total=25` with no matching items. | `count_documents(filter)`. |
| 14 | `list_employees` | No sort (unstable paging) and no bounds: `page_size=0` becomes `limit(0)`, which Mongo treats as no limit, so everything is returned. | `page_size=0` returns all 25 rows with status 200. | Sort by `emp_code`; `page >= 1` and `1 <= page_size <= 100` give 422; a skip beyond the total returns empty items. |
| 15 | `punch_in` | Unknown employee: `emp["shift_start"]` on `None` crashes. | Punch-in for an unknown `emp_code` returns 500. | Return 404. |
| 16 | `punch_in` | `status` is any string, so `ABSENT` and `LEAVE` can be created by punch-in. | `status=ABSENT` returns 201. | `Literal["PRESENT", "WFH", "ON_DUTY"]`. |
| 17 | `punch_in` | `punched_at` is unvalidated and tested for truthiness. Seconds-looking values give a 1970 date, `0` is treated as missing, and huge values raise `OverflowError`. | `punched_at=1783312500` returns 201 with a 1970 date; `10**20` returns 500; `0` is silently "now". | Strict `EpochMs` type (int, 1e11 to 4.1e12) and an `is not None` check. |
| 18 | `punch_in` | `datetime.fromtimestamp(ms / 1000)` uses server-local naive time and float division. The date comes from that local calendar day instead of the R1 IST/overnight rule, and sub-seconds are not truncated. | Run the server in a non-IST zone: attendance dates shift. Overnight early-morning punches get the wrong date. | `ms_to_dt` (UTC-aware), `truncate_seconds`, `attendance_date`. |
| 19 | `punch_in` | Check-then-insert race and no unique `(emp_code, date)` index. | Parallel identical punch-ins create duplicates or several 201s. | Unique compound index; `DuplicateKeyError` returns 409. |
| 20 | `punch_in` response | Leaks `id` (the `_id`) and returns datetimes instead of epoch ms. | Response contains `id` and ISO strings. | `record_to_api` serialiser. |
| 21 | `list_attendance` | Loads every matching document into memory, sorts in Python and slices. `total` comes from `len(docs)`. | Slow and memory-heavy at 100k records; explain shows COLLSCAN. | Database-side sort, skip and limit, `count_documents`, and supporting indexes. |
| 22 | `list_attendance` | Sorts by `date` only (contract: `date` descending, then `emp_code` ascending). | Same-date rows come back in arbitrary order. | Sort `[("date", -1), ("emp_code", 1)]`. |
| 23 | `list_attendance` | No validation: `date_from > date_to` returns 200, malformed dates and unknown statuses are accepted, `page_size` is unbounded, `page < 1` slices negatively. | `date_from=2026-02-01&date_to=2026-01-01` returns 200; `page_size=-5` returns 200. | Typed query parameters (`DateStr`, status enum), bounds, explicit 422. |
| 24 | `list_attendance` | Leaks `id`; datetimes are not epoch ms; legacy documents without `half_day` or `history` are returned without defaults. | Response has `id`; legacy docs lack fields. | `record_to_api` applies defaults and converts instants. |

## Examined and judged not a defect

- `load_dotenv()` does not override real environment variables, which is what the contract requires.
- Endpoints are plain `def`, so FastAPI runs them in a thread pool and PyMongo is thread-safe; async is not needed.
- `doc.pop("_id")` after `insert_one` is correct (PyMongo adds `_id` to the dict in place).
- Default shifts 09:30 / 18:30 and the default `status=PRESENT` match the contract.
- Client-sent derived fields are ignored because Pydantic drops unknown fields by default.
- The starter's helpers are meant for reuse, but none is safe as written (rows 1-5).
# REVIEW.md

Defects found in the starter `app/main.py`, all fixed in this version.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Grace check uses `int(seconds/60) > 10`, so 09:40:01 gives 10 min and is treated as on time (R2 says strictly more than 10:00). | Shift 09:30, punch-in 09:40:01 -> expected 10, got 0. | Compare whole seconds: late only if `seconds > 600`, then `seconds // 60`. |
| 2 | `compute_late_minutes` | Builds shift start from the punch-in's own date and mixes naive/aware values; wrong for overnight shifts (punch-in after midnight belongs to the previous day's shift). | Overnight 22:00-06:00 punch-in at 01:00 -> negative or zero late. | Shift start is derived from the record's attendance date in IST (`shift_bounds`). |
| 3 | `compute_work_hours` | `round(x, 2)` on a float is not half-up (banker's rounding plus float error). | 27090 s = 7.525 h -> may give 7.52 instead of 7.53. | Integer maths: `(200*seconds + 3600) // 7200` hundredths. |
| 4 | `compute_overtime` | No 30-minute minimum, ignores overnight shifts (end is next day), `replace()` keeps stale seconds, naive date + `tzinfo=IST` handling is fragile, and it is never called. | Punch-out 10 min after shift end returns 10 instead of 0. | Rewritten with `shift_bounds` and the >= 30 rule; wired into punch-out. |
| 5 | `punch_in` timestamps | `datetime.fromtimestamp(ms/1000)` uses the server's local zone (naive), not UTC/IST; milliseconds are not truncated; `if body.punched_at` treats 0 as missing; no range/seconds-vs-ms validation. | Same request gives a different `date` on a UTC server vs IST laptop; `punched_at=1700000000` accepted. | Strict `EpochMs` type (422 on seconds/floats/strings), UTC-aware datetimes truncated to seconds, `date` computed in IST (R1). |
| 6 | `punch_in` | Unknown employee: `emp` is `None`, `emp["shift_start"]` raises -> 500. | POST with unknown `emp_code`. | 404 via `get_employee`. |
| 7 | `punch_in` | Check-then-insert race: two simultaneous requests both pass `find_one` and both insert. | Fire 2 parallel punch-ins -> two 201s, duplicate rows. | Unique index `(emp_code, date)`; catch `DuplicateKeyError` -> 409. |
| 8 | `PunchInIn.status` | Any string accepted, including `ABSENT`/`LEAVE` or garbage, and the response leaked `id` (the `_id`). | `status="FOO"` returns 201. | `Literal["PRESENT","WFH","ON_DUTY"]`; `_id` never returned. |
| 9 | `punch_in` response | Returned raw datetimes (not epoch ms), `id`, and left `late_minutes` etc. only partly typed. | Response `punch_in` is an ISO string. | `ser_record` converts to epoch ms and the documented fields only. |
| 10 | `create_employee` | Check-then-insert race on `emp_code`; no unique index. | Parallel creates -> several 201s. | Unique index + `DuplicateKeyError` -> 409. |
| 11 | `EmployeeIn` | No validation: emp_code format, email, `HH:MM` shifts, `shift_start == shift_end`, invalid `joined_on` like 2026-02-30. | POST `{"shift_start":"25:99"}` returns 201. | Field patterns, date validator, model validator; 422. |
| 12 | `create_employee` | `created_at = datetime.now()` is naive local time and returned as a datetime, not epoch ms. | Value differs by server timezone. | UTC-aware `now_utc()` and epoch-ms output. |
| 13 | `list_employees` | `skip = page * page_size` skips the first page (page starts at 1). | `page=1` returns the second page. | `(page-1) * page_size`. |
| 14 | `list_employees` | `total` counts the whole collection, ignoring the department filter; no sort; `page`/`page_size` unbounded (0, -1, 10^6). | `department=Sales` shows total of all employees. | `count_documents(q)`, sort by `emp_code`, `page >= 1`, `1 <= page_size <= 100` (R10). |
| 15 | `list_attendance` | Loads the entire collection into memory and sorts/paginates in Python; no index. | Slow / memory heavy at 100k rows. | Server-side `find().sort().skip().limit()` backed by indexes. |
| 16 | `list_attendance` | Sort only by `date` (no `emp_code` tie-break), `page=0` gives a negative slice, no `date_from > date_to` check, no date-format validation, unbounded `page_size`. | `page=0`, `date_from=2026-08-01&date_to=2026-07-01`. | Sort `(date desc, emp_code asc)`, validated params, 422 on bad ranges. |
| 17 | `list_attendance` | Output leaks `id`, returns naive datetimes, and omits defaults for legacy docs (missing `history`/`half_day`). | Legacy record lacks `half_day`. | `ser_record` normalises everything. |
| 18 | `/health` | Always returns ok even if MongoDB is down. | Stop Mongo, still 200. | Pings MongoDB; 503 on failure. |
| 19 | `MongoClient` | Default naive datetimes and no server selection timeout (requests hang 30 s when Mongo is down). | Comparing/serialising datetimes breaks; hangs. | `tz_aware=True`, 5 s timeout, `PyMongoError` -> 503. |
| 20 | Startup | No indexes at all. | `explain` shows COLLSCAN. | Idempotent index creation at startup. |

**Looked at and judged fine:** reading `MONGO_URI`/`MONGO_DB` from the environment with `load_dotenv()` (it does not override real env vars, which is what the contract wants); `joined_on` and `date` kept as strings (matches the data model); returning 201 for create/punch-in.

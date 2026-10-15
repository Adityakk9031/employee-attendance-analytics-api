# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (business key, race-safe create, list sort), `(department, emp_code)`, `(department, joined_on)` and `(joined_on)` for department/headcount queries. `attendance_logs`: unique `(emp_code, date)` (punch-in race, punch-out, PATCH, monthly, per-employee lookups), `(date desc, emp_code)` for the list order and month scans, and `(status, date desc, emp_code)`. Rejected: a standalone `status` index (too low selectivity) and copying `department` into every log.
2. **Punch-in race.** Both requests read the employee and build the same document, then call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept exactly one; the other gets `DuplicateKeyError`, which I turn into 409. No check-then-insert.
3. **Ties.** `$setWindowFields` with `$rank` gives standard competition ranks (1, 2, 2, 4). I filter `rank <= limit` after ranking, so everyone tied at the cutoff is returned, even if that exceeds `limit`.
4. **Headcount.** The summary starts from `employees` (joined on or before month end), then `$lookup`s logs per employee. An employee with no logs gets an empty array and still adds 1 to headcount.
5. **At 100x data.** I would pre-aggregate per employee per month (or use a time-series/summary collection) instead of scanning raw logs on every request, and cache results.

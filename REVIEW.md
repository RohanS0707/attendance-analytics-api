# REVIEW.md

I read the starter `app/main.py` the way I would read a teammate's pull request. The happy path works, but I found
21 problems. Line numbers refer to the starter file, and every problem listed here is fixed in my `app/main.py`.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `MongoClient`, L22 | The client is not `tz_aware`, so datetimes from MongoDB come back naive. Naive and aware datetimes cannot be mixed. | Subtracting an aware time from a stored `punch_in` raises `TypeError`: HTTP 500. | `tz_aware=True`, everything in UTC. Also a short `serverSelectionTimeoutMS`, so a missing database fails quickly. |
| 2 | `compute_late_minutes`, L35 | The shift time (IST) is put on the punch-in's own date and timezone. | Server in UTC: a 09:40 IST punch-in (04:10 UTC) is compared with 09:30 UTC, looks early, so late = 0. | Convert to IST and build the shift start from the attendance date in IST. |
| 3 | `compute_late_minutes`, L36-37 | `int(seconds / 60)` and then `> 10`. The rule says strictly more than 10:00, counted in seconds. Milliseconds are not cut off either. | Shift 09:30, punch-in 09:40:01: `int(10.016)` is 10 and `10 > 10` is false, so 0. It should be 10. | Compare seconds `> 600`, then floor to minutes. Milliseconds are dropped when the timestamp is read. |
| 4 | `compute_late_minutes` L35, record date L103 | For overnight shifts the shift start and the record date are always on the punch-in's calendar day. | Shift 22:00-06:00, punch-in 03:00 on 7 July belongs to the 6 July record. The starter files it under 7 July and gets a negative lateness (so 0). | `attendance_date()`: for an overnight shift, a punch-in before `shift_end` belongs to the previous day. |
| 5 | `compute_work_hours`, L41 | Python `round()` on a float is not half-up. Rule R4 wants half-up. | 9630 s is 2.675 h. `round(2.675, 2)` gives 2.67, it should be 2.68. | Whole seconds divided with `Decimal`, then `quantize(0.01, ROUND_HALF_UP)`. |
| 6 | `compute_overtime`, L44-47 | No 30-minute minimum (R3). For overnight shifts the shift end lands on the wrong day. `end` has a timezone, a punch-out read from the DB is naive. | Shift end 18:30, punch-out 18:55: gives 25, should be 0. Shift 22:00-06:00, punch-out 06:40 next day: about 1480 instead of 40. | Overtime only when it is 30 minutes or more. An overnight shift ends on the next day. Everything timezone-aware. |
| 7 | `EmployeeIn`, L53-60 | No validation at all. | `emp_code` "abc", email "x", `shift_start` "25:99", equal start and end, `joined_on` "2026-02-30", empty name: all return 201. | Patterns and lengths on each field, plus a check for a real date and `shift_start != shift_end`. 422 on failure. `joined_on` must be exactly `YYYY-MM-DD`. |
| 8 | `PunchInIn`, L63-66 | `status: str` takes anything. `punched_at: int` takes seconds, negatives, whole-number floats and numeric strings. | `{"status": "ABSENT"}` gives 201. `punched_at` in seconds gives a date in 1970. A huge number makes `fromtimestamp` fail: 500. | `Literal["PRESENT","WFH","ON_DUTY"]`, `StrictInt`, and the epoch-millisecond range from the contract. 422 on failure. |
| 9 | `health`, L72-74 | Always answers ok. The contract wants 503 when MongoDB does not answer a ping. | Stop MongoDB: `/health` still returns 200. | A real `ping`, 503 if it fails. It also builds the indexes once the database is reachable. |
| 10 | `create_employee`, L79-83 | Check then insert (`find_one`, then `insert_one`) is a race. Two requests at the same moment both pass the check. | 12 identical `POST /employees` at once: several 201 and duplicate employees stored. | Unique index on `emp_code`; `insert_one` and catch `DuplicateKeyError` for 409. |
| 11 | `create_employee`, L82 | `datetime.now()` is local naive time and is returned as a datetime string. The contract wants UTC and epoch milliseconds. | `created_at` depends on the server timezone and is a string, not an integer. | `now_utc()` and `dt_to_ms()` in the response. |
| 12 | `list_employees`, L93 | `skip = page * page_size` skips one page too many. | `GET /employees?page=1` returns the second page (starts at the 21st employee). The first page is only reachable with the invalid `page=0`. | `skip = (page - 1) * page_size`. |
| 13 | `list_employees`, L94 | `total` counts the whole collection, not the filtered set. | `?department=Sales` returns 2 items but `total` is 6. | `count_documents(q)` with the same filter. |
| 14 | `list_employees`, L89-97 | No sort, and no limits on `page` or `page_size`. | The natural order is not guaranteed, so pages can repeat or miss employees. `page=-1` gives a negative skip: 500. `page_size=100000` is accepted. | Sort by `emp_code`. `page >= 1` and `1 <= page_size <= 100`, otherwise 422. |
| 15 | `punch_in`, L101 | Unknown employee: `emp` is `None`, and `emp["shift_start"]` raises `TypeError`. | `POST /attendance/punch-in {"emp_code": "EMP9999"}` gives 500. | An explicit 404. |
| 16 | `punch_in`, L102 | `datetime.fromtimestamp(ms / 1000)` uses the server's local timezone and keeps milliseconds. `if body.punched_at` treats 0 as "not given". The date is the local date, not the IST date. | The result changes with the server timezone. 09:40:00.900 is not treated as 09:40:00. | UTC datetime cut to whole seconds, `is not None`, and the date in IST (see #4). |
| 17 | `punch_in`, L104-118 | The same check-then-insert race, now for the pair (employee, date). | 12 punch-ins at once: more than one 201 and several records for the same day. | Unique index on `(emp_code, date)` and catch `DuplicateKeyError` for 409. Result: one 201, eleven 409. |
| 18 | `punch_in` / `list_attendance`, L119-122, L149 | The response leaks `id` (the ObjectId as text), which is not in the contract. Times are datetimes, not epoch milliseconds. Missing fields in old documents are not defaulted. | The response has an `id` field, and `punch_in` is an ISO string. | `attendance_out()`: no `_id`, epoch milliseconds, defaults for `late_minutes`, `overtime_minutes`, `half_day`, `history`. |
| 19 | `list_attendance`, L144-147 | Loads the whole collection into Python, sorts and slices there, and `total = len(docs)`. No tie-break in the sort. | Fine with 9 records, slow and heavy with 100,000. Equal dates come back in any order. | Filter, sort `(date desc, emp_code asc)`, skip and limit all run in MongoDB, backed by an index. `count_documents` for the total. |
| 20 | `list_attendance`, L125-143 | The filters are not validated. | `date_from` after `date_to`, `date_from=2026-13-40`, `status=FOO`, `page_size=5000`: all return 200. | Date pattern and real-date check, `Literal` for `status`, `date_from <= date_to`, page limits. 422 on failure. |
| 21 | Whole file | No `create_index` anywhere: no indexes are created at startup. | Duplicates are possible, sorting needs an in-memory sort, and explain shows `COLLSCAN`. | `ensure_indexes()` at startup, safe to run many times. See DECISIONS.md. |

## Things I looked at and decided are fine

- **Plain `def` endpoints with PyMongo.** The calls block, but FastAPI runs `def` endpoints in a thread pool, so
  the event loop is not blocked. I kept it. Motor would only add complexity here.
- **`load_dotenv()` at import.** It does not override real environment variables, which is what the contract asks for.
- **Dates stored as `YYYY-MM-DD` strings.** Comparing them as strings only works because they are zero-padded.
  That is why I enforce the format (`2026-1-5` would sort wrongly and break the `joined_on <= date` rule).
- **Derived fields (`late_minutes`, `work_hours`, ...) stored when writing.** This is what the data model
  describes. Analytics read the stored values, and they are recomputed whenever a record changes.
- **`doc.pop("_id")` after `insert_one`.** It keeps the generated `_id` out of the response. I kept it (as `employee_out`).
- **201 for creating things.** Matches the contract.

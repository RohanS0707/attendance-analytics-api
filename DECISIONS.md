# DECISIONS.md

1. Indexes.`attendance_logs`: `(emp_code, date)` unique, so one record per employee per day and punch-in is safe; `(date desc, emp_code)` for the list sort and month scans; `(emp_code, punch_in desc)` for punch-out; `(status, date, emp_code)` for the status filter. `employees`: `emp_code` unique, `(department, emp_code)`, `joined_on`. I rejected an index on `late_minutes` alone: the leaderboard narrows by month first.

2. Punch-in race. Both requests call `insert_one`. The unique `(emp_code, date)` index lets only one win, the other gets `DuplicateKeyError` and I return 409. I don't check first, because check-then-insert lets both through.

3. Ties: I use `$rank`, so equal minutes share a rank (1, 2, 2, 4), and I apply `limit` after ranking. A tie at the cutoff returns everyone tied, so there can be more rows than `limit`. Dropping one of two equal people would be arbitrary.

4. Headcount.: The summary starts from `employees`, not the logs. It takes employees who joined by month end and joins their logs with `$lookup`. `$unwind` with `preserveNullAndEmptyArrays` keeps people with no logs. Headcount counts distinct employee codes.

5. Change for 100x. I'd keep monthly totals in a separate collection so analytics stop scanning a month of records each time, and switch from `skip` to keyset pagination.

# DECISIONS.md

1. **Indexes.** On attendance I made three: `(emp_code, date)` unique, so one employee can never have two records for a day and per-employee lookups are fast; `(date, emp_code)` for the default list order and month queries; `(status, date, emp_code)` for the status filter. On employees: unique `emp_code`, `(department, emp_code)`, `(department, joined_on)` for headcount, and `joined_on`. I rejected an index that also stores `late_minutes` and `work_hours`: it makes every punch slower and 100k rows don't need it.

2. **Punch-in race.** Both requests work out the same date and try `insert_one`. The unique index lets only one insert succeed. The other gets a duplicate-key error and I return 409. I never check first and then insert.

3. **Ties.** I rank with `$rank`, so tied people share a rank and the next rank is skipped (1, 2, 2, 4). Then I keep `rank <= limit`. So a tie at the cutoff returns everyone tied, even if that is more than `limit` rows, sorted by `emp_code`.

4. **Headcount.** The summary starts from the employees collection, not the logs, then looks up each person's logs. Someone with no logs still counts as one.

5. **100x data.** I would stop joining at read time: store monthly totals per employee and department, update them on each punch, and copy `department` onto every log.

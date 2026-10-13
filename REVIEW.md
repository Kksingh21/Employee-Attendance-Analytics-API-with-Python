# REVIEW.md

Problems I found in the starter `app/main.py`, and how I fixed them. All shift times are IST (UTC+05:30).

| # | Where | What is wrong | How you would notice | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | It builds the shift start using the UTC date and clock, not IST. 09:30 IST gets treated as 09:30 UTC, so the answer is wrong for everyone. Overnight shifts are not handled. | Punch in at 10:05 IST on a 09:30 shift: should be 35 late minutes, starter gives the wrong number. | Build the shift start for the IST attendance date and subtract real instants. |
| 2 | `compute_late_minutes` | It floors to whole minutes before checking the 10-minute grace. 09:40:30 becomes "10 minutes" so it is not late, but the rule says anything over 10:00 is late. | 09:40:01 should give 10; starter gives 0. | Compare in seconds (`> 600`), then divide by 60. |
| 3 | `compute_work_hours` | Uses Python `round()`, which rounds ties to even and has float noise. The rule is half-up. Fractions of a second are not cut off. | 8.125 hours should be 8.13; starter gives 8.12. | Use `Decimal` with `ROUND_HALF_UP` on instants cut to whole seconds. |
| 4 | `compute_overtime` | Shift end is always on the same date, so overnight shifts go negative. No 30-minute minimum. Mixing naive and aware datetimes can crash. | Overnight sample: 06:40 out should give 40; starter gives 0. Leaving 5 minutes late gives 5, should be 0. | Roll the end to the next day for overnight shifts, floor minutes, keep only if 30 or more, use aware UTC datetimes everywhere. |
| 5 | Mongo client | Not timezone-aware, so PyMongo returns naive datetimes, and `datetime.now()` uses the server's local time. No timeout, no indexes. | Run the server in a non-UTC timezone: stored times shift. | `tz_aware=True` with UTC, a timeout, and indexes created at startup. |
| 6 | `/health` | Always says ok, never talks to MongoDB. | Stop MongoDB: still 200, should be 503. | Ping MongoDB; return 503 if it fails. |
| 7 | `POST /employees` | Checks for a duplicate and then inserts. Two requests at the same time can both pass the check. | 20 parallel creates of one `emp_code` give several 201s. | Unique index on `emp_code`; a duplicate-key error becomes 409. |
| 8 | `POST /employees` | No validation of code format, email, shift times, equal start and end, or a real `joined_on`. `created_at` is local naive time and not epoch milliseconds. | `shift_start="25:99"` is accepted. | Pydantic rules from the contract; `created_at` is UTC and returned in milliseconds. |
| 9 | `GET /employees` | `skip = page * page_size` skips the first page. `total` ignores the department filter. No sort. `page=0` and huge `page_size` are allowed. | `?page=1` starts at the 21st employee. | `(page - 1) * page_size`, count with the filter, sort by `emp_code`, limit `page >= 1` and `page_size <= 100`. |
| 10 | `punch-in` | Unknown employee gives `None`, then `emp["shift_start"]` crashes with a 500. | Punch in for `NOPE`. | Look up the employee first and return 404. |
| 11 | `punch-in` | Check-then-insert race, same as #7: two records for one day. | 10 identical parallel requests. | Unique `(emp_code, date)` index; duplicate-key error becomes 409. |
| 12 | `punch-in` | Attendance date uses the UTC or local date, not IST. No overnight rule. Sub-seconds are kept. | Punch at 00:30 IST lands on the wrong day. An overnight worker at 02:00 IST should belong to the previous day. | `attendance_date()` follows rule R1; times are cut to whole seconds before use and saving. |
| 13 | `punch-in` | `if body.punched_at` treats 0 as missing. Seconds-sized numbers, floats and strings are accepted. `status` can be any text, even `ABSENT`. | `punched_at=1783312500` (seconds) creates a record in 1970. | Strict integer between 1e11 and 4102444800000, else 422; status only PRESENT, WFH or ON_DUTY. |
| 14 | `punch-in` response | Returns `id` (the ObjectId) and `punch_in` as a datetime object, not milliseconds. | Compare with the contract. | One `record_out()` function builds every attendance response. |
| 15 | `GET /attendance` | Loads the whole collection into Python, sorts and slices there. Slow at 100k records. | Large dataset: very slow. | `find().sort().skip().limit()` using indexes, and `count_documents` for total. |
| 16 | `GET /attendance` | Sorts by date only, not then by `emp_code`. `page=0` breaks the slice. `page_size` unlimited. | Same-date rows swap between calls. | Sort by `date` descending then `emp_code` ascending; limit page values. |
| 17 | `GET /attendance` | Dates are not checked, `date_from` after `date_to` is allowed, `status` is any text. | `?date_from=banana`. | Strict date check, 422 for a reversed range, status must be one of the five values. |
| 18 | All time output | Datetimes come out as ISO text; the contract wants epoch milliseconds, including inside `history`. | Any response with a time. | Convert at the edge with `to_ms()`. |

## Things I checked and left alone
* `IST = timezone(timedelta(hours=5, minutes=30))` is right: India has a fixed offset and no daylight saving.
* `load_dotenv()` does not override real environment variables by default, which is what the contract asks for.
* A repeated punch-in should return 409. Only the way it was detected was wrong.
* `status_code=201` on the two POST endpoints is correct.
* Old records without `half_day` or `history` are handled when reading, so the data does not need changing.

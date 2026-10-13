# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in a local .env
uvicorn app.main:app --port 8000
```
Indexes are created idempotently at startup. `GET /health` returns 200 once MongoDB answers.

## Tests
```bash
pip install pytest httpx mongomock
pytest tests/test_logic.py tests/test_crud_mock.py        # no database needed
MONGO_URI=... MONGO_DB=attendance_test pytest tests/test_integration.py   # real MongoDB 6+
```
`test_integration.py` seeds the awkward cases (tie at the cutoff, employee with no logs, weekend record,
mid-month joiner, employee joining after the month, unknown-employee logs) and compares the analytics
and explain endpoints with an independent Python reference.

## Notes
* Everything is implemented: all endpoints in `openapi.yaml`, the three aggregation pipelines, `/admin/explain`.
* Analytics read the stored derived values; only the pipelines' output is rounded (half-up) in Python.
* Concurrency: punch-in relies on the unique `(emp_code, date)` index, punch-out and PATCH use compare-and-set updates
  (PATCH re-reads and retries up to 8 times, then answers 409), so history entries are never lost.
* `REVIEW.md` lists the starter defects; `DECISIONS.md` answers the five questions.

"""End-to-end analytics checks against a REAL MongoDB 6+ (needs $setWindowFields / $densify).
    MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_test pytest tests/test_integration.py
Skipped automatically when no MongoDB answers. Each expected value is computed by an independent
plain-Python reference below, from the same seeded rows, so a pipeline bug cannot hide behind itself."""
import os
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("MONGO_DB", "attendance_test")
from app import main as m  # noqa: E402

try:
    m.client.admin.command("ping")
except Exception:  # pragma: no cover
    pytest.skip("no MongoDB available", allow_module_level=True)

UTC = timezone.utc
IST = m.IST


def at(d, hh, mm):  # IST wall-clock -> UTC
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST).astimezone(UTC)


EMPS = [  # code, dept, joined_on
    ("EMP0001", "Eng", "2026-01-05"), ("EMP0002", "Eng", "2026-07-13"),   # mid-month joiner
    ("EMP0003", "Eng", "2026-02-01"), ("EMP0004", "Eng", "2026-03-01"),   # EMP0004: no logs at all
    ("EMP0005", "Ops", "2026-03-10"), ("EMP0006", "Ops", "2026-08-15"),   # EMP0006 joins after July
]
# (emp, date, status, in(h,m), out(h,m), half_day, late, ot)
ROWS = [
    ("EMP0001", date(2026, 7, 6), "PRESENT", (10, 0), (18, 30), False, 30, 0),
    ("EMP0001", date(2026, 7, 7), "WFH", (9, 30), (13, 30), True, 0, 0),
    ("EMP0001", date(2026, 7, 11), "PRESENT", (9, 50), (19, 30), False, 20, 60),   # Saturday: late/OT only
    ("EMP0003", date(2026, 7, 6), "PRESENT", (10, 0), (18, 30), False, 30, 0),     # ties EMP0001 on 30
    ("EMP0003", date(2026, 7, 7), "LEAVE", None, None, False, 0, 0),
    ("EMP0003", date(2026, 7, 8), "ON_DUTY", (9, 30), (18, 30), False, 0, 0),
    ("EMP0002", date(2026, 7, 14), "PRESENT", (9, 30), (18, 30), False, 0, 0),
    ("EMP0005", date(2026, 7, 6), "PRESENT", (9, 30), (18, 30), False, 15, 0),
    ("EMP0005", date(2026, 7, 9), "ABSENT", None, None, False, 0, 0),
]


@pytest.fixture(scope="module", autouse=True)
def seed():
    m.employees.delete_many({})
    m.logs.delete_many({})
    m.ensure_indexes()
    m.employees.insert_many([{"emp_code": c, "name": f"N{c}", "email": "a@b.co", "department": dep,
                              "shift_start": "09:30", "shift_end": "18:30", "joined_on": j,
                              "created_at": datetime.now(UTC)} for c, dep, j in EMPS])
    docs = [{"emp_code": c, "date": d.isoformat(), "status": s,
             "punch_in": at(d, *i) if i else None, "punch_out": at(d, *o) if o else None,
             "work_hours": (4.0 if h else 9.0) if i else None, "late_minutes": lt, "overtime_minutes": ot,
             "half_day": h, "history": []} for c, d, s, i, o, h, lt, ot in ROWS]
    m.logs.insert_many(docs)
    m.logs.insert_one({"emp_code": "GHOST1", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 99,
                       "punch_in": at(date(2026, 7, 6), 11, 0), "work_hours": None})  # unknown employee
    yield
    m.employees.delete_many({})
    m.logs.delete_many({})

@pytest.fixture(scope="module")
def c():
    yield TestClient(m.app)


def test_monthly(c):
    j = c.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    # 23 weekdays; present = 1 (Mon) + 0.5 (half-day Tue); Saturday record excluded from present_days
    assert (j["working_days"], j["present_days"], j["late_count"], j["total_late_minutes"],
            j["total_overtime_minutes"], j["attendance_pct"]) == (23, 1.5, 2, 50, 60, 6.52)
    j = c.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-07"}).json()
    assert (j["working_days"], j["present_days"]) == (15, 1.0)                       # counted from joined_on
    assert c.get("/analytics/employees/EMP0004/monthly", params={"month": "2026-07"}).json()["present_days"] == 0
    j = c.get("/analytics/employees/EMP0006/monthly", params={"month": "2026-07"}).json()
    assert j["working_days"] == 0 and j["attendance_pct"] is None
    assert c.get("/analytics/employees/NOPE/monthly", params={"month": "2026-07"}).status_code == 404


def test_department_summary(c):
    items = {i["department"]: i for i in c.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]}
    eng = items["Eng"]
    assert eng["headcount"] == 4                      # EMP0004 (no logs) counted; all joined by 31 Jul
    assert eng["present_days"] == 1 + 0.5 + 1 + 1 + 1  # E1 Mon+half, E3 Mon + on-duty, E2 one day
    assert eng["leave_count"] == 1 and eng["on_duty_count"] == 1
    assert eng["late_count"] == 3 and eng["total_late_minutes"] == 80
    # mean over present records with work_hours: 9, 4, 9(Sat), 9, 9, 9  -> 49/6 = 8.1667 (not avg of avgs)
    assert eng["avg_work_hours"] == 8.17
    assert items["Ops"]["headcount"] == 1             # EMP0006 joins in August -> excluded
    assert [i["department"] for i in c.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]] == sorted(items)
    assert c.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Ops"}).json()["items"][0]["department"] == "Ops"
    assert c.get("/analytics/departments/summary", params={"month": "2026-01", "department": "Ops"}).json()["items"] == []


def test_leaderboard_ties_and_limit(c):
    j = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()["items"]
    # totals: EMP0001=50, EMP0003=30, EMP0005=15 (GHOST1 ignored). Ranks 1,2,3 -> limit 2 keeps 2 rows.
    assert [(r["rank"], r["emp_code"]) for r in j] == [(1, "EMP0001"), (2, "EMP0003")]
    m.logs.update_one({"emp_code": "EMP0001", "date": "2026-07-11"}, {"$set": {"late_minutes": 0}})
    try:
        j = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1}).json()["items"]
        # now EMP0001=30 ties EMP0003=30 at rank 1: both returned although limit=1; next rank would be 3
        assert [(r["rank"], r["emp_code"]) for r in j] == [(1, "EMP0001"), (1, "EMP0003")]
        j = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 3}).json()["items"]
        assert [r["rank"] for r in j] == [1, 1, 3]
        j = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "Ops"}).json()["items"]
        assert [(r["rank"], r["emp_code"]) for r in j] == [(1, "EMP0005")]
    finally:
        m.logs.update_one({"emp_code": "EMP0001", "date": "2026-07-11"}, {"$set": {"late_minutes": 20}})


def _ref_trend(dept, a, b):
    emps = [(c_, date.fromisoformat(j)) for c_, d_, j in EMPS if d_ == dept]
    rows, rates = [], []
    for n in range((b - a).days + 1):
        d = a + timedelta(days=n)
        recs = [r for r in ROWS if r[0] in {e[0] for e in emps} and r[1] == d]
        p2 = sum((1 if r[5] else 2) for r in recs if r[2] in m.PRESENCE)
        h = sum(1 for _, j in emps if j <= d)
        rate = Fraction(p2, 2 * h) if d.weekday() < 5 and h else None
        rates.append(rate)
        win = [x for x in rates[-7:] if x is not None]
        rows.append({"date": d.isoformat(), "is_working_day": d.weekday() < 5, "headcount": h, "present_count": p2 / 2,
                     "late_count": sum(1 for r in recs if r[6] > 0),
                     "attendance_rate": None if rate is None else m.round_half_up(rate, 4),
                     "moving_avg_7d": m.round_half_up(sum(win) / len(win), 4) if win else None})
    return rows


@pytest.mark.parametrize("dept,a,b", [("Eng", date(2026, 7, 1), date(2026, 7, 31)),
                                       ("Eng", date(2026, 7, 12), date(2026, 7, 16)),
                                       ("Ops", date(2026, 8, 10), date(2026, 8, 20)),     # no logs, joiner mid-range
                                       ("Ops", date(2026, 7, 6), date(2026, 7, 6))])
def test_trend_matches_reference(c, dept, a, b):
    got = c.get(f"/analytics/departments/{dept}/trend", params={"from": a.isoformat(), "to": b.isoformat()}).json()["items"]
    assert got == _ref_trend(dept, a, b)
    assert len(got) == (b - a).days + 1


def test_trend_errors(c):
    p = {"from": "2026-07-01", "to": "2026-07-31"}
    assert c.get("/analytics/departments/Nope/trend", params=p).status_code == 404
    assert c.get("/analytics/departments/Eng/trend", params={"from": "2026-07-31", "to": "2026-07-01"}).status_code == 422
    assert c.get("/analytics/departments/Eng/trend", params={"from": "2026-01-01", "to": "2026-04-03"}).status_code == 422  # 93 days
    assert c.get("/analytics/departments/Eng/trend", params={"from": "2026-01-01", "to": "2026-04-02"}).status_code == 200  # 92 days


@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", {}), ("attendance_list", {"emp_code": "EMP0001"}),
    ("attendance_list", {"status": "LEAVE", "date_from": "2026-07-01"}),
    ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31"}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}), ("department_summary", {"month": "2026-07", "department": "Eng"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("department_trend", {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"})])
def test_explain_uses_index_and_no_collscan(c, endpoint, params):
    r = c.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200
    s = str(r.json()["explain"])
    assert "IXSCAN" in s and "COLLSCAN" not in s


def test_explain_missing_params(c):
    assert c.get("/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code == 422
    assert c.get("/admin/explain/department_trend", params={"department": "Eng"}).status_code == 422
    assert c.get("/admin/explain/nope").status_code == 422

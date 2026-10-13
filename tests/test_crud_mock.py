"""CRUD / validation / concurrency-logic smoke tests against mongomock (no real MongoDB needed).
mongomock cannot run $setWindowFields/$densify, so analytics endpoints are covered by
tests/test_integration.py, which needs a real MongoDB 6+ (set MONGO_URI)."""
import mongomock
import pytest
from fastapi.testclient import TestClient

from app import main as m

BASE = 1783312500000  # 2026-07-06 04:35:00 UTC = 10:05 IST


@pytest.fixture()
def client(monkeypatch):
    mc = mongomock.MongoClient(tz_aware=True)
    d = mc["t"]
    monkeypatch.setattr(m, "db", d)
    monkeypatch.setattr(m, "employees", d["employees"])
    monkeypatch.setattr(m, "logs", d["attendance_logs"])
    m.employees.create_index("emp_code", unique=True)
    m.logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    return TestClient(m.app)


def emp(c, code="EMP0001", **kw):
    body = {"emp_code": code, "name": "A", "email": "a@x.com", "department": "Eng", "joined_on": "2026-01-05", **kw}
    return c.post("/employees", json=body)


def test_employee_create_validation_and_dupe(client):
    r = emp(client)
    assert r.status_code == 201 and isinstance(r.json()["created_at"], int) and "_id" not in r.json()
    assert emp(client).status_code == 409
    assert emp(client, "BAD1").status_code == 422
    assert emp(client, "EMP0002", shift_start="09:00", shift_end="09:00").status_code == 422
    assert emp(client, "EMP0003", joined_on="2026-02-30").status_code == 422
    assert emp(client, "EMP0004", shift_start="25:00").status_code == 422


def test_employee_pagination(client):
    for i in range(1, 6):
        emp(client, f"EMP000{i}", department="A" if i < 4 else "B")
    r = client.get("/employees", params={"page": 2, "page_size": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0003", "EMP0004"] and r["total"] == 5
    assert client.get("/employees", params={"department": "B"}).json()["total"] == 2
    assert client.get("/employees", params={"page": 0}).status_code == 422
    assert client.get("/employees", params={"page_size": 101}).status_code == 422


def test_punch_in_out_flow(client):
    emp(client)
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": BASE + 900})
    assert r.status_code == 201
    j = r.json()
    assert j["date"] == "2026-07-06" and j["late_minutes"] == 35 and j["punch_in"] == BASE  # truncated to second
    assert j["history"] == [] and j["work_hours"] is None and "id" not in j
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": BASE}).status_code == 409
    out = BASE + (9 * 3600 + 5 * 60) * 1000  # 19:10 IST
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out})
    assert r.status_code == 200
    j = r.json()
    assert j["work_hours"] == 9.08 and j["overtime_minutes"] == 40 and j["half_day"] is False
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out}).status_code == 409


def test_punch_errors(client):
    assert client.post("/attendance/punch-in", json={"emp_code": "NOPE"}).status_code == 404
    emp(client)
    for bad in (BASE // 1000, float(BASE), str(BASE), 99_999_999_999, 4_102_444_800_001, True):
        assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad}).status_code == 422, bad
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": BASE}).status_code == 404
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": BASE})
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": BASE}).status_code == 422
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": BASE + 25 * 3600_000}).status_code == 422


def test_overnight_punch_across_midnight(client):
    emp(client, "EMP0005", shift_start="22:00", shift_end="06:00")
    pin = BASE + (11 * 3600 + 50 * 60) * 1000  # 2026-07-06 21:55 IST
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": pin})
    assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 0
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": pin + (8 * 3600 + 45 * 60) * 1000})
    assert r.status_code == 200 and r.json()["work_hours"] == 8.75 and r.json()["overtime_minutes"] == 40


def test_list_attendance_filters_and_order(client):
    emp(client)
    emp(client, "EMP0002")
    for code in ("EMP0002", "EMP0001"):
        for day in range(6, 9):
            client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": BASE + (day - 6) * 86400_000})
    r = client.get("/attendance", params={"page_size": 100}).json()
    assert r["total"] == 6
    assert [(i["date"], i["emp_code"]) for i in r["items"]][:2] == [("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0002")]
    assert client.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-07"}).json()["total"] == 2
    assert client.get("/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-07"}).status_code == 422
    assert client.get("/attendance", params={"date_from": "07/07/2026"}).status_code == 422
    assert client.get("/attendance", params={"status": "NOPE"}).status_code == 422
    assert client.get("/attendance", params={"emp_code": "EMP0001", "page": 2, "page_size": 2}).json()["total"] == 3


def test_regularize(client):
    emp(client)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": BASE})  # 10:05 -> late 35
    p = "/attendance/EMP0001/2026-07-06"
    base = {"reason": "biometric glitch", "regularized_by": "hr"}
    new_in = BASE - 37 * 60_000  # 09:28
    r = client.patch(p, json={**base, "punch_in": new_in})
    assert r.status_code == 200
    j = r.json()
    assert j["late_minutes"] == 0 and len(j["history"]) == 1
    assert j["history"][0]["changes"] == {"punch_in": {"from": BASE, "to": new_in}, "late_minutes": {"from": 35, "to": 0}}
    # add a punch_out -> derived fields recomputed, history appended (never overwritten)
    r = client.patch(p, json={**base, "punch_out": new_in + 9 * 3600_000 + 40 * 60_000})
    j = r.json()
    assert len(j["history"]) == 2 and j["work_hours"] == 9.67
    assert set(j["history"][1]["changes"]) == {"punch_out", "work_hours", "overtime_minutes"}
    # no-op and rule violations
    assert client.patch(p, json={**base, "punch_out": new_in + 9 * 3600_000 + 40 * 60_000}).status_code == 422
    assert client.patch(p, json={**base, "status": "LEAVE", "punch_in": new_in}).status_code == 422
    assert client.patch(p, json={**base, "punch_in": BASE + 86400_000}).status_code == 422       # other day
    assert client.patch(p, json={**base, "punch_out": new_in - 1000}).status_code == 422
    assert client.patch(p, json={**base, "punch_out": None}).status_code == 422
    assert client.patch(p, json={"reason": "abc", "regularized_by": "hr", "status": "WFH"}).status_code == 422
    assert client.patch(p, json={**base, "work_hours": 1.0, "status": "WFH"}).status_code == 200  # derived ignored
    j = client.patch(p, json={**base, "status": "ABSENT"}).json()
    assert j["punch_in"] is None and j["punch_out"] is None and j["work_hours"] is None and j["half_day"] is False
    assert set(j["history"][-1]["changes"]) >= {"status", "punch_in", "punch_out", "work_hours"}
    # ABSENT -> PRESENT needs a punch_in
    assert client.patch(p, json={**base, "status": "PRESENT"}).status_code == 422
    assert client.patch(p, json={**base, "status": "PRESENT", "punch_in": new_in}).status_code == 200
    assert client.patch("/attendance/EMP0001/2026-07-09", json={**base, "status": "LEAVE"}).status_code == 404
    assert client.patch("/attendance/NOPE/2026-07-06", json={**base, "status": "LEAVE"}).status_code == 404
    assert client.patch("/attendance/EMP0001/2026-13-40", json={**base, "status": "LEAVE"}).status_code == 422


def test_regularize_legacy_record_without_history(client):
    emp(client)
    from datetime import datetime, timezone
    m.logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT",
                       "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=timezone.utc),
                       "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=timezone.utc),
                       "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    r = client.patch("/attendance/EMP0001/2026-07-14", json={"reason": "correction", "regularized_by": "hr", "status": "WFH"})
    assert r.status_code == 200 and len(r.json()["history"]) == 1 and r.json()["half_day"] is False
    assert list(r.json()["history"][0]["changes"]) == ["status"]

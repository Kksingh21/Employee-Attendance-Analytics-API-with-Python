"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (real environment variables win over a local .env)

Layout of this single file
  1. configuration, database handle, index creation
  2. time + rounding helpers and the business rules R1-R5 (pure functions)
  3. request models (validation) and response serialisers
  4. employees / attendance endpoints
  5. aggregation pipeline builders + analytics endpoints
  6. /admin/explain, which reuses exactly the same builders as the real endpoints
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, StrictStr, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False: real environment variables take priority over .env

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration, database, indexes
# --------------------------------------------------------------------------- #
# tz_aware=True: PyMongo hands back aware UTC datetimes, never naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: re-creating an identical index is a no-op."""
    global _indexes_ready
    # employees
    employees.create_index([("emp_code", 1)], unique=True, name="uq_emp_code")  # identity, 409 on duplicates
    employees.create_index([("department", 1), ("emp_code", 1)], name="dept_emp_code")  # list + dept lookups
    employees.create_index([("department", 1), ("joined_on", 1)], name="dept_joined_on")  # headcount (R9)
    employees.create_index([("joined_on", 1)], name="joined_on")  # headcount without a department filter
    # attendance_logs
    logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uq_emp_date")  # one record/day, race guard
    logs.create_index([("date", -1), ("emp_code", 1)], name="date_emp")  # default list order, month scans
    logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_emp")  # status filter
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # keep serving /health (503) instead of crashing
        log.error("could not create indexes at startup: %s", exc)
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_unavailable(_, exc: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Helpers: time, rounding, business rules
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MS_MIN, MS_MAX = 100_000_000_000, 4_102_444_800_000

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
NON_PRESENCE = ("ABSENT", "LEAVE")
Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_BELOW = Decimal("4.50")
MAX_SHIFT_SPAN = timedelta(hours=24)

DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


def now_utc() -> datetime:
    """Current instant, truncated to whole milliseconds (BSON precision)."""
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


def from_ms(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime truncated to whole seconds (R1)."""
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: a naive value coming out of the DB is UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def bad_request(loc: tuple, msg: str) -> RequestValidationError:
    """A 422 with FastAPI's standard body shape."""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


def parse_date(value: str, loc: tuple) -> date:
    if not DATE_RE.match(value):
        raise bad_request(loc, "date must be YYYY-MM-DD")
    try:
        return date(int(value[:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        raise bad_request(loc, "not a real calendar date") from None


def month_bounds(month: str, loc: tuple = ("query", "month")) -> tuple[date, date]:
    if not re.match(MONTH_PATTERN, month):
        raise bad_request(loc, "month must be YYYY-MM")
    y, m = int(month[:4]), int(month[5:7])
    try:
        return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    except ValueError:
        raise bad_request(loc, "not a real calendar month") from None


def weekdays_between(start: date, end: date) -> int:
    """Mon-Fri days in [start, end] inclusive (R7). No holiday calendar."""
    n, d = 0, start
    while d <= end:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def round_half_up(x: Any, places: int) -> float:
    """R8. Exact half-up. Floats are first snapped to 10 dp so binary noise
    (8.0149999999999) cannot turn a true tie (8.015) the wrong way."""
    if isinstance(x, float):
        d = Decimal(repr(x)).quantize(Decimal("1e-10"))
    elif isinstance(x, Fraction):
        d = Decimal(x.numerator) / Decimal(x.denominator)
    else:
        d = Decimal(x)
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _secs(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 3600 + int(m) * 60


def is_overnight(emp: dict) -> bool:
    return _secs(emp["shift_end"]) <= _secs(emp["shift_start"])


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: IST calendar date of the punch-in; an overnight-shift punch earlier
    than shift_end (IST time of day) belongs to the previous day's shift."""
    ist = punch_in.astimezone(IST)
    d = ist.date()
    if is_overnight(emp):
        if ist.hour * 3600 + ist.minute * 60 + ist.second < _secs(emp["shift_end"]):
            d -= timedelta(days=1)
    return d


def shift_bounds(d: date, emp: dict) -> tuple[datetime, datetime]:
    """Absolute start/end instants of the shift that belongs to attendance date d."""
    def at(hhmm: str) -> datetime:
        h, m = hhmm.split(":")
        return datetime.combine(d, time(int(h), int(m)), tzinfo=IST)
    start, end = at(emp["shift_start"]), at(emp["shift_end"])
    if is_overnight(emp):
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """R2: late only when strictly more than 10:00 after start; then floor minutes since start."""
    secs = int((punch_in - shift_start).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    """R3: floor minutes after shift end, only when >= 30."""
    minutes = int((punch_out - shift_end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: seconds / 3600 rounded half-up to 2 dp (Decimal - no float / banker's rounding)."""
    secs = int((punch_out - punch_in).total_seconds())
    return (Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def derive(emp: dict, d: date, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All derived fields for a presence record (R2-R5)."""
    start, end = shift_bounds(d, emp)
    out = {"late_minutes": compute_late_minutes(punch_in, start),
           "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out.update(work_hours=float(wh), overtime_minutes=compute_overtime(punch_out, end),
                   half_day=wh < HALF_DAY_BELOW)  # R5 uses the ROUNDED value
    return out


# --------------------------------------------------------------------------- #
# 3. Models and serialisers
# --------------------------------------------------------------------------- #
EpochMs = Annotated[StrictInt, Field(ge=MS_MIN, le=MS_MAX)]  # strict: no floats, strings, bools


class EmployeeIn(BaseModel):
    emp_code: StrictStr = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: StrictStr = Field(min_length=1, max_length=100)
    email: StrictStr = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: StrictStr = Field(min_length=1, max_length=50)
    shift_start: StrictStr = Field("09:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    shift_end: StrictStr = Field("18:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    joined_on: StrictStr

    @model_validator(mode="after")
    def _check(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            parse_date(self.joined_on, ("body", "joined_on"))
        except RequestValidationError:
            raise ValueError("joined_on must be a real YYYY-MM-DD date") from None
        return self


class PunchInIn(BaseModel):
    emp_code: StrictStr = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: StrictStr = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # Defaults of None are not validated, so an explicit `null` is still rejected (422).
    status: Status = None  # type: ignore[assignment]
    punch_in: EpochMs = None  # type: ignore[assignment]
    punch_out: EpochMs = None  # type: ignore[assignment]
    reason: StrictStr = Field(min_length=5, max_length=200)
    regularized_by: StrictStr = Field(min_length=1, max_length=50)


def employee_out(e: dict) -> dict:
    return {"emp_code": e["emp_code"], "name": e["name"], "email": e["email"],
            "department": e["department"], "shift_start": e["shift_start"],
            "shift_end": e["shift_end"], "joined_on": e["joined_on"],
            "created_at": to_ms(e.get("created_at"))}


_TIME_FIELDS = ("punch_in", "punch_out")


def record_out(r: dict) -> dict:
    """Stored document -> API shape. Fills legacy gaps; never leaks _id."""
    history = []
    for h in r.get("history") or []:
        changes = {}
        for field, ch in (h.get("changes") or {}).items():
            conv = to_ms if field in _TIME_FIELDS else (lambda v: v)
            changes[field] = {"from": conv(ch.get("from")), "to": conv(ch.get("to"))}
        history.append({"at": to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"),
                        "changes": changes})
    return {"emp_code": r["emp_code"], "date": r["date"], "status": r["status"],
            "punch_in": to_ms(r.get("punch_in")), "punch_out": to_ms(r.get("punch_out")),
            "work_hours": r.get("work_hours"),
            "late_minutes": r.get("late_minutes") or 0,
            "overtime_minutes": r.get("overtime_minutes") or 0,
            "half_day": bool(r.get("half_day")),
            "history": history}


def require_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 4. System, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)  # the unique index is the only duplicate guard
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return employee_out(doc)


PageQ = Annotated[int, Query(ge=1)]
PageSizeQ = Annotated[int, Query(ge=1, le=100)]
MonthQ = Annotated[str, Query(pattern=MONTH_PATTERN)]


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageQ = 1, page_size: PageSizeQ = 20):
    q = {} if department is None else {"department": department}
    total = employees.count_documents(q)
    cur = employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(e) for e in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = require_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc().replace(microsecond=0)
    d = attendance_date(ts, emp)
    start, _ = shift_bounds(d, emp)
    doc = {"emp_code": body.emp_code, "date": d.isoformat(), "status": body.status,
           "punch_in": ts, "punch_out": None, "work_hours": None,
           "late_minutes": compute_late_minutes(ts, start),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        logs.insert_one(doc)  # race-safe: unique (emp_code, date); the loser gets DuplicateKeyError
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date") from None
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = require_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc().replace(microsecond=0)
    # Most recent record whose punch_in <= ts. Range on a date-typed field never matches the
    # null punch_in of ABSENT/LEAVE rows. Served by the (emp_code, date) index, newest first.
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("date", -1)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = rec["punch_in"]
    if ts <= pin or ts - pin > MAX_SHIFT_SPAN:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")
    fields = derive(emp, parse_date(rec["date"], ("db", "date")), pin, ts)
    fields["punch_out"] = ts
    # Compare-and-set: only the request that still sees an open record wins -> exactly one 200.
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": pin},
        {"$set": fields}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def build_attendance_query(emp_code, date_from, date_to, status, page, page_size):
    """Shared by GET /attendance and the explain endpoint so both run the identical query."""
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    lo = parse_date(date_from, ("query", "date_from")) if date_from is not None else None
    hi = parse_date(date_to, ("query", "date_to")) if date_to is not None else None
    if lo and hi and lo > hi:
        raise bad_request(("query", "date_from"), "date_from must not be after date_to")
    if lo or hi:
        q["date"] = {**({"$gte": date_from} if lo else {}), **({"$lte": date_to} if hi else {})}
    if status is not None:
        q["status"] = status
    sort = [("date", -1), ("emp_code", 1)]
    return q, sort, (page - 1) * page_size, page_size


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[Status] = None,
                    page: PageQ = 1, page_size: PageSizeQ = 20):
    q, sort, skip, limit = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
    total = logs.count_documents(q)
    items = [record_out(r) for r in logs.find(q, {"_id": 0}).sort(sort).skip(skip).limit(limit)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


REGULARIZE_RETRIES = 8


def plan_regularization(rec: dict, emp: dict, body: RegularizeIn) -> tuple[dict, dict]:
    """Validate a correction against the current record; return ($set fields, history `changes`)."""
    cur_status, cur_in, cur_out = rec["status"], rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or cur_status
    req_in = from_ms(body.punch_in) if body.punch_in is not None else None
    req_out = from_ms(body.punch_out) if body.punch_out is not None else None

    if new_status in NON_PRESENCE:
        if req_in is not None or req_out is not None:
            raise HTTPException(422, f"{new_status} clears punch times; do not supply them")
        new_in = new_out = None
    else:
        new_in = req_in or cur_in
        new_out = req_out or cur_out
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if req_in is not None and attendance_date(req_in, emp).isoformat() != rec["date"]:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if (req_in or req_out) and new_out is not None and not (new_in < new_out <= new_in + MAX_SHIFT_SPAN):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if (new_status, new_in, new_out) == (cur_status, cur_in, cur_out):
        raise HTTPException(422, "request changes nothing")

    if new_status in NON_PRESENCE:
        derived = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    else:
        derived = derive(emp, parse_date(rec["date"], ("db", "date")), new_in, new_out)
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
    old = {"status": cur_status, "punch_in": cur_in, "punch_out": cur_out,
           "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
           "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day"))}
    changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
    return new, changes


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, ("path", "date"))
    emp = require_employee(emp_code)
    for _ in range(REGULARIZE_RETRIES):
        rec = logs.find_one({"emp_code": emp_code, "date": date}, {"_id": 0})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")
        new, changes = plan_regularization(rec, emp, body)
        entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        # Optimistic concurrency: the write only lands if the record is exactly as we read it
        # (same history length and same status/times). A concurrent edit makes this a no-match,
        # and we re-read and re-plan instead of overwriting the other editor's history entry.
        hist = rec.get("history")
        guard = {"emp_code": emp_code, "date": date, "status": rec["status"],
                 "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
                 "history": None if hist is None else {"$size": len(hist)}}
        update = {"$set": new, "$push": {"history": entry}} if hist is not None \
            else {"$set": {**new, "history": [entry]}}
        updated = logs.find_one_and_update(guard, update, return_document=ReturnDocument.AFTER)
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "concurrent modification, please retry")


# --------------------------------------------------------------------------- #
# 5. Analytics: pipeline builders (shared with /admin/explain) + endpoints
#    Expression fragments take a field prefix: "$" for a document, "$$x." inside $map/$reduce.
# --------------------------------------------------------------------------- #
def _present(p): return {"$in": [p + "status", PRESENCE]}
def _is_weekday(p):
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": p + "date", "format": "%Y-%m-%d"}}}, 5]}
def _late_min(p): return {"$ifNull": [p + "late_minutes", 0]}
def _is_late(p): return {"$gt": [_late_min(p), 0]}
def _half(p): return {"$eq": [{"$ifNull": [p + "half_day", False]}, True]}


def _present_x2(p):
    """Presence in half-days: 2 per full day, 1 per half day, 0 if not present (any weekday)."""
    return {"$cond": [_present(p), {"$cond": [_half(p), 1, 2]}, 0]}


def _present_days_x2(p):
    """Same, but only Mon-Fri records count (R7)."""
    return {"$cond": [_is_weekday(p), _present_x2(p), 0]}


def _reduce_sum(array, expr):
    return {"$reduce": {"input": array, "initialValue": 0, "in": {"$add": ["$$value", expr]}}}


_X = "$$this."  # variable used inside $reduce


def monthly_pipeline(emp_code: str, first: date, last: date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_x2": {"$sum": _present_days_x2("$")},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [_is_late("$"), 1, 0]}},
            "total_late_minutes": {"$sum": _late_min("$")},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def summary_pipeline(first: date, last: date, department: Optional[str]) -> list:
    """employees (headcount, R9) -> $lookup that employee's month of logs -> per-employee totals
    -> $group by department. Starting from employees is what keeps zero-log people counted."""
    match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    wh_ok = {"$and": [_present(_X), {"$isNumber": _X + "work_hours"}]}
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1, "work_hours": 1}},
            ],
            "as": "logs"}},
        {"$set": {
            "present_x2": _reduce_sum("$logs", _present_days_x2(_X)),
            "late_count": _reduce_sum("$logs", {"$cond": [_is_late(_X), 1, 0]}),
            "total_late_minutes": _reduce_sum("$logs", _late_min(_X)),
            "leave_count": _reduce_sum("$logs", {"$cond": [{"$eq": [_X + "status", "LEAVE"]}, 1, 0]}),
            "on_duty_count": _reduce_sum("$logs", {"$cond": [{"$eq": [_X + "status", "ON_DUTY"]}, 1, 0]}),
            # work hours as integer hundredths: exact sums, exact half-up average afterwards
            "wh_cents": _reduce_sum("$logs", {"$cond": [wh_ok, {"$round": [{"$multiply": [_X + "work_hours", 100]}, 0]}, 0]}),
            "wh_n": _reduce_sum("$logs", {"$cond": [wh_ok, 1, 0]}),
        }},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
                    **{k: {"$sum": "$" + k} for k in ("present_x2", "late_count", "total_late_minutes",
                                                      "leave_count", "on_duty_count", "wh_cents", "wh_n")}}},
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(first: date, last: date, department: Optional[str], limit: int) -> list:
    stages: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped here
    ]
    if department is not None:
        stages.append({"$match": {"emp.department": department}})  # rank inside the department only
    stages += [
        # $rank = standard competition ranking (1, 2, 2, 4). limit is applied on the rank, not the position.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return stages


def trend_pipeline(department: str, frm: date, to: date) -> list:
    """One pass over the department: every employee contributes a 'join' event (headcount +1 on
    the day they count) and one event per log. Group by day, $densify to one row per calendar day,
    running-sum the joins into headcount, then a 7-row moving average of the rate."""
    f, t = frm.isoformat(), to.isoformat()
    day = lambda d: datetime(d.year, d.month, d.day, tzinfo=UTC)
    join_day = {"$cond": [{"$lt": ["$joined_on", f]}, f, {"$cond": [{"$gt": ["$joined_on", t]}, t, "$joined_on"]}]}
    X = "$$x."
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": f, "$lte": t}}},
                         {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}}],
            "as": "logs"}},
        {"$project": {"_id": 0, "ev": {"$concatArrays": [
            [{"d": join_day, "h": {"$cond": [{"$lte": ["$joined_on", t]}, 1, 0]}, "p": 0, "l": 0}],
            {"$map": {"input": "$logs", "as": "x",
                      "in": {"d": X + "date", "h": 0, "p": _present_x2(X), "l": {"$cond": [_is_late(X), 1, 0]}}}},
        ]}}},
        {"$unwind": "$ev"},
        {"$group": {"_id": "$ev.d", "h": {"$sum": "$ev.h"}, "p": {"$sum": "$ev.p"}, "l": {"$sum": "$ev.l"}}},
        {"$set": {"dt": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$sort": {"dt": 1}},
        {"$densify": {"field": "dt", "range": {"step": 1, "unit": "day", "bounds": [day(frm), day(to) + timedelta(days=1)]}}},
        {"$set": {"h": {"$ifNull": ["$h", 0]}, "p": {"$ifNull": ["$p", 0]}, "l": {"$ifNull": ["$l", 0]}}},
        {"$setWindowFields": {"sortBy": {"dt": 1},
                              "output": {"headcount": {"$sum": "$h", "window": {"documents": ["unbounded", "current"]}}}}},
        {"$set": {"working": {"$lte": [{"$isoDayOfWeek": "$dt"}, 5]}}},
        {"$set": {"rate": {"$cond": [{"$and": ["$working", {"$gt": ["$headcount", 0]}]},
                                     {"$divide": ["$p", {"$multiply": [2, "$headcount"]}]}, None]}}},
        {"$setWindowFields": {"sortBy": {"dt": 1},
                              "output": {"ma": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"dt": 1}},
        {"$project": {"_id": 0, "date": {"$dateToString": {"date": "$dt", "format": "%Y-%m-%d"}},
                      "working": 1, "headcount": 1, "p": 1, "l": 1, "rate": 1, "ma": 1}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQ):
    first, last = month_bounds(month)
    emp = require_employee(emp_code)
    row = next(iter(logs.aggregate(monthly_pipeline(emp_code, first, last))), None) or {}
    px2 = int(row.get("present_x2", 0))
    working = weekdays_between(max(first, parse_date(emp["joined_on"], ("db", "joined_on"))), last)
    return {"emp_code": emp_code, "month": month, "working_days": working,
            "present_days": round_half_up(Fraction(px2, 2), 2),
            "leave_days": int(row.get("leave_days", 0)),
            "late_count": int(row.get("late_count", 0)),
            "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
            "attendance_pct": round_half_up(Fraction(px2 * 100, 2 * working), 2) if working else None}


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQ, department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for r in employees.aggregate(summary_pipeline(first, last, department)):
        n = int(r["wh_n"])
        items.append({"department": r["_id"], "headcount": int(r["headcount"]),
                      "present_days": round_half_up(Fraction(int(r["present_x2"]), 2), 2),
                      "avg_work_hours": round_half_up(Fraction(int(r["wh_cents"]), 100 * n), 2) if n else None,
                      "late_count": int(r["late_count"]),
                      "total_late_minutes": int(r["total_late_minutes"]),
                      "leave_count": int(r["leave_count"]), "on_duty_count": int(r["on_duty_count"])})
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthQ, limit: Annotated[int, Query(ge=1, le=50)] = 10,
                     department: Optional[str] = None):
    first, last = month_bounds(month)
    items = list(logs.aggregate(leaderboard_pipeline(first, last, department, limit)))
    return {"month": month, "items": items}


def _trend_range(frm: str, to: str) -> tuple[date, date]:
    a, b = parse_date(frm, ("query", "from")), parse_date(to, ("query", "to"))
    if b < a:
        raise bad_request(("query", "to"), "`to` must not be before `from`")
    if (b - a).days + 1 > 92:
        raise bad_request(("query", "to"), "range must not exceed 92 days")
    return a, b


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, frm: Annotated[str, Query(alias="from")], to: str):
    a, b = _trend_range(frm, to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = []
    for r in employees.aggregate(trend_pipeline(department, a, b)):
        h, p = int(r["headcount"]), int(r["p"])
        items.append({"date": r["date"], "is_working_day": bool(r["working"]), "headcount": h,
                      "present_count": p / 2, "late_count": int(r["l"]),
                      "attendance_rate": None if r["rate"] is None else round_half_up(Fraction(p, 2 * h), 4),
                      "moving_avg_7d": None if r["ma"] is None else round_half_up(float(r["ma"]), 4)})
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 6. Explain: same builders as the endpoints above, run under explain/executionStats
# --------------------------------------------------------------------------- #
ExplainName = Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"]


def _need(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise bad_request(("query", missing[0]), f"{', '.join(missing)} required for this endpoint")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainName,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    frm: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    if endpoint == "attendance_list":
        q, sort, skip, lim = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
        coll, cmd = "attendance_logs", {"find": "attendance_logs", "filter": q, "sort": dict(sort),
                                        "skip": skip, "limit": lim}
    else:
        if endpoint == "department_trend":
            _need(department=department, **{"from": frm}, to=to)
            a, b = _trend_range(frm, to)
            coll, pipeline = "employees", trend_pipeline(department, a, b)
        elif endpoint == "employee_monthly":
            _need(emp_code=emp_code, month=month)
            first, last = month_bounds(month)
            coll, pipeline = "attendance_logs", monthly_pipeline(emp_code, first, last)
        elif endpoint == "department_summary":
            _need(month=month)
            first, last = month_bounds(month)
            coll, pipeline = "employees", summary_pipeline(first, last, department)
        else:  # late_leaderboard
            _need(month=month)
            first, last = month_bounds(month)
            coll, pipeline = "attendance_logs", leaderboard_pipeline(first, last, department, limit)
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
    raw = db.command({"explain": cmd, "verbosity": "executionStats"})
    explain = json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": coll, "explain": explain}

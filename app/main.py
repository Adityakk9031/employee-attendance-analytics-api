"""
Employee Attendance & Analytics API (FastAPI + MongoDB).

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Conventions used throughout this file
  * Instants are BSON datetimes in MongoDB (UTC, tz-aware client) and integer epoch milliseconds in the API.
  * Calendar dates / months / shift times are plain strings (IST), exactly as stored.
  * Derived fields (work_hours, late_minutes, overtime_minutes, half_day) are written only by this API and
    read - never recomputed - by analytics.
  * Every rounding is done with integer arithmetic so "half-up" is exact (no float / banker's rounding).
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False: variables already in the environment take priority

log = logging.getLogger("attendance")

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,  # datetimes come back as aware UTC, never naive
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_CENTS = 450  # R5 (4.50 hours, in hundredths)
MAX_SHIFT_SECONDS = 24 * 3600  # punch-out / correction window
MAX_TREND_DAYS = 92

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    specs = [
        # unique: emp_code is the business key (race-safe create) and serves GET /employees sorted by emp_code
        (db.employees, [("emp_code", ASCENDING)], {"unique": True}),
        # department filter + emp_code sort (list), department-scoped pipelines (summary, trend)
        (db.employees, [("department", ASCENDING), ("emp_code", ASCENDING)], {}),
        # headcount per day: department + joined_on <= day
        (db.employees, [("department", ASCENDING), ("joined_on", ASCENDING)], {}),
        # headcount without a department filter: joined_on <= end of month
        (db.employees, [("joined_on", ASCENDING)], {}),
        # unique: one record per (emp_code, date) - makes punch-in race-safe; serves punch-out, PATCH,
        # employee monthly, and every per-employee $lookup into the logs
        (db.attendance_logs, [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True}),
        # GET /attendance default order (date desc, emp_code asc) and month-wide scans (leaderboard)
        (db.attendance_logs, [("date", DESCENDING), ("emp_code", ASCENDING)], {}),
        # status filter + the same sort order
        (db.attendance_logs, [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {}),
    ]
    ok = True
    for coll, keys, opts in specs:
        try:
            coll.create_index(keys, **opts)
        except PyMongoError as exc:  # e.g. Mongo not reachable yet; /health retries later
            ok = False
            log.warning("index %s on %s not created: %s", keys, coll.name, exc)
    _indexes_ready = ok


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    log.error("MongoDB error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# Small helpers: errors, time conversion, rounding
# --------------------------------------------------------------------------- #
def bad(loc: tuple, msg: str) -> RequestValidationError:
    """A 422 with FastAPI's default body shape."""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


def to_ms(dt: Any) -> Optional[int]:
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return int(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def half_up_div(num: int, den: int) -> int:
    """round-half-up(num / den) for non-negative integers, exactly."""
    return (2 * num + den) // (2 * den)


def parse_date(value: str, loc: tuple) -> date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise bad(loc, "date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise bad(loc, "not a valid calendar date")


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        raise bad(("query", "month"), "year out of range")
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


def hhmm(value: str) -> time:
    return time(int(value[:2]), int(value[3:5]))


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]


# --------------------------------------------------------------------------- #
# Business rules R1 - R5
# --------------------------------------------------------------------------- #
def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: the IST calendar date of the punch-in; overnight shifts started the day before if the
    punch-in is earlier than shift_end."""
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(emp) and ist.time() < hhmm(emp["shift_end"]):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(date_str: str, emp: dict) -> tuple[datetime, datetime]:
    """Shift start / end instants for the shift that belongs to attendance date `date_str`."""
    day = date.fromisoformat(date_str)
    start = datetime.combine(day, hhmm(emp["shift_start"]), tzinfo=IST)
    end_day = day + timedelta(days=1) if is_overnight(emp) else day
    end = datetime.combine(end_day, hhmm(emp["shift_end"]), tzinfo=IST)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then whole minutes since shift start."""
    seconds = int((punch_in - shift_start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_cents(punch_in: datetime, punch_out: datetime) -> int:
    """R4 in hundredths of an hour, rounded half-up from whole seconds."""
    seconds = int((punch_out - punch_in).total_seconds())
    return half_up_div(seconds * 100, 3600)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return compute_work_cents(punch_in, punch_out) / 100


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    """R3: whole minutes after shift end, only when at least 30."""
    seconds = int((punch_out - shift_end).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derive(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], emp: dict, date_str: str) -> dict:
    """All derived fields for a record in its final state."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return out
    start, end = shift_bounds(date_str, emp)
    out["late_minutes"] = compute_late_minutes(punch_in, start)
    if punch_out is not None:
        cents = compute_work_cents(punch_in, punch_out)
        out["work_hours"] = cents / 100
        out["overtime_minutes"] = compute_overtime(punch_out, end)
        out["half_day"] = cents < HALF_DAY_CENTS
    return out


def working_days(start: date, end: date, joined_on: date) -> int:
    """R7: Mon-Fri between max(start, joined_on) and end, inclusive."""
    day, count = max(start, joined_on), 0
    while day <= end:
        count += day.weekday() < 5
        day += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Serialisation (Mongo document -> API shape)
# --------------------------------------------------------------------------- #
def ser_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc.get("created_at")),
    }


def ser_history(entry: dict) -> dict:
    changes = {}
    for key, change in (entry.get("changes") or {}).items():
        if key in ("punch_in", "punch_out"):
            change = {"from": to_ms(change.get("from")), "to": to_ms(change.get("to"))}
        changes[key] = change
    return {"at": to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def ser_record(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [ser_history(h) for h in (doc.get("history") or [])],
    }


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date.fromisoformat(v)  # raises ValueError for 2026-02-30 etc.
        return v

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
MonthParam = Annotated[str, Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]


# --------------------------------------------------------------------------- #
# System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    ensure_indexes()  # retries if the startup attempt happened before Mongo was up
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the unique index decides concurrent duplicates
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return ser_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [ser_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(body.status, ts, None, emp, day),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index: exactly one concurrent winner
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return ser_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("date", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    seconds = int((ts - rec["punch_in"]).total_seconds())
    if seconds <= 0:
        raise bad(("body", "punched_at"), "punched_at must be after punch_in")
    if seconds > MAX_SHIFT_SECONDS:
        raise bad(("body", "punched_at"), "punched_at is more than 24 hours after punch_in")
    fields = {"punch_out": ts, **derive(rec["status"], rec["punch_in"], ts, emp, rec["date"])}
    # the punch_out: None filter makes the close atomic: of two simultaneous requests only one matches
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None}, {"$set": fields}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return ser_record(updated)


def _date_param(value: Optional[str], name: str) -> Optional[str]:
    return parse_date(value, ("query", name)).isoformat() if value is not None else None


def attendance_query(emp_code, date_from, date_to, status, page, page_size) -> dict:
    """The one place GET /attendance (and its explain) builds its find()."""
    lo, hi = _date_param(date_from, "date_from"), _date_param(date_to, "date_to")
    if lo and hi and lo > hi:
        raise bad(("query", "date_from"), "date_from must not be after date_to")
    q: dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if lo or hi:
        q["date"] = {**({"$gte": lo} if lo else {}), **({"$lte": hi} if hi else {})}
    if status is not None:
        q["status"] = status
    return {
        "filter": q,
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    spec = attendance_query(emp_code, date_from, date_to, status, page, page_size)
    coll = db.attendance_logs
    total = coll.count_documents(spec["filter"])
    cursor = (
        coll.find(spec["filter"], {"_id": 0})
        .sort(list(spec["sort"].items()))
        .skip(spec["skip"])
        .limit(spec["limit"])
    )
    return {"items": [ser_record(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


def plan_correction(rec: dict, emp: dict, body: RegularizeIn) -> dict:
    """Validate a correction and work out the new field values + the history 'changes'."""
    new_status = body.status or rec["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise bad(("body", "punch_in"), f"{new_status} records cannot carry punch times")
        pin = pout = None
    else:
        pin = from_ms(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        pout = from_ms(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if pin is None:
            raise bad(("body", "punch_in"), f"status {new_status} requires a punch_in")
        if body.punch_in is not None and attendance_date(pin, emp) != rec["date"]:
            raise bad(("body", "punch_in"), "punch_in must stay on the record's attendance date")
        if pout is not None:
            seconds = int((pout - pin).total_seconds())
            if seconds <= 0:
                raise bad(("body", "punch_out"), "punch_out must be after punch_in")
            if seconds > MAX_SHIFT_SECONDS:
                raise bad(("body", "punch_out"), "punch_out must be within 24 hours of punch_in")

    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    new = {"status": new_status, "punch_in": pin, "punch_out": pout, **derive(new_status, pin, pout, emp, rec["date"])}
    if all(to_ms(old[k]) == to_ms(new[k]) if k.startswith("punch") else old[k] == new[k] for k in ("status", "punch_in", "punch_out")):
        raise bad(("body",), "the request changes nothing")
    changes = {}
    for key, before in old.items():
        after = new[key]
        same = to_ms(before) == to_ms(after) if key.startswith("punch") else before == after
        if not same:
            changes[key] = {"from": before, "to": after}
    return {"new": new, "changes": changes}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date(date, ("path", "date")).isoformat()
    emp = get_employee(emp_code)
    for _ in range(5):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")
        plan = plan_correction(rec, emp, body)
        history = rec.get("history") or []
        entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": plan["changes"]}
        # Optimistic concurrency: only write if the record is still exactly as we read it
        # (same editable fields, and still `len(history)` entries). A concurrent edit makes this
        # match nothing and we re-read and try again, so no history entry is ever overwritten.
        guard = {
            "_id": rec["_id"],
            "status": rec["status"],
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            f"history.{len(history)}": {"$exists": False},
        }
        result = db.attendance_logs.update_one(guard, {"$set": plan["new"], "$push": {"history": entry}})
        if result.matched_count == 1:
            return ser_record({**rec, **plan["new"], "history": history + [entry]})
    raise HTTPException(409, "record is being modified concurrently, retry")


# --------------------------------------------------------------------------- #
# Analytics - pipeline builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def e_weekday(date_expr: str) -> dict:
    """True when the 'YYYY-MM-DD' string is Mon-Fri ($dayOfWeek: 1 = Sunday ... 7 = Saturday)."""
    day = {"$dateFromString": {"dateString": date_expr, "format": "%Y-%m-%d", "onError": None, "onNull": None}}
    return {"$in": [{"$dayOfWeek": day}, [2, 3, 4, 5, 6]]}


def e_present2(status: str, half_day: str, date_expr: Optional[str] = None) -> dict:
    """Presence in half-day units: 2 = full day, 1 = half day, 0 = not present (optionally Mon-Fri only)."""
    cond: dict = {"$in": [status, PRESENCE]}
    if date_expr is not None:
        cond = {"$and": [cond, e_weekday(date_expr)]}
    is_half = {"$eq": [{"$ifNull": [half_day, False]}, True]}
    return {"$cond": [cond, {"$cond": [is_half, 1, 2]}, 0]}


def e_late(late_minutes: str) -> dict:
    return {"$gt": [{"$ifNull": [late_minutes, 0]}, 0]}


def monthly_pipeline(emp_code: str, start: date, end: date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "present2": {"$sum": e_present2("$status", "$half_day", "$date")},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [e_late("$late_minutes"), 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def summary_pipeline(start: date, end: date, department: Optional[str]) -> list:
    """Employees first (so people with zero logs still count), logs joined per employee through the
    (emp_code, date) index, then folded per department."""
    match: dict[str, Any] = {"joined_on": {"$lte": end.isoformat()}}  # R9
    if department is not None:
        match["department"] = department

    def over_logs(expr: dict) -> dict:
        return {"$sum": {"$map": {"input": "$logs", "as": "l", "in": expr}}}

    is_present = {"$in": ["$$l.status", PRESENCE]}
    has_hours = {"$and": [is_present, {"$isNumber": "$$l.work_hours"}]}
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
                    {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1, "work_hours": 1}},
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "p2": over_logs(e_present2("$$l.status", "$$l.half_day", "$$l.date")),
                "late_count": over_logs({"$cond": [e_late("$$l.late_minutes"), 1, 0]}),
                "late_minutes": over_logs({"$cond": [e_late("$$l.late_minutes"), "$$l.late_minutes", 0]}),
                "leave_count": over_logs({"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}),
                "on_duty_count": over_logs({"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}),
                "hours_n": over_logs({"$cond": [has_hours, 1, 0]}),
                # work_hours in hundredths, so the average can be rounded half-up with integers
                "hours_cents": over_logs(
                    {"$cond": [has_hours, {"$round": [{"$multiply": ["$$l.work_hours", 100]}, 0]}, 0]}
                ),
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "p2": {"$sum": "$p2"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "hours_n": {"$sum": "$hours_n"},
                "hours_cents": {"$sum": "$hours_cents"},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {"$divide": ["$p2", 2]},
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$hours_n", 0]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$divide": [
                                            {"$add": [{"$multiply": [2, "$hours_cents"]}, "$hours_n"]},
                                            {"$multiply": [2, "$hours_n"]},
                                        ]
                                    }
                                },
                                100,
                            ]
                        },
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]


def leaderboard_pipeline(start: date, end: date, limit: int, department: Optional[str]) -> list:
    emp_match: dict[str, Any] = {"department": department} if department is not None else {}
    return [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "pipeline": [{"$match": emp_match}, {"$project": {"_id": 0, "name": 1, "department": 1}}],
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},  # drops logs of unknown employees / other departments
        # $rank is standard competition ranking: ties share a rank, the next rank is skipped
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]


def trend_pipeline(department: str, start: date, end: date) -> list:
    start_s, end_s = start.isoformat(), end.isoformat()
    window = {"documents": [-6, 0]}
    day_start = datetime.combine(start, time(0), tzinfo=UTC)
    day_after_end = datetime.combine(end + timedelta(days=1), time(0), tzinfo=UTC)  # $densify upper bound is exclusive
    return [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start_s, "$lte": end_s}}},
                    {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
                ],
                "as": "logs",
            }
        },
        # keep employees without logs: guarantees at least one document so $densify has something to fill
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": {"$ifNull": ["$logs.date", start_s]},
                "p2": {"$sum": e_present2("$logs.status", "$logs.half_day")},
                "late": {"$sum": {"$cond": [e_late("$logs.late_minutes"), 1, 0]}},
            }
        },
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$sort": {"d": 1}},
        # exactly one row per calendar day: fill the days that have no records at all
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [day_start, day_after_end]}}},
        {
            "$set": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                "p2": {"$ifNull": ["$p2", 0]},
                "late": {"$ifNull": ["$late", 0]},
                "is_working_day": {"$in": [{"$dayOfWeek": "$d"}, [2, 3, 4, 5, 6]]},
            }
        },
        {
            "$lookup": {  # R9: employees of the department who had joined by that day
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {
            "$set": {  # attendance_rate x 10000, rounded half-up with exact integer maths; null otherwise
                "rate_int": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$floor": {
                                "$divide": [
                                    {"$add": [{"$multiply": [10000, "$p2"]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]},
                                ]
                            }
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"d": 1},
                "output": {
                    "rate_sum": {"$sum": "$rate_int", "window": window},
                    "rate_n": {"$sum": {"$cond": [{"$isNumber": "$rate_int"}, 1, 0]}, "window": window},
                },
            }
        },
        {"$sort": {"d": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {"$divide": ["$p2", 2]},
                "late_count": "$late",
                "attendance_rate": {"$cond": [{"$isNumber": "$rate_int"}, {"$divide": ["$rate_int", 10000]}, None]},
                "moving_avg_7d": {
                    "$cond": [
                        {"$gt": ["$rate_n", 0]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$divide": [
                                            {"$add": [{"$multiply": [2, "$rate_sum"]}, "$rate_n"]},
                                            {"$multiply": [2, "$rate_n"]},
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
    ]


def trend_range(frm: str, to: str) -> tuple[date, date]:
    start, end = parse_date(frm, ("query", "from")), parse_date(to, ("query", "to"))
    if end < start:
        raise bad(("query", "to"), "to must not be before from")
    if (end - start).days + 1 > MAX_TREND_DAYS:
        raise bad(("query", "to"), f"range must not exceed {MAX_TREND_DAYS} days")
    return start, end


# --------------------------------------------------------------------------- #
# Analytics - endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthParam):
    start, end = month_bounds(month)
    emp = get_employee(emp_code)
    row = next(db.attendance_logs.aggregate(monthly_pipeline(emp_code, start, end)), None) or {}
    days = working_days(start, end, date.fromisoformat(emp["joined_on"]))
    present2 = int(row.get("present2", 0))
    pct = half_up_div(10000 * present2, 2 * days) / 100 if days else None  # present/working x 100, 2 dp
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": present2 / 2,
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthParam, department: Optional[str] = None):
    start, end = month_bounds(month)
    items = list(db.employees.aggregate(summary_pipeline(start, end, department)))
    for item in items:
        item["present_days"] = float(item["present_days"])
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthParam, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    start, end = month_bounds(month)
    return {"month": month, "items": list(db.attendance_logs.aggregate(leaderboard_pipeline(start, end, limit, department)))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from")],
    to: Annotated[str, Query()],
):
    start, end = trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "department not found")
    return {"department": department, "items": list(db.employees.aggregate(trend_pipeline(department, start, end)))}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def _required(**params: Any) -> None:
    for name, value in params.items():
        if value is None:
            raise bad(("query", name), "Field required")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        spec = attendance_query(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command: dict[str, Any] = {"find": collection, "filter": spec["filter"], "sort": spec["sort"], "limit": spec["limit"]}
        if spec["skip"]:
            command["skip"] = spec["skip"]
    else:
        if endpoint == "employee_monthly":
            _required(emp_code=emp_code, month=month)
            collection, pipeline = "attendance_logs", monthly_pipeline(emp_code, *month_bounds(month))
        elif endpoint == "department_summary":
            _required(month=month)
            collection, pipeline = "employees", summary_pipeline(*month_bounds(month), department)
        elif endpoint == "late_leaderboard":
            _required(month=month)
            collection, pipeline = "attendance_logs", leaderboard_pipeline(*month_bounds(month), limit, department)
        else:
            _required(department=department, **{"from": from_, "to": to})
            collection, pipeline = "employees", trend_pipeline(department, *trend_range(from_, to))
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS)),
    }

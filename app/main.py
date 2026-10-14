"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded; real environment variables win)

Status of this file:
  done : helpers, indexes, /health, /employees (POST, GET), punch-in, punch-out, /attendance (GET)
  done : PATCH regularize
  done : 4 analytics endpoints (aggregation pipelines)
  done : /admin/explain/{endpoint}
"""
import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date as date_cls, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

load_dotenv()  # load_dotenv() does NOT override real environment variables

# tz_aware=True -> datetimes read from Mongo carry UTC tzinfo (no naive datetimes)
# serverSelectionTimeoutMS: fail fast (5 s) instead of the 30 s default when MongoDB is unreachable
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True,
                     serverSelectionTimeoutMS=5000)
db = client[os.getenv("MONGO_DB", "attendance_db")]

logger = logging.getLogger("attendance")
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000   # contract: EpochMillis range
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
MIN_YEAR, MAX_YEAR = 1970, 2100                  # same span as EpochMillis; keeps date arithmetic in range
PCT_DECIMALS = 2                                 # R8: reported numbers 2 decimals ("rates" such as attendance_rate: 4)
PRESENT = ["PRESENT", "WFH", "ON_DUTY"]          # R6
WEEKDAYS = [2, 3, 4, 5, 6]                       # MongoDB $dayOfWeek: 1=Sunday ... 7=Saturday


_indexes_ready = False


def ensure_indexes() -> bool:
    """Create all indexes (create_index is idempotent). Returns False if MongoDB is not reachable yet."""
    global _indexes_ready
    if _indexes_ready:
        return True
    try:
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
        # analytics: department summary without a department filter selects employees by joined_on
        db.employees.create_index([("joined_on", ASCENDING)])
        # one record per (emp_code, date): this unique index is what makes punch-in race-safe
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        # list sorted by date desc, emp_code asc
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        # punch-out: "latest record of this employee with punch_in <= punched_at"
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
        # GET /attendance?status=...: the filter, its count and the sort all use this one index
        db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)])
    except PyMongoError as exc:
        logger.warning("could not create indexes yet (%s); /health keeps retrying", exc)
        return False
    _indexes_ready = True
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Best effort: if MongoDB is still starting, the app must stay up so /health can answer 503 and retry
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(ConnectionFailure)
async def database_unavailable(request: Request, exc: ConnectionFailure):
    # MongoDB unreachable -> a clean 503 instead of a 500 traceback
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """epoch ms -> UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Mongo datetime -> epoch ms for the API."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def hhmm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date_cls:
    """R1: IST calendar date of punch-in; overnight early punch-ins belong to the previous day."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        d -= timedelta(days=1)
    return d


def compute_late_minutes(punch_in: datetime, shift_start: str, att_date: date_cls) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since shift start."""
    h, m = hhmm(shift_start)
    start = datetime(att_date.year, att_date.month, att_date.day, h, m, tzinfo=IST)
    secs = int((punch_in - start).total_seconds())
    return secs // 60 if secs > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up (Decimal, not round())."""
    secs = int((punch_out - punch_in).total_seconds())
    hours = Decimal(secs) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, att_date: date_cls) -> int:
    """R3: floor minutes after shift end, only if >= 30. Overnight shifts end on the next day."""
    h, m = hhmm(shift_end)
    end_date = att_date + timedelta(days=1) if shift_end <= shift_start else att_date
    end = datetime(end_date.year, end_date.month, end_date.day, h, m, tzinfo=IST)
    mins = int((punch_out - end).total_seconds()) // 60
    return mins if mins >= 30 else 0


def unprocessable(message: str) -> HTTPException:
    """422 with the same body shape FastAPI/Pydantic use for validation errors (detail = list of objects),
    as the contract's ValidationError response requires. Other errors (404, 409, 503) keep a plain-string detail."""
    return HTTPException(422, detail=[{"loc": [], "msg": message, "type": "value_error"}])


def parse_date_or_422(value: str, name: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise unprocessable(f"{name} must be a valid YYYY-MM-DD date")
    return value


def _ms_if_dt(v):
    return dt_to_ms(v) if isinstance(v, datetime) else v


def history_out(entries) -> list:
    out = []
    for e in entries or []:
        changes = {
            k: {"from": _ms_if_dt(v.get("from")), "to": _ms_if_dt(v.get("to"))}
            for k, v in (e.get("changes") or {}).items()
        }
        out.append({"at": dt_to_ms(e.get("at")), "by": e.get("by"),
                    "reason": e.get("reason"), "changes": changes})
    return out


def attendance_out(doc: dict) -> dict:
    """Mongo document -> API shape. No _id; missing fields get their defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_out(doc.get("history")),
    }


def employee_out(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    doc["created_at"] = dt_to_ms(doc.get("created_at"))
    return doc


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=DATE_RE)       # exactly YYYY-MM-DD (strptime alone accepts 2026-1-5)

    @model_validator(mode="after")
    def check(self):
        try:
            datetime.strptime(self.joined_on, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    # StrictInt: floats and strings are rejected (422); range check rejects "seconds"-looking values
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_MS, le=MAX_MS)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_MS, le=MAX_MS)


class RegularizeIn(BaseModel):
    """Only these fields can change. Derived fields sent by a client are ignored (extra fields are dropped)."""
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=MIN_MS, le=MAX_MS)
    punch_out: Optional[StrictInt] = Field(default=None, ge=MIN_MS, le=MAX_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def no_explicit_null(self):
        # "omitted" means unchanged; an explicit null is not a valid value for these fields
        for f in ("status", "punch_in", "punch_out"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} must not be null (omit it to leave it unchanged)")
        return self


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    if not ensure_indexes():                      # MongoDB came up after the app did: build the indexes now
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)          # unique index decides, not a prior find_one
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    cur = (db.employees.find(q, {"_id": 0})
           .sort("emp_code", ASCENDING)
           .skip((page - 1) * page_size)
           .limit(page_size))
    return {"items": [employee_out(d) for d in cur], "total": total,
            "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    att_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], att_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)    # race-safe: unique (emp_code, date) index
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    po = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # most recent record whose punch_in is at or before punched_at (works across midnight,
    # because we search by punch_in time, not by calendar date). ABSENT/LEAVE have no punch_in,
    # so a $lte comparison never matches them.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": po}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    pin = rec["punch_in"]
    if pin.tzinfo is None:
        pin = pin.replace(tzinfo=timezone.utc)
    if po <= pin:
        raise unprocessable("punched_at must be after punch_in")
    if po - pin > timedelta(hours=24):
        raise unprocessable("punched_at is more than 24 hours after punch_in")

    att_date = date_cls.fromisoformat(rec["date"])
    work_hours = compute_work_hours(pin, po)
    updates = {
        "punch_out": po,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(po, emp["shift_start"], emp["shift_end"], att_date),
        "half_day": work_hours < 4.5,
    }
    # Atomic: only succeeds if the record is STILL open (punch_out is null). Two simultaneous
    # punch-outs -> one matches, the other matches nothing -> 409.
    res = db.attendance_logs.update_one(
        {"emp_code": rec["emp_code"], "date": rec["date"], "punch_out": None, "punch_in": rec["punch_in"]},
        {"$set": updates},
    )
    if res.matched_count == 0:
        raise HTTPException(409, "already punched out")
    rec.update(updates)
    return attendance_out(rec)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _same(a, b) -> bool:
    return _ms_if_dt(a) == _ms_if_dt(b)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(body: RegularizeIn, emp_code: str, date: str = Path(pattern=DATE_RE)):
    parse_date_or_422(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for this date")

    old_status = rec["status"]
    old_in, old_out = _aware(rec.get("punch_in")), _aware(rec.get("punch_out"))
    new_status = body.status or old_status
    att_date = date_cls.fromisoformat(date)

    if new_status in ("ABSENT", "LEAVE"):
        # these records have no punch times: the status clears them, so supplying one is a contradiction
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable(f"{new_status} records cannot have punch_in or punch_out")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise unprocessable("a presence status requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != att_date:
            raise unprocessable("punch_in must stay on the record's attendance date")
        if new_out is not None:
            if new_out <= new_in:
                raise unprocessable("punch_out must be after punch_in")
            if new_out - new_in > timedelta(hours=24):
                raise unprocessable("punch_out must be within 24 hours of punch_in")

    # recompute every derived field from the final state (R2-R5)
    if new_in is None:
        late, work_hours, overtime, half_day = 0, None, 0, False
    else:
        late = compute_late_minutes(new_in, emp["shift_start"], att_date)
        if new_out is None:
            work_hours, overtime, half_day = None, 0, False
        else:
            work_hours = compute_work_hours(new_in, new_out)
            overtime = compute_overtime(new_out, emp["shift_start"], emp["shift_end"], att_date)
            half_day = work_hours < 4.5

    new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
                "work_hours": work_hours, "late_minutes": late,
                "overtime_minutes": overtime, "half_day": half_day}
    old_vals = {"status": old_status, "punch_in": old_in, "punch_out": old_out,
                "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes", 0),
                "overtime_minutes": rec.get("overtime_minutes", 0), "half_day": rec.get("half_day", False)}
    changes = {k: {"from": old_vals[k], "to": v} for k, v in new_vals.items() if not _same(old_vals[k], v)}
    if not changes:
        raise unprocessable("request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: write only if the record is still exactly as we read it (same status,
    # punch times and number of history entries). A concurrent PATCH, punch-out or any other change
    # makes this match nothing -> 409, so nobody's history entry is silently lost.
    flt = {"emp_code": emp_code, "date": date, "status": old_status,
           "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out")}
    hist = rec.get("history") or []
    if hist:
        flt["history"] = {"$size": len(hist)}
    else:
        flt["$or"] = [{"history": {"$exists": False}}, {"history": {"$size": 0}}]
    res = db.attendance_logs.update_one(flt, {"$set": new_vals, "$push": {"history": entry}})
    if res.matched_count == 0:
        raise HTTPException(409, "record was modified concurrently, retry")
    return attendance_out({**rec, **new_vals, "history": hist + [entry]})


def attendance_list_query(emp_code, date_from, date_to, status, page, page_size):
    """Validate the filters and build (filter, sort, skip, limit). Used by GET /attendance AND by
    /admin/explain, so the explained query is exactly the query the endpoint runs."""
    if date_from:
        parse_date_or_422(date_from, "date_from")
    if date_to:
        parse_date_or_422(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise unprocessable("date_from must be <= date_to")

    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    sort = [("date", DESCENDING), ("emp_code", ASCENDING)]
    return q, sort, (page - 1) * page_size, page_size


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q, sort, skip, limit = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
    # sort + skip + limit happen in the database, using the index; nothing is loaded into memory
    total = db.attendance_logs.count_documents(q)     # exact (estimated_document_count is only metadata)
    cur = db.attendance_logs.find(q).sort(sort).skip(skip).limit(limit)
    return {"items": [attendance_out(d) for d in cur], "total": total,
            "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Analytics (all numbers come from MongoDB aggregation pipelines)
# --------------------------------------------------------------------------- #
def month_range(month: str):
    """'2026-07' -> (first day, last day, first day of next month) as date objects."""
    y, m = int(month[:4]), int(month[5:7])
    if not MIN_YEAR <= y <= MAX_YEAR:             # 0000-01 / 9999-12 would overflow date arithmetic -> 422, not 500
        raise unprocessable(f"month year must be between {MIN_YEAR} and {MAX_YEAR}")
    first = date_cls(y, m, 1)
    last = date_cls(y, m, calendar.monthrange(y, m)[1])
    return first, last, last + timedelta(days=1)


def half_up(value, places: int) -> float:
    """R8: half-up rounding done on a Decimal (MongoDB's $round and Python's round() are half-even)."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def working_days(joined_on: str, month: str) -> int:
    """R7: Mon-Fri in the month, counted from joined_on for a mid-month joiner (calendar arithmetic only)."""
    first, last, _ = month_range(month)
    start = max(first, date_cls.fromisoformat(joined_on))
    return sum(1 for i in range((last - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


def _is_weekday(date_field: str) -> dict:
    # attendance dates are 'YYYY-MM-DD' strings; $dateFromString reads them as UTC midnight, so the
    # weekday is the calendar weekday
    return {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": date_field}}}, WEEKDAYS]}


def _present_weight(prefix: str) -> dict:
    """1 for a present-status record on Mon-Fri, 0.5 if it is a half day, otherwise 0 (R5-R7)."""
    return {"$cond": [
        {"$and": [{"$in": [f"${prefix}status", PRESENT]}, _is_weekday(f"${prefix}date")]},
        {"$cond": [{"$eq": [f"${prefix}half_day", True]}, 0.5, 1]},
        0,
    ]}


def employee_monthly_pipeline(emp_code: str, month: str):
    first, _, nxt = month_range(month)
    return "attendance_logs", [
        # served by the (emp_code, date) index
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lt": nxt.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _present_weight("")},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(month: str, department: Optional[str]):
    first, last, nxt = month_range(month)
    emp_match: dict = {"joined_on": {"$lte": last.isoformat()}}          # R9
    if department is not None:
        emp_match["department"] = department
    return "employees", [
        {"$match": emp_match},
        # each employee's logs for the month, found through the (emp_code, date) index
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"c": "$emp_code"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$c"]},
                {"$gte": ["$date", first.isoformat()]},
                {"$lt": ["$date", nxt.isoformat()]},
            ]}}}],
            "as": "log",
        }},
        # keep employees without logs: they still count in headcount
        {"$unwind": {"path": "$log", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": _present_weight("log.")},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$log.late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$log.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$log.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$log.status", "ON_DUTY"]}, 1, 0]}},
            # average over RECORDS: keep sum (in cents, exact integers) and count, divide at the end
            "wh_cents": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$log.status", PRESENT]}, {"$isNumber": "$log.work_hours"}]},
                {"$round": [{"$multiply": ["$log.work_hours", 100]}, 0]}, 0]}},
            "wh_n": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$log.status", PRESENT]}, {"$isNumber": "$log.work_hours"}]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]):
    first, _, nxt = month_range(month)
    pipe: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lt": nxt.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},                       # logs whose employee does not exist are dropped
    ]
    if department is not None:
        pipe.append({"$match": {"emp.department": department}})   # rank inside the department only
    pipe += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},     # limit AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return "attendance_logs", pipe


def department_trend_pipeline(department: str, d_from: date_cls, d_to: date_cls):
    n_days = (d_to - d_from).days + 1
    start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    window = {"documents": [-6, 0]}                 # current row + up to 6 preceding rows, inside the range
    return "employees", [
        {"$match": {"department": department}},     # (department, emp_code) index
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joined": {"$push": "$joined_on"}}},
        # gap filling: one row for EVERY calendar day, generated by the database ($range), so empty days exist
        {"$project": {"_id": 0, "codes": 1, "joined": 1, "days": {"$map": {
            "input": {"$range": [0, n_days]}, "as": "i",
            "in": {"$dateToString": {"format": "%Y-%m-%d",
                                     "date": {"$dateAdd": {"startDate": start, "unit": "day", "amount": "$$i"}}}},
        }}}},
        {"$unwind": "$days"},
        # that day's records of this department's employees (index on date)
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$days", "codes": "$codes"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
                {"$group": {"_id": None,
                            "present": {"$sum": {"$cond": [{"$in": ["$status", PRESENT]},
                                                           {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                            "late": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}}},
            ],
            "as": "agg",
        }},
        {"$addFields": {
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$agg.present", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$agg.late", 0]}, 0]},
            "headcount": {"$size": {"$filter": {"input": "$joined", "as": "j", "cond": {"$lte": ["$$j", "$days"]}}}},
            "is_wd": _is_weekday("$days"),
        }},
        # attendance_rate x 10000 as an exact half-up integer: floor((2*P2*5000 + h) / (2h)), P2 = 2*present
        {"$addFields": {"rate_int": {"$cond": [
            {"$and": ["$is_wd", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [
                {"$add": [{"$multiply": [{"$multiply": ["$present_count", 2]}, 10000]}, "$headcount"]},
                {"$multiply": ["$headcount", 2]}]}},
            None]}}},
        {"$addFields": {"has_rate": {"$cond": [{"$eq": ["$rate_int", None]}, 0, 1]}}},
        # moving average over the (rounded) rates: $sum ignores nulls, has_rate counts the non-null ones
        {"$setWindowFields": {"sortBy": {"days": 1}, "output": {
            "s": {"$sum": "$rate_int", "window": window},
            "n": {"$sum": "$has_rate", "window": window}}}},
        {"$project": {
            "_id": 0, "date": "$days", "is_working_day": "$is_wd", "headcount": 1,
            "present_count": 1, "late_count": 1,
            "attendance_rate": {"$cond": [{"$eq": ["$rate_int", None]}, None, {"$divide": ["$rate_int", 10000]}]},
            "moving_avg_7d": {"$cond": [{"$gt": ["$n", 0]}, {"$divide": [
                {"$floor": {"$divide": [{"$add": [{"$multiply": ["$s", 2]}, "$n"]}, {"$multiply": ["$n", 2]}]}},
                10000]}, None]},
        }},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_RE)):
    month_range(month)                            # validate the month before touching the database
    emp = db.employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    coll, pipe = employee_monthly_pipeline(emp_code, month)
    row = next(iter(db[coll].aggregate(pipe)), {})         # no logs at all -> no group row -> zeros
    wd = working_days(emp["joined_on"], month)
    present = row.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": float(present),
        "leave_days": row.get("leave_days", 0),
        "late_count": row.get("late_count", 0),
        "total_late_minutes": row.get("total_late_minutes", 0),
        "total_overtime_minutes": row.get("total_overtime_minutes", 0),
        "attendance_pct": None if wd == 0 else half_up(Decimal(str(present)) / wd * 100, PCT_DECIMALS),
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=MONTH_RE), department: Optional[str] = None):
    month_range(month)
    coll, pipe = department_summary_pipeline(month, department)
    items = []
    for r in db[coll].aggregate(pipe):
        n = r["wh_n"]
        items.append({
            "department": r["_id"],
            "headcount": len(r["codes"]),
            "present_days": float(r["present_days"]),
            "avg_work_hours": half_up(Decimal(r["wh_cents"]) / Decimal(100 * n), 2) if n else None,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(pattern=MONTH_RE), limit: int = Query(10, ge=1, le=50),
                     department: Optional[str] = None):
    month_range(month)
    coll, pipe = late_leaderboard_pipeline(month, limit, department)
    return {"month": month, "items": list(db[coll].aggregate(pipe))}


def trend_range(date_from: str, date_to: str):
    parse_date_or_422(date_from, "from")
    parse_date_or_422(date_to, "to")
    d_from, d_to = date_cls.fromisoformat(date_from), date_cls.fromisoformat(date_to)
    if d_to < d_from:
        raise unprocessable("to must be >= from")
    if (d_to - d_from).days + 1 > 92:
        raise unprocessable("range must not exceed 92 days")
    return d_from, d_to


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str,
                     date_from: str = Query(alias="from", pattern=DATE_RE),
                     date_to: str = Query(alias="to", pattern=DATE_RE)):
    d_from, d_to = trend_range(date_from, date_to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    coll, pipe = department_trend_pipeline(department, d_from, d_to)
    return {"department": department, "items": list(db[coll].aggregate(pipe))}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def _need(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise unprocessable("missing query parameter(s): " + ", ".join(missing))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    trend_from: Optional[str] = Query(None, alias="from", pattern=DATE_RE),
    trend_to: Optional[str] = Query(None, alias="to", pattern=DATE_RE),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """executionStats explain of the endpoint's main query, built by the SAME builder functions the
    endpoint uses (same filters, sort, pagination), so what you see here is what really runs."""
    if month is not None:
        month_range(month)
    if endpoint == "attendance_list":
        q, sort, skip, lim = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
        coll = "attendance_logs"
        cmd = {"find": coll, "filter": q, "sort": dict(sort), "skip": skip, "limit": lim}
    else:
        if endpoint == "employee_monthly":
            _need(emp_code=emp_code, month=month)
            coll, pipe = employee_monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            _need(month=month)
            coll, pipe = department_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            _need(month=month)
            coll, pipe = late_leaderboard_pipeline(month, limit, department)
        else:  # department_trend
            _need(department=department, **{"from": trend_from, "to": trend_to})
            d_from, d_to = trend_range(trend_from, trend_to)
            coll, pipe = department_trend_pipeline(department, d_from, d_to)
        cmd = {"aggregate": coll, "pipeline": pipe, "cursor": {}}

    raw = db.command({"explain": cmd, "verbosity": "executionStats"})
    # the raw explain contains BSON types (dates, Int64...): convert to plain JSON, nothing else changed
    explain = json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": coll, "explain": explain}
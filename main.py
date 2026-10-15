"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pymongo import MongoClient
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

print("ENV FILE EXISTS:", (Path(__file__).resolve().parent.parent / ".env").is_file())

mongo_uri = os.getenv("MONGO_URI", "").strip()
print("MONGO_URI loaded:", bool(mongo_uri))

from urllib.parse import urlparse

parsed_uri = urlparse(mongo_uri)

print("MONGO URI scheme:", parsed_uri.scheme)
print("MONGO URI host:", parsed_uri.hostname)
print("Atlas format:", mongo_uri.startswith("mongodb+srv://"))

client = MongoClient(mongo_uri) if mongo_uri else MongoClient("mongodb://localhost:27017")

db = client[os.getenv("MONGO_DB", "attendance_db")]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.employees.create_index("joined_on")

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
    )
    db.attendance_logs.create_index([("date", -1), ("status", 1)])
    db.attendance_logs.create_index([("status", 1), ("late_minutes", -1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])


@app.on_event("startup")
def startup_event():
    create_indexes()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #



IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def truncate_seconds(value: datetime) -> datetime:
    """Discard fractional seconds, as required by the API contract."""
    return value.replace(microsecond=0)


def to_ist(value: datetime) -> datetime:
    """Convert an instant to IST."""
    if value.tzinfo is None:
        # PyMongo returns naive datetimes representing UTC.
        value = value.replace(tzinfo=UTC)
    return truncate_seconds(value.astimezone(IST))


def to_mongo_datetime(value: datetime) -> datetime:
    """Return a naive UTC datetime for PyMongo storage."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return truncate_seconds(value.astimezone(UTC)).replace(tzinfo=None)


def epoch_ms_to_datetime(value: int) -> datetime:
    """Convert epoch milliseconds to a UTC datetime."""
    return truncate_seconds(
        datetime.fromtimestamp(value / 1000, tz=UTC)
    )


def datetime_to_epoch_ms(value: datetime) -> int:
    """Convert a datetime to epoch milliseconds."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)



def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> int:
    local_in = to_ist(punch_in)
    start_time = time.fromisoformat(shift_start)
    end_time = time.fromisoformat(shift_end)

    shift_date = local_in.date()

    # For an overnight shift, an early-morning punch
    # belongs to the shift that started the previous day.
    if end_time <= start_time and local_in.time() < end_time:
        shift_date -= timedelta(days=1)

    scheduled_start = datetime.combine(
        shift_date, start_time, tzinfo=IST
    )

    elapsed_seconds = (
        local_in - scheduled_start
    ).total_seconds()

    # Ten-minute grace period; lateness must be strictly greater.
    if elapsed_seconds <= 600:
        return 0

    return int(elapsed_seconds // 60)



def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    start = to_ist(punch_in)
    end = to_ist(punch_out)

    seconds = Decimal(str((end - start).total_seconds()))
    hours = (seconds / Decimal("3600")).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return float(hours)


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    attendance_date: str,
    shift_start: str,
) -> int:
    end_time = time.fromisoformat(shift_end)
    start_time = time.fromisoformat(shift_start)
    shift_date = date.fromisoformat(attendance_date)

    if end_time <= start_time:
        shift_date += timedelta(days=1)

    scheduled_end = datetime.combine(
        shift_date, end_time, tzinfo=IST
    )
    actual_out = to_ist(punch_out)

    elapsed_seconds = (
        actual_out - scheduled_end
    ).total_seconds()

    if elapsed_seconds < 30 * 60:
        return 0

    return int(elapsed_seconds // 60)



# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="MongoDB is unavailable",
        )



@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = to_mongo_datetime(datetime.now(UTC))
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc




@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if page < 1:
        raise HTTPException(
            status_code=422,
            detail="page must be at least 1",
        )

    if page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=422,
            detail="page_size must be between 1 and 100",
        )

    query = {}
    if department:
        query["department"] = department

    total = db.employees.count_documents(query)

    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }





@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    allowed_statuses = {"PRESENT", "WFH", "ON_DUTY"}
    if body.status not in allowed_statuses:
        raise HTTPException(
            status_code=422,
            detail="Invalid attendance status",
        )

    try:
        if body.punched_at is not None:
            ts = epoch_ms_to_datetime(body.punched_at)
        else:
            ts = datetime.now(UTC)
    except (ValueError, TypeError, OverflowError, OSError):
        raise HTTPException(
            status_code=422,
            detail="Invalid punched_at timestamp",
        )

    ts = truncate_seconds(ts)
    local_ts = to_ist(ts)

    shift_start = time.fromisoformat(emp["shift_start"])
    shift_end = time.fromisoformat(emp["shift_end"])

    attendance_date = local_ts.date()

    # Early-morning punches for overnight shifts belong
    # to the previous attendance date.
    if shift_end <= shift_start and local_ts.time() < shift_end:
        attendance_date -= timedelta(days=1)

    d = attendance_date.isoformat()
    ts = to_mongo_datetime(ts)

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts, emp["shift_start"], emp["shift_end"]
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        res = db.attendance_logs.insert_one(doc)
    except Exception as exc:
        from pymongo.errors import DuplicateKeyError

        if isinstance(exc, DuplicateKeyError):
            raise HTTPException(
                status_code=409,
                detail="Already punched in for this date",
            )
        raise

    
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc






@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if page < 1:
        raise HTTPException(422, "page must be at least 1")

    if not 1 <= page_size <= 100:
        raise HTTPException(
            422, "page_size must be between 1 and 100"
        )

    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    try:
        for value in (date_from, date_to):
            if value is not None:
                parsed = date.fromisoformat(value)
                if parsed.isoformat() != value:
                    raise ValueError("Invalid date format")
    except ValueError:
        raise HTTPException(
            422, "Dates must use YYYY-MM-DD format"
        )

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            422, "date_from must be on or before date_to"
        )

    if date_from or date_to:
        query["date"] = {}

        if date_from:
            query["date"]["$gte"] = date_from

        if date_to:
            query["date"]["$lte"] = date_to

    if status:
        allowed_statuses = {
            "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
        }

        if status not in allowed_statuses:
            raise HTTPException(422, "Invalid attendance status")

        query["status"] = status

    total = db.attendance_logs.count_documents(query)

    items = list(
        db.attendance_logs.find(query, {"_id": 0})
        .sort("date", -1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }




class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    
class AttendanceCorrectionIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    by: str
    reason: str




@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    try:
        if body.punched_at is not None:
            now = epoch_ms_to_datetime(body.punched_at)
        else:
            now = datetime.now(UTC)
    except (ValueError, TypeError, OverflowError, OSError):
        raise HTTPException(
            status_code=422,
            detail="Invalid punched_at timestamp",
        )

    now = truncate_seconds(now)
    now = to_mongo_datetime(now)

    attendance = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_out": None,
        },
        sort=[("date", -1)],
    )

    if not attendance:
        existing_attendance = db.attendance_logs.find_one(
            {"emp_code": body.emp_code},
            sort=[("date", -1)],
        )

        if existing_attendance is not None:
            if existing_attendance.get("punch_out") is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Attendance was already punched out",
                )

        raise HTTPException(
            status_code=404,
            detail="Attendance record not found",
        )

    punch_in_time = attendance.get("punch_in")

    if not punch_in_time:
        raise HTTPException(
            status_code=409,
            detail="Attendance record has no punch-in time",
        )

    if now <= punch_in_time:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in",
        )

    work_hours = compute_work_hours(punch_in_time, now)

    overtime = compute_overtime(
        now,
        emp["shift_end"],
        attendance["date"],
        emp["shift_start"],
    )

    half_day = work_hours < 4.50

    result = db.attendance_logs.update_one(
        {
            "_id": attendance["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": now,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance was already punched out",
        )

    updated = db.attendance_logs.find_one(
        {"_id": attendance["_id"]},
        {"_id": 0},
    )

    return updated


@app.patch("/attendance/{emp_code}/{attendance_date}")
def correct_attendance(
    emp_code: str,
    attendance_date: str,
    body: AttendanceCorrectionIn,
):
    # Validate the date
    try:
        parsed_date = date.fromisoformat(attendance_date)
        if parsed_date.isoformat() != attendance_date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Date must use YYYY-MM-DD format",
        )

    if not body.by.strip() or not body.reason.strip():
        raise HTTPException(
            status_code=422,
            detail="by and reason are required",
        )

    allowed_statuses = {
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    }

    if body.status is not None and body.status not in allowed_statuses:
        raise HTTPException(
            status_code=422,
            detail="Invalid attendance status",
        )

    if (
        body.status is None
        and body.punch_in is None
        and body.punch_out is None
    ):
        raise HTTPException(
            status_code=422,
            detail="Provide at least one field to correct",
        )

    emp = db.employees.find_one({"emp_code": emp_code})

    if not emp:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    attendance = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": attendance_date,
    })

    if not attendance:
        raise HTTPException(
            status_code=404,
            detail="Attendance record not found",
        )

    updates = {}

    if body.status is not None:
        updates["status"] = body.status

    for field, timestamp in (
        ("punch_in", body.punch_in),
        ("punch_out", body.punch_out),
    ):
        if timestamp is not None:
            try:
                updates[field] = to_mongo_datetime(
                    epoch_ms_to_datetime(timestamp)
                )
            except (ValueError, TypeError, OverflowError, OSError):
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid {field} timestamp",
                )

    punch_in_time = updates.get(
        "punch_in", attendance.get("punch_in")
    )
    punch_out_time = updates.get(
        "punch_out", attendance.get("punch_out")
    )

    if punch_in_time is not None and punch_out_time is not None:
        if punch_out_time <= punch_in_time:
            raise HTTPException(
                status_code=422,
                detail="Punch-out must be after punch-in",
            )

    if punch_in_time is not None:
        updates["late_minutes"] = compute_late_minutes(
            punch_in_time,
            emp["shift_start"],
            emp["shift_end"],
        )

    if punch_out_time is not None and punch_in_time is not None:
        work_hours = compute_work_hours(
            punch_in_time,
            punch_out_time,
        )
        updates["work_hours"] = work_hours
        updates["half_day"] = work_hours < 4.50
        updates["overtime_minutes"] = compute_overtime(
            punch_out_time,
            emp["shift_end"],
            attendance_date,
            emp["shift_start"],
        )
    elif "punch_out" in updates and punch_out_time is None:
        updates["work_hours"] = None
        updates["half_day"] = False
        updates["overtime_minutes"] = 0

    history_entry = {
        "corrected_at": to_mongo_datetime(datetime.now(UTC)),
        "by": body.by.strip(),
        "reason": body.reason.strip(),
        "previous_values": {
            key: attendance.get(key)
            for key in updates
        },
    }

    result = db.attendance_logs.update_one(
        {
            "_id": attendance["_id"],
        },
        {
            "$set": updates,
            "$push": {"history": history_entry},
        },
    )

    if result.matched_count != 1:
        raise HTTPException(
            status_code=404,
            detail="Attendance record not found",
        )

    corrected = db.attendance_logs.find_one(
        {"_id": attendance["_id"]},
        {"_id": 0},
    )

    return corrected

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str,
):
    if not month or len(month) != 7:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    try:
        month_start = date.fromisoformat(month + "-01")
        if month_start.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    if month_start.month == 12:
        next_month = date(month_start.year + 1, 1, 1)
    else:
        next_month = date(
            month_start.year,
            month_start.month + 1,
            1,
        )

    emp = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "emp_code": 1, "joined_on": 1},
    )

    if not emp:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    try:
        joined_on = date.fromisoformat(emp["joined_on"])
    except (ValueError, TypeError, KeyError):
        raise HTTPException(
            status_code=500,
            detail="Employee has an invalid joined_on date",
        )

    effective_start = max(month_start, joined_on)

    if effective_start >= next_month:
        return {
            "emp_code": emp_code,
            "month": month,
            "total_records": 0,
            "present_days": 0,
            "late_days": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
            "average_work_hours": None,
        }

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": effective_start.isoformat(),
                    "$lt": next_month.isoformat(),
                },
            }
        },
        {
            "$addFields": {
                "date_obj": {
                    "$dateFromString": {
                        "dateString": "$date",
                        "format": "%Y-%m-%d",
                    }
                }
            }
        },
        {
            "$addFields": {
                "weekday": {
                    "$dayOfWeek": "$date_obj"
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "total_records": {"$sum": 1},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$in": [
                                            "$weekday",
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_days": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": "$late_minutes"
                },
                "total_overtime_minutes": {
                    "$sum": "$overtime_minutes"
                },
                "average_work_hours": {
                    "$avg": "$work_hours"
                },
            }
        },
    ]

    result = list(db.attendance_logs.aggregate(pipeline))

    summary = result[0] if result else {
        "total_records": 0,
        "present_days": 0,
        "late_days": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
        "average_work_hours": None,
    }

    summary.pop("_id", None)
    summary["emp_code"] = emp_code
    summary["month"] = month

    if summary["average_work_hours"] is not None:
        summary["average_work_hours"] = float(
            Decimal(str(summary["average_work_hours"])).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
        )

    return summary


@app.get("/analytics/departments/summary")
def department_summary(
    date_from: str,
    date_to: str,
):
    try:
        start_date = date.fromisoformat(date_from)
        end_date = date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format",
        )

    if start_date > end_date:
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )

    pipeline = [
        {
            "$match": {
                "joined_on": {"$lte": end_date.isoformat()}
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", start_date.isoformat()]},
                                    {"$lte": ["$date", end_date.isoformat()]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "date_obj": {
                                "$dateFromString": {
                                    "dateString": "$date",
                                    "format": "%Y-%m-%d",
                                }
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "weekday": {"$dayOfWeek": "$date_obj"}
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "records": {"$sum": 1},
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                {
                                                    "$in": [
                                                        "$weekday",
                                                        [2, 3, 4, 5, 6],
                                                    ]
                                                },
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_minutes": {"$sum": "$late_minutes"},
                            "overtime_minutes": {"$sum": "$overtime_minutes"},
                        }
                    },
                ],
                "as": "attendance",
            }
        },
        {
            "$unwind": {
                "path": "$attendance",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "employees_with_records": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$attendance.records", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "present_days": {
                    "$sum": {"$ifNull": ["$attendance.present_days", 0]}
                },
                "late_minutes": {
                    "$sum": {"$ifNull": ["$attendance.late_minutes", 0]}
                },
                "overtime_minutes": {
                    "$sum": {"$ifNull": ["$attendance.overtime_minutes", 0]}
                },
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "employees_with_records": 1,
                "present_days": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
            }
        },
    ]

    return list(db.employees.aggregate(pipeline))





@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    # Validate date format
    try:
        start_date = date.fromisoformat(from_)
        end_date = date.fromisoformat(to)

        if start_date.isoformat() != from_ or end_date.isoformat() != to:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format",
        )

    if end_date < start_date:
        raise HTTPException(
            status_code=422,
            detail="'to' must be on or after 'from'",
        )

    if (end_date - start_date).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    start_str = start_date.isoformat()
    end_str = end_date.isoformat()

    # Verify that this department exists.
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = [
        # Create one document for every calendar day.
        {
            "$documents": [
                {"date": (start_date + timedelta(days=i)).isoformat()}
                for i in range((end_date - start_date).days + 1)
            ]
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "emp_code": 1}},
                ],
                "as": "employees",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "day": "$date",
                    "codes": "$employees.emp_code",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "daily",
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": {
                    "$and": [
                        {
                            "$gte": [
                                {
                                    "$isoDayOfWeek": {
                                        "$dateFromString": {
                                            "dateString": "$date"
                                        }
                                    }
                                },
                                1,
                            ]
                        },
                        {
                            "$lte": [
                                {
                                    "$isoDayOfWeek": {
                                        "$dateFromString": {
                                            "dateString": "$date"
                                        }
                                    }
                                },
                                5,
                            ]
                        },
                    ]
                },
                "headcount": {"$size": "$employees"},
                "present_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$daily.present_count", 0]},
                        0,
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$daily.late_count", 0]},
                        0,
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]},
            }
        },
    ]

    items = list(db.aggregate(pipeline))

    # The pipeline's $documents stage is run against the selected collection.
    return {"department": department, "items": items}



@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    date_from: str,
    date_to: str,
    limit: int = 10,
):
    try:
        start_date = date.fromisoformat(date_from)
        end_date = date.fromisoformat(date_to)

        if (
            start_date.isoformat() != date_from
            or end_date.isoformat() != date_to
        ):
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format",
        )

    if start_date > end_date:
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )

    if not 1 <= limit <= 100:
        raise HTTPException(
            status_code=422,
            detail="limit must be between 1 and 100",
        )

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": date_from,
                    "$lte": date_to,
                },
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": "$late_minutes"
                },
                "late_days": {"$sum": 1},
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {
                    "total_late_minutes": -1
                },
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$limit": limit},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "total_late_minutes": 1,
                "late_days": 1,
                "rank": 1,
            }
        },
    ]

    return list(db.attendance_logs.aggregate(pipeline))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    month: Optional[str] = None,
    limit: int = 10,
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"endpoint must be one of: {', '.join(sorted(allowed))}",
        )

    if endpoint == "attendance_list":
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to

        return db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1},
                "limit": 20,
            },
            "verbosity": "executionStats",
        })

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required",
            )

        try:
            start = date.fromisoformat(month + "-01")
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="month must use YYYY-MM format",
            )

        next_month = (
            date(start.year + 1, 1, 1)
            if start.month == 12
            else date(start.year, start.month + 1, 1)
        )

        query = {
            "emp_code": emp_code,
            "date": {
                "$gte": start.isoformat(),
                "$lt": next_month.isoformat(),
            },
        }

        return db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": query,
            },
            "verbosity": "executionStats",
        })

    if endpoint in ("department_summary", "department_trend"):
        if not date_from or not date_to:
            raise HTTPException(
                status_code=422,
                detail="date_from and date_to are required",
            )

        query = {
            "date": {
                "$gte": date_from,
                "$lte": date_to,
            }
        }

        if endpoint == "department_trend" and department:
            query["department"] = department

        return db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": query,
            },
            "verbosity": "executionStats",
        })

    if endpoint == "late_leaderboard":
        if not date_from or not date_to:
            raise HTTPException(
                status_code=422,
                detail="date_from and date_to are required",
            )

        return db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": {
                    "date": {
                        "$gte": date_from,
                        "$lte": date_to,
                    },
                    "late_minutes": {"$gt": 0},
                },
                "sort": {"late_minutes": -1},
                "limit": limit,
            },
            "verbosity": "executionStats",
        })


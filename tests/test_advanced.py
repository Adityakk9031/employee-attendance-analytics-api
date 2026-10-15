import concurrent.futures
from datetime import date, datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient

from app.main import (
    app,
    db,
    client,
    ensure_indexes,
    to_ms,
    UTC,
    IST,
)

test_client = TestClient(app)

def test_concurrency_punch_in():
    # Two simultaneous punch-ins for the same employee on a new date
    # Clean test data for EMP0001 on 2026-08-03
    db.attendance_logs.delete_many({"emp_code": "EMP0001", "date": "2026-08-03"})
    
    dt = datetime(2026, 8, 3, 9, 30, 0, tzinfo=IST)
    ms = to_ms(dt)
    
    def do_punch():
        # Fresh test client per thread or shared
        c = TestClient(app)
        return c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms})

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch) for _ in range(5)]
        results = [f.result() for f in futures]

    statuses = [r.status_code for r in results]
    # Exactly one 201, rest 409
    assert statuses.count(201) == 1
    assert statuses.count(409) == 4

def test_concurrency_punch_out():
    # Prepare one open record
    db.attendance_logs.delete_many({"emp_code": "EMP0001", "date": "2026-08-04"})
    dt_in = datetime(2026, 8, 4, 9, 30, 0, tzinfo=IST)
    test_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": to_ms(dt_in)})

    dt_out = datetime(2026, 8, 4, 18, 30, 0, tzinfo=IST)
    ms_out = to_ms(dt_out)

    def do_out():
        c = TestClient(app)
        return c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out})

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_out) for _ in range(5)]
        results = [f.result() for f in futures]

    statuses = [r.status_code for r in results]
    # Exactly one 200, rest 409
    assert statuses.count(200) == 1
    assert statuses.count(409) == 4

def test_leaderboard_ties_and_cutoff():
    # Set up 4 employees in Sales for month 2026-09
    # EMP0003: 60 min late
    # EMP0004: 40 min late
    # EMP0007: 40 min late (tie with EMP0004)
    # EMP0008: 20 min late
    db.employees.delete_many({"emp_code": {"$in": ["EMP0007", "EMP0008"]}})
    db.employees.insert_many([
        {
            "emp_code": "EMP0007",
            "name": "Tied Person A",
            "email": "tied.a@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
        {
            "emp_code": "EMP0008",
            "name": "Tied Person B",
            "email": "tied.b@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
    ])
    db.attendance_logs.delete_many({"date": {"$regex": "^2026-09"}})
    db.attendance_logs.insert_many([
        {"emp_code": "EMP0003", "date": "2026-09-01", "status": "PRESENT", "late_minutes": 60},
        {"emp_code": "EMP0004", "date": "2026-09-01", "status": "PRESENT", "late_minutes": 40},
        {"emp_code": "EMP0007", "date": "2026-09-01", "status": "PRESENT", "late_minutes": 40},
        {"emp_code": "EMP0008", "date": "2026-09-01", "status": "PRESENT", "late_minutes": 20},
    ])

    # Rank 1: EMP0003 (60 min)
    # Rank 2: EMP0004 and EMP0007 (40 min each, tie)
    # Rank 4: EMP0008 (20 min) -> rank 3 skipped!
    
    # Query with limit=2:
    # Since rank <= limit, employees with rank 1 and rank 2 are included.
    # Because EMP0004 and EMP0007 tie at rank 2, BOTH must be returned (total 3 items)!
    res = test_client.get("/analytics/leaderboard/late?month=2026-09&limit=2")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3
    assert items[0]["emp_code"] == "EMP0003"
    assert items[0]["rank"] == 1
    assert items[1]["rank"] == 2
    assert items[2]["rank"] == 2
    # Check ordering between tied: emp_code asc
    assert items[1]["emp_code"] == "EMP0004"
    assert items[2]["emp_code"] == "EMP0007"

    # Query with limit=3:
    # rank 4 is > 3, so EMP0008 is still excluded! Total 3 items.
    res3 = test_client.get("/analytics/leaderboard/late?month=2026-09&limit=3")
    assert len(res3.json()["items"]) == 3

    # Query with limit=4:
    # EMP0008 included with rank 4!
    res4 = test_client.get("/analytics/leaderboard/late?month=2026-09&limit=4")
    items4 = res4.json()["items"]
    assert len(items4) == 4
    assert items4[3]["emp_code"] == "EMP0008"
    assert items4[3]["rank"] == 4

def test_weekend_record_presence_and_overtime():
    # R7: Saturday 2026-08-01 record
    db.attendance_logs.delete_many({"emp_code": "EMP0001", "date": {"$regex": "^2026-08"}})
    db.attendance_logs.insert_many([
        # Saturday record with late and overtime
        {
            "emp_code": "EMP0001",
            "date": "2026-08-01", # Saturday
            "status": "PRESENT",
            "punch_in": datetime(2026, 8, 1, 4, 30, tzinfo=UTC),
            "punch_out": datetime(2026, 8, 1, 14, 0, tzinfo=UTC),
            "work_hours": 9.5,
            "late_minutes": 30,
            "overtime_minutes": 60,
            "half_day": False,
        },
        # Monday record (Aug 3)
        {
            "emp_code": "EMP0001",
            "date": "2026-08-03", # Monday
            "status": "PRESENT",
            "punch_in": datetime(2026, 8, 3, 4, 0, tzinfo=UTC),
            "punch_out": datetime(2026, 8, 3, 13, 0, tzinfo=UTC),
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    ])

    res = test_client.get("/analytics/employees/EMP0001/monthly?month=2026-08")
    assert res.status_code == 200
    data = res.json()
    # August 2026 has 21 weekdays (Aug 1 is Sat, Aug 2 is Sun, Aug 3 is Mon... Aug 31 is Mon)
    assert data["working_days"] == 21
    # present_days counts ONLY Mon-Fri records -> exactly 1.0! Saturday is excluded!
    assert data["present_days"] == 1.0
    # BUT late and overtime from Saturday ARE counted!
    assert data["total_late_minutes"] == 30
    assert data["total_overtime_minutes"] == 60
    assert data["late_count"] == 1

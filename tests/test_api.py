from datetime import date, datetime, timedelta
import pytest
from fastapi.testclient import TestClient
from bson import json_util
import pathlib

from app.main import (
    app,
    db,
    ensure_indexes,
    shift_bounds,
    attendance_date,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    working_days,
    IST,
    UTC,
    to_ms,
)

test_client = TestClient(app)

@pytest.fixture(autouse=True)
def seed_sample_data():
    ensure_indexes()
    here = pathlib.Path(__file__).parent.parent / "sample_data"
    opts = json_util.JSONOptions(tz_aware=True)
    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
        db[name].delete_many({})
        db[name].insert_many(docs)

def test_business_rules_unit():
    # R2: late minutes
    emp_day = {"shift_start": "09:30", "shift_end": "18:30"}
    start_dt, end_dt = shift_bounds("2026-07-06", emp_day)
    
    # 09:40:00 -> 0
    t0 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
    assert compute_late_minutes(t0, start_dt) == 0

    # 09:40:01 -> 10
    t1 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
    assert compute_late_minutes(t1, start_dt) == 10

    # 10:15:59 -> 45
    t2 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
    assert compute_late_minutes(t2, start_dt) == 45

    # R3: overtime
    # < 30 min -> 0
    out_29 = end_dt + timedelta(minutes=29, seconds=59)
    assert compute_overtime(out_29, end_dt) == 0

    # 30 min -> 30
    out_30 = end_dt + timedelta(minutes=30)
    assert compute_overtime(out_30, end_dt) == 30

    # 40 min -> 40
    out_40 = end_dt + timedelta(minutes=40, seconds=30)
    assert compute_overtime(out_40, end_dt) == 40

    # R4: work hours (round half up)
    # 27090 seconds = 7.525 hours -> 7.53
    pin = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
    pout = pin + timedelta(seconds=27090)
    assert compute_work_hours(pin, pout) == 7.53

    # R1: overnight shift attendance date
    emp_night = {"shift_start": "22:00", "shift_end": "06:00"}
    pin_night_1 = datetime(2026, 7, 6, 21, 55, 0, tzinfo=IST)
    assert attendance_date(pin_night_1, emp_night) == "2026-07-06"

    pin_night_2 = datetime(2026, 7, 7, 1, 0, 0, tzinfo=IST)
    assert attendance_date(pin_night_2, emp_night) == "2026-07-06"

    pin_night_3 = datetime(2026, 7, 7, 6, 0, 0, tzinfo=IST)
    assert attendance_date(pin_night_3, emp_night) == "2026-07-07"

    # R7: working days
    wd = working_days(date(2026, 7, 1), date(2026, 7, 31), date(2026, 1, 1))
    assert wd == 23

    wd_mid = working_days(date(2026, 7, 1), date(2026, 7, 31), date(2026, 7, 15))
    assert wd_mid == 13

def test_health():
    res = test_client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

def test_employees_crud():
    # List employees
    res = test_client.get("/employees")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 6
    assert len(data["items"]) == 6
    assert data["items"][0]["emp_code"] == "EMP0001"
    # Ensure _id is not in response
    assert "_id" not in data["items"][0]

    # Filter by department
    res_eng = test_client.get("/employees?department=Engineering")
    assert res_eng.status_code == 200
    assert res_eng.json()["total"] == 2

    # Create employee
    new_emp = {
        "emp_code": "EMP9999",
        "name": "Test Engineer",
        "email": "test.eng@example.com",
        "department": "Engineering",
        "shift_start": "10:00",
        "shift_end": "19:00",
        "joined_on": "2026-03-01",
    }
    res_post = test_client.post("/employees", json=new_emp)
    assert res_post.status_code == 201
    created = res_post.json()
    assert created["emp_code"] == "EMP9999"
    assert isinstance(created["created_at"], int)

    # 409 duplicate
    res_dup = test_client.post("/employees", json=new_emp)
    assert res_dup.status_code == 409

    # 422 invalid emp_code
    res_bad_code = test_client.post("/employees", json={**new_emp, "emp_code": "BAD1"})
    assert res_bad_code.status_code == 422

    # 422 shift_start == shift_end
    res_bad_shift = test_client.post("/employees", json={**new_emp, "emp_code": "EMP9998", "shift_end": "10:00"})
    assert res_bad_shift.status_code == 422

    # 422 invalid joined_on (Feb 30)
    res_bad_date = test_client.post("/employees", json={**new_emp, "emp_code": "EMP9998", "joined_on": "2026-02-30"})
    assert res_bad_date.status_code == 422

def test_punch_in_and_punch_out():
    # EMP0002 has no log on 2026-07-06
    # Punch in 09:40:01 IST -> 10 min late (R2)
    # 2026-07-06 09:40:01 IST = 2026-07-06 04:10:01 UTC
    dt_in = datetime(2026, 7, 6, 4, 10, 1, tzinfo=UTC)
    ms_in = to_ms(dt_in)

    res_in = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0002",
        "punched_at": ms_in,
        "status": "PRESENT"
    })
    assert res_in.status_code == 201
    doc = res_in.json()
    assert doc["emp_code"] == "EMP0002"
    assert doc["date"] == "2026-07-06"
    assert doc["punch_in"] == ms_in
    assert doc["punch_out"] is None
    assert doc["late_minutes"] == 10
    assert doc["work_hours"] is None
    assert doc["half_day"] is False
    assert doc["history"] == []
    assert "_id" not in doc

    # Duplicate punch-in on same date -> 409
    res_dup = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0002",
        "punched_at": ms_in,
    })
    assert res_dup.status_code == 409

    # Punch out at 19:15:00 IST (13:45:00 UTC) -> 45 min overtime (shift_end 18:30)
    dt_out = datetime(2026, 7, 6, 13, 45, 0, tzinfo=UTC)
    ms_out = to_ms(dt_out)
    res_out = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP0002",
        "punched_at": ms_out
    })
    assert res_out.status_code == 200
    doc_out = res_out.json()
    assert doc_out["punch_out"] == ms_out
    assert doc_out["overtime_minutes"] == 45
    # Work hours: 04:10:01 to 13:45:00 UTC = 34499 seconds / 3600 = 9.58305 -> 9.58
    assert doc_out["work_hours"] == 9.58
    assert doc_out["half_day"] is False

    # Second punch out -> 409 already punched out
    res_out_dup = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP0002",
        "punched_at": ms_out + 1000
    })
    assert res_out_dup.status_code == 409

def test_punch_out_overnight():
    # EMP0005 has an open punch-in on 2026-07-08 at 21:55 IST (16:25 UTC)
    # Let's punch in for EMP0005 for 2026-07-08
    dt_in = datetime(2026, 7, 8, 16, 25, 0, tzinfo=UTC)
    res_in = test_client.post("/attendance/punch-in", json={
        "emp_code": "EMP0005",
        "punched_at": to_ms(dt_in)
    })
    assert res_in.status_code == 201

    # Punch out next day at 06:40 IST (01:10 UTC on 2026-07-09)
    # Overnight shift 22:00-06:00: shift_end is 06:00 next day -> 40 min overtime
    dt_out = datetime(2026, 7, 9, 1, 10, 0, tzinfo=UTC)
    res_out = test_client.post("/attendance/punch-out", json={
        "emp_code": "EMP0005",
        "punched_at": to_ms(dt_out)
    })
    assert res_out.status_code == 200
    doc = res_out.json()
    assert doc["overtime_minutes"] == 40
    assert doc["work_hours"] == 8.75
    assert doc["date"] == "2026-07-08"

def test_attendance_list():
    res = test_client.get("/attendance")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 9
    assert len(data["items"]) == 9
    # check default sort: date desc, emp_code asc
    dates = [item["date"] for item in data["items"]]
    assert dates == sorted(dates, reverse=True)

    # Filter by date range
    res_filter = test_client.get("/attendance?date_from=2026-07-06&date_to=2026-07-06")
    assert res_filter.status_code == 200
    assert res_filter.json()["total"] == 3

    # 422 if date_from > date_to
    res_bad = test_client.get("/attendance?date_from=2026-07-10&date_to=2026-07-06")
    assert res_bad.status_code == 422

def test_regularize_patch():
    # Regularize EMP0003 on 2026-07-06
    # Original: punch_in 10:05 IST (late_minutes 35)
    # Correct punch_in to 09:30 IST (04:00 UTC) -> late_minutes becomes 0
    new_in = to_ms(datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC))
    res = test_client.patch("/attendance/EMP0003/2026-07-06", json={
        "punch_in": new_in,
        "reason": "biometric reader lagged",
        "regularized_by": "manager.bob"
    })
    assert res.status_code == 200
    doc = res.json()
    assert doc["late_minutes"] == 0
    assert doc["punch_in"] == new_in
    assert len(doc["history"]) == 1
    h = doc["history"][0]
    assert h["by"] == "manager.bob"
    assert h["reason"] == "biometric reader lagged"
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert "punch_in" in h["changes"]

    # Patch with no changes -> 422
    res_noop = test_client.patch("/attendance/EMP0003/2026-07-06", json={
        "punch_in": new_in,
        "reason": "again same punch in",
        "regularized_by": "manager.bob"
    })
    assert res_noop.status_code == 422

    # Patch 404 for missing record
    res_404 = test_client.patch("/attendance/EMP0003/2026-01-01", json={
        "reason": "no such record",
        "regularized_by": "admin"
    })
    assert res_404.status_code == 404

def test_analytics_employee_monthly():
    # EMP0001 July 2026
    res = test_client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] == 2.0
    assert data["leave_days"] == 0
    assert data["late_count"] == 0
    assert data["attendance_pct"] == 8.70

    # 404 unknown employee
    res_404 = test_client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
    assert res_404.status_code == 404

def test_analytics_department_summary():
    res = test_client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    depts = {item["department"]: item for item in data["items"]}
    assert "Engineering" in depts
    assert "Sales" in depts
    assert "Support" in depts
    # Engineering headcount is 2 (joined on or before end of July)
    assert depts["Engineering"]["headcount"] == 2
    assert depts["Engineering"]["present_days"] == 3.0
    assert isinstance(depts["Engineering"]["avg_work_hours"], float)

def test_analytics_late_leaderboard():
    res = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=5")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    items = data["items"]
    # In sample data, EMP0003 has late_minutes = 35
    assert len(items) >= 1
    assert items[0]["emp_code"] == "EMP0003"
    assert items[0]["total_late_minutes"] == 35
    assert items[0]["rank"] == 1

def test_analytics_department_trend():
    res = test_client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-14")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    items = data["items"]
    # 9 days from 2026-07-06 to 2026-07-14
    assert len(items) == 9
    dates = [i["date"] for i in items]
    assert dates[0] == "2026-07-06"
    assert dates[-1] == "2026-07-14"
    # EMP0002 joins on 2026-07-13: headcount is 1 before 2026-07-13, and 2 from 2026-07-13
    assert items[0]["headcount"] == 1  # 2026-07-06
    assert items[6]["headcount"] == 1  # 2026-07-12
    assert items[7]["headcount"] == 2  # 2026-07-13
    assert items[8]["headcount"] == 2  # 2026-07-14
    # Weekend days: 2026-07-11 (index 5) and 2026-07-12 (index 6)
    assert items[5]["is_working_day"] is False
    assert items[5]["attendance_rate"] is None
    # Working days have attendance_rate
    assert items[0]["is_working_day"] is True
    assert isinstance(items[0]["attendance_rate"], float)
    assert isinstance(items[0]["moving_avg_7d"], float)

def test_admin_explain():
    endpoints = [
        ("/admin/explain/attendance_list?page=1&page_size=20", "attendance_logs"),
        ("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07", "attendance_logs"),
        ("/admin/explain/department_summary?month=2026-07", "employees"),
        ("/admin/explain/late_leaderboard?month=2026-07&limit=10", "attendance_logs"),
        ("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-07", "employees"),
    ]
    for url, expected_coll in endpoints:
        res = test_client.get(url)
        assert res.status_code == 200, f"Failed explain for {url}: {res.text}"
        data = res.json()
        assert data["collection"] == expected_coll
        assert "explain" in data
        explain_json = str(data["explain"])
        # Ensure IXSCAN is used and no COLLSCAN in the root plan
        assert "IXSCAN" in explain_json
        assert "COLLSCAN" not in explain_json

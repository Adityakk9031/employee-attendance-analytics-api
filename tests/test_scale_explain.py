import json
import time
from datetime import date, datetime, timedelta
from fastapi.testclient import TestClient
from app.main import app, db, ensure_indexes, UTC

test_client = TestClient(app)

def test_explain_on_100k_records():
    ensure_indexes()
    print("Preparing 100,000 records benchmark...")
    
    # Check if we already have ~100k records in a test collection or attendance_logs
    current_count = db.attendance_logs.count_documents({})
    if current_count < 100_000:
        db.attendance_logs.delete_many({})
        # Create 500 employees across 5 departments
        depts = ["Engineering", "Sales", "Support", "Marketing", "HR"]
        emp_docs = []
        for i in range(1, 501):
            emp_docs.append({
                "emp_code": f"EMP{i:04d}",
                "name": f"Employee {i}",
                "email": f"emp{i}@example.com",
                "department": depts[i % len(depts)],
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            })
        db.employees.delete_many({})
        db.employees.insert_many(emp_docs)
        
        # Insert 100,000 attendance records across July - October 2026 (approx 200 days * 500 employees)
        # Using batches of 10,000 for fast insertion
        batch_size = 10_000
        batch = []
        start_date = date(2026, 7, 1)
        total_needed = 100_000 - current_count
        
        day_offset = 0
        emp_idx = 1
        for n in range(total_needed):
            curr_day = start_date + timedelta(days=day_offset)
            curr_day_str = curr_day.isoformat()
            code = f"EMP{emp_idx:04d}"
            
            is_late = (n % 10 == 0)
            batch.append({
                "emp_code": code,
                "date": curr_day_str,
                "status": "PRESENT",
                "punch_in": datetime.combine(curr_day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=4),
                "punch_out": datetime.combine(curr_day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=13),
                "work_hours": 9.0,
                "late_minutes": 15 if is_late else 0,
                "overtime_minutes": 0,
                "half_day": False,
                "history": [],
            })
            
            emp_idx += 1
            if emp_idx > 500:
                emp_idx = 1
                day_offset += 1
                
            if len(batch) >= batch_size:
                db.attendance_logs.insert_many(batch)
                batch = []
        if batch:
            db.attendance_logs.insert_many(batch)

    count = db.attendance_logs.count_documents({})
    print(f"Dataset ready with {count} attendance records and {db.employees.count_documents({})} employees.")

    # Now verify the explain output of all 5 endpoints
    test_cases = [
        ("attendance_list", "/admin/explain/attendance_list?page=1&page_size=20"),
        ("attendance_list_filtered", "/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31"),
        ("employee_monthly", "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07"),
        ("department_summary", "/admin/explain/department_summary?month=2026-07"),
        ("late_leaderboard", "/admin/explain/late_leaderboard?month=2026-07&limit=10"),
        ("department_trend", "/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-31"),
    ]

    for label, url in test_cases:
        t0 = time.perf_counter()
        res = test_client.get(url)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert res.status_code == 200, f"Explain failed for {label}: {res.text}"
        data = res.json()
        explain_str = json.dumps(data["explain"])
        
        # Check IXSCAN vs COLLSCAN
        assert "IXSCAN" in explain_str, f"Expected IXSCAN in {label} explain plan!"
        assert "COLLSCAN" not in explain_str, f"Found COLLSCAN in {label} explain plan! Plan: {explain_str[:500]}"
        print(f"[{label}] PASS: IXSCAN used, NO COLLSCAN. Execution time: {elapsed_ms:.1f}ms")

if __name__ == "__main__":
    test_explain_on_100k_records()

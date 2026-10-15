# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env     # set MONGO_URI / MONGO_DB (real environment variables take priority)
uvicorn app.main:app --port 8000
```
MongoDB 6.0+ is required (`$densify`, `$setWindowFields`). Indexes are created at startup.
`python sample_seed.py` loads the nine sample documents.

## Notes
- Part A findings: `REVIEW.md`. Part C answers: `DECISIONS.md`.
- All endpoints in `openapi.yaml` are implemented. Analytics and explain use aggregation pipelines built by shared builder functions.
- Rounding (work hours, rates, averages) uses integer arithmetic so it is exactly half-up.

## Testing
The test suite covers unit rules, request validation, concurrency, all analytics pipelines, and index plans:
```bash
python -m pytest tests/ -v
```
All endpoints and pipelines have been thoroughly verified against MongoDB (including MongoDB 7.0):
- **Business rules (R1-R10)**: late calculations (grace period, whole minutes), overtime thresholds (>= 30 min), half-up rounding, overnight shift dating, and Mon-Fri working days logic.
- **Concurrency**: verified race safety for parallel punch-ins (exactly one 201, rest 409) and punch-outs (exactly one 200, rest 409), and optimistic locking on history regularization.
- **Analytics & Pipelines**: tested employee monthly summaries, cross-collection department summaries (including zero-log employees), late leaderboard standard competition ranking with ties (1, 2, 2, 4) and cutoff limits, and department trend gap-filling (`$densify`) with 7-day moving averages.
- **Scale & Indexing (`/admin/explain`)**: benchmarked on ~100k attendance records; confirmed all explained queries perform index scans (`IXSCAN`) with zero collection scans (`COLLSCAN`).

# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run

```bash
python -m venv .venv
.venv\Scripts\activate        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Needs MongoDB 6.0 or newer (tested on 8.0). Settings come from `MONGO_URI` (default `mongodb://localhost:27017`)
and `MONGO_DB` (default `attendance_db`); a local `.env` is read if present. Indexes are created at startup.
Interactive docs: `http://127.0.0.1:8000/docs`.

## Status

Everything in the contract is implemented; nothing is missing. See `REVIEW.md` and `DECISIONS.md`.

## Where the contract was open, I assumed

- `attendance_pct` is rounded to 2 decimals half-up; `attendance_rate` and `moving_avg_7d` to 4.
- PATCH: `ABSENT` / `LEAVE` with punch times is 422; a presence status needs a `punch_in` on the record's date;
  a request that changes nothing is 422; a correction that collides with another gets 409.
- Leaderboard ties share a rank (1, 2, 2, 4) and `limit` applies after ranking, so ties can return more rows.
- `month` must be a year from 1970 to 2100; an empty filter such as `?department=` is applied (matches nothing).
- `/health` is 503 until MongoDB answers.

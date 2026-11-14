"""Queries for the Status page and data freshness indicators."""
from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta, timezone

from app import market_calendar as mcal
from app.config import get_settings
from app.db import get_conn, kv_get
from app.jobs import any_running, is_running
from app.news import sentiment
from app.pipeline import news as news_steps

# (label, SQL returning (latest data point, latest fetch time, row count), max age of the last fetch in days
#  before it's flagged stale (None = updates only when something new exists))
FRESHNESS = [
    ("Stock prices (Yahoo)", "SELECT MAX(date), MAX(fetched_at), COUNT(*) FROM prices WHERE symbol LIKE '%.AX' AND symbol != 'STW.AX'", ("t", 2)),
    ("Market series (Yahoo)", "SELECT MAX(date), MAX(fetched_at), COUNT(*) FROM prices WHERE symbol NOT LIKE '%.AX' OR symbol='STW.AX'", ("t", 2)),
    ("Fundamentals (Yahoo)", "SELECT MAX(as_of_date), MAX(fetched_at), COUNT(*) FROM fundamentals WHERE source='yahoo'", 8),
    ("Key stats + franking (ASX)", "SELECT MAX(as_of_date), MAX(fetched_at), COUNT(*) FROM fundamentals WHERE source='asx'", 8),
    ("Annual financials (Yahoo)", "SELECT MAX(period_end), MAX(fetched_at), COUNT(*) FROM financials_annual", 10),
    ("Dividends", "SELECT MAX(ex_date), MAX(fetched_at), COUNT(*) FROM dividends", None),
    ("Calendar events", "SELECT MAX(event_date), MAX(fetched_at), COUNT(*) FROM calendar_events", 8),
    ("ASX announcements", "SELECT MAX(published_at), MAX(fetched_at), COUNT(*) FROM announcements", 5),
    ("Director trades (3Y)", "SELECT MAX(published_at), MAX(fetched_at), COUNT(*) FROM director_trades", 10),
    ("Short positions (ASIC)", "SELECT MAX(date), MAX(fetched_at), COUNT(*) FROM short_positions", 5),
    ("RBA cash rate", "SELECT MAX(date), MAX(fetched_at), COUNT(*) FROM macro_series WHERE series='cash_rate'", 5),
    ("RBA AUD/USD", "SELECT MAX(date), MAX(fetched_at), COUNT(*) FROM macro_series WHERE series='audusd_rba'", 5),
    ("Technical indicators", "SELECT MAX(date), MAX(computed_at), COUNT(*) FROM technicals", ("t", 2)),
    ("Fundamental scores", "SELECT MAX(as_of_date), MAX(computed_at), COUNT(*) FROM scores", ("t", 2)),
    ("News (RSS)", "SELECT MAX(available_at), MAX(fetched_at), COUNT(*) FROM news_items", 2),
    ("Sentiment scores (FinBERT)", "SELECT MAX(computed_at), MAX(computed_at), COUNT(*) FROM text_sentiment", None),
    ("Forecasts (LightGBM)", "SELECT MAX(as_of_date), MAX(computed_at), COUNT(*) FROM forecasts", ("t", 2)),
    ("Backtest runs", "SELECT MAX(started_at), MAX(finished_at), COUNT(*) FROM backtest_runs WHERE status='ok'", 9),
    ("Index constituents", "SELECT MAX(as_of), MAX(fetched_at), COUNT(*) FROM index_membership", 9),
]


_fresh_cache: dict = {"key": None, "rows": None}


def _freshness(c) -> list[dict]:
    """Freshness rows, memoised until a job starts or finishes (MAX over big tables takes ~0.8 s)."""
    key = (str(get_settings().db_path),
           *c.execute("SELECT MAX(id), MAX(finished_at), COUNT(finished_at) FROM job_runs").fetchone())
    if _fresh_cache["key"] == key:
        rows = _fresh_cache["rows"]
    else:
        rows = [(label, *c.execute(sql).fetchone(), max_age) for label, sql, max_age in FRESHNESS]
        _fresh_cache.update(key=key, rows=rows)
    now = datetime.now(timezone.utc)
    fresh = []
    for label, latest, fetched, n, max_age in rows:
        stale = None
        if max_age is not None:
            try:
                ts = datetime.fromisoformat(str(fetched)) if fetched else None
                ts = ts if ts is None or ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            except ValueError:
                ts = None
            if isinstance(max_age, tuple):      # ("t", n): n ASX trading days (weekends + holidays don't count)
                stale = ts is None or mcal.trading_days_after(ts, now) > max_age[1]
            else:
                stale = ts is None or now - ts > timedelta(days=max_age)
        fresh.append({"label": label, "latest": latest, "fetched_at": fetched, "rows": n,
                      "max_age": max_age, "stale": stale})
    return fresh


def status_snapshot() -> dict:
    s = get_settings()
    with get_conn() as c:
        fresh = _freshness(c)
        last_runs = [dict(r) for r in c.execute(
            "SELECT j.* FROM job_runs j JOIN (SELECT job, MAX(id) id FROM job_runs GROUP BY job) m ON m.id=j.id "
            "ORDER BY j.started_at DESC")]
        last_ok = {r["job"]: r["t"] for r in c.execute(
            "SELECT job, MAX(finished_at) t FROM job_runs WHERE status IN ('ok','partial') GROUP BY job")}
        for r in last_runs:
            r["last_success"] = last_ok.get(r["job"])
        sources = [dict(r) for r in c.execute("SELECT * FROM source_status ORDER BY source")]
        errors = [dict(r) for r in c.execute("SELECT * FROM errors ORDER BY id DESC LIMIT 50")]
        universe_row = c.execute(
            "SELECT MAX(as_of), COUNT(*), MAX(source), MAX(approximate) FROM index_membership "
            "WHERE index_code=? AND as_of=(SELECT MAX(as_of) FROM index_membership WHERE index_code=?)",
            (s.universe, s.universe)).fetchone()
        companies = c.execute("SELECT status, COUNT(*) FROM companies GROUP BY status").fetchall()
        backfill_done = kv_get(c, "initial_backfill_done")
        news_backlog = news_steps.backlog(c)
        last_update = kv_get(c, "last_daily_update")
    return {
        "universe": {"index": s.universe, "as_of": universe_row[0], "members": universe_row[1],
                     "source": universe_row[2], "approximate": bool(universe_row[3])},
        "companies": {r[0]: r[1] for r in companies},
        "freshness": fresh,
        "jobs": last_runs,
        "sources": sources,
        "errors": errors,
        "running": any_running(),
        "initial_backfill_done": backfill_done,
        "last_daily_update": last_update,
        "news_backlog": news_backlog,
        "sentiment_model": sentiment.model_info(),
        "market_open_today": mcal.is_trading_day(mcal.today()),
        "holiday_today": mcal.holiday_name(mcal.today()),
        "next_holidays": [(d.isoformat(), n) for d, n in mcal.upcoming(3)],
    }


def health(snap: dict) -> dict:
    """Overall state for the Status page and dashboard: ok | warn | bad, with reasons."""
    issues = []
    if not snap.get("initial_backfill_done"):
        issues.append(("warn", "The initial backfill hasn't finished."))
    for j in snap["jobs"]:
        if j["status"] == "error":
            issues.append(("bad", f"Job {j['job']} failed at its last run: {j.get('message') or ''}"[:200]))
        elif j["status"] in ("partial", "interrupted") and j["job"] in ("daily_update", "train"):
            issues.append(("warn", f"Job {j['job']} last ended {j['status']}."))
    if snap.get("initial_backfill_done"):
        stale = [f["label"] for f in snap["freshness"] if f.get("stale") and f["rows"]]
        if stale:
            issues.append(("warn", "Not refreshed recently: " + ", ".join(stale) + "."))
    level = "bad" if any(l == "bad" for l, _ in issues) else "warn" if issues else "ok"
    return {"level": level, "issues": [t for _, t in issues]}


def _dir_size(path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _read_int(path: str) -> int | None:
    try:
        v = open(path).read().strip()
        return None if v == "max" else int(v)
    except (OSError, ValueError):
        return None


def storage() -> dict:
    """Disk use of ./data (by part), free space, and the container's memory (cgroup v2)."""
    s = get_settings()
    d = s.data_dir
    parts = []
    for name in ("asx.db", "asx.db-wal", "cache", "models", "backups"):
        p = d / name
        if p.exists():
            parts.append({"name": name, "bytes": p.stat().st_size if p.is_file() else _dir_size(p)})
    du = shutil.disk_usage(d)
    from app.pipeline.maintenance import list_backups
    return {"parts": parts, "total": sum(p["bytes"] for p in parts), "free": du.free, "disk": du.total,
            "mem_used": _read_int("/sys/fs/cgroup/memory.current"), "mem_max": _read_int("/sys/fs/cgroup/memory.max"),
            "mem_peak": _read_int("/sys/fs/cgroup/memory.peak"), "backups": list_backups(), "backup_keep": s.backup_keep}


def data_state() -> dict:
    """Small, cheap summary for the header data bar (every page + polled while a refresh runs):
    when the daily update last finished, the latest market close stored, and the refresh in progress."""
    with get_conn() as c:
        last = kv_get(c, "last_daily_update")
        price_date = c.execute("SELECT MAX(date) FROM prices WHERE symbol='^AXJO'").fetchone()[0]
        run = c.execute("SELECT started_at, finished_at, status, message FROM job_runs WHERE job='daily_update' "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    running = is_running("daily_update")
    # Right after the button is pressed the thread runs but its job_runs row may not exist yet: only use a
    # row that is itself running, never the previous run's summary.
    live = run if running and run and run["status"] == "running" else None
    last_run = run if run and run["status"] != "running" else None
    out = {"last_update": last, "price_date": price_date, "running": running,
           "last_status": last_run["status"] if last_run else None, "message": None, "done": None, "total": None,
           "started_at": live["started_at"] if live else None}
    if live and live["message"]:
        out["message"] = live["message"]
        # Steps report "prices 40/200": expose the counts so the bar can show how far that step is.
        tail = live["message"].rsplit(" ", 1)[-1]
        if "/" in tail:
            a, _, b = tail.partition("/")
            if a.isdigit() and b.isdigit() and int(b) > 0:
                out["done"], out["total"] = int(a), int(b)
    return out

"""Job runner: records each run in job_runs, per-item errors in `errors`,
per-source health in source_status. One instance of a job at a time."""
from __future__ import annotations

import logging
import threading
import traceback
from dataclasses import dataclass, field
from typing import Callable

from app.db import get_conn, utcnow

log = logging.getLogger(__name__)

_running: dict[str, threading.Thread | None] = {}
_running_lock = threading.Lock()


@dataclass
class JobContext:
    job: str
    run_id: int
    items: int = 0
    errors: int = 0
    notes: list[str] = field(default_factory=list)

    def error(self, source: str, item: str, exc: BaseException | str) -> None:
        self.errors += 1
        msg = str(exc)[:500]
        log.warning("[%s] %s %s: %s", self.job, source, item, msg)
        with get_conn() as c:
            c.execute("INSERT INTO errors(at, job, source, item, message) VALUES(?,?,?,?,?)",
                      (utcnow(), self.job, source, item, msg))
        source_failed(source, msg)

    def progress(self, message: str) -> None:
        with get_conn() as c:
            c.execute("UPDATE job_runs SET message=?, items=?, errors=? WHERE id=?",
                      (message[:500], self.items, self.errors, self.run_id))


def source_ok(source: str) -> None:
    now = utcnow()
    with get_conn() as c:
        c.execute(
            "INSERT INTO source_status(source,last_success,last_attempt,ok_count) VALUES(?,?,?,1) "
            "ON CONFLICT(source) DO UPDATE SET last_success=excluded.last_success, "
            "last_attempt=excluded.last_attempt, ok_count=ok_count+1",
            (source, now, now),
        )


def source_failed(source: str, message: str) -> None:
    now = utcnow()
    with get_conn() as c:
        c.execute(
            "INSERT INTO source_status(source,last_attempt,last_error,last_error_at,error_count) "
            "VALUES(?,?,?,?,1) ON CONFLICT(source) DO UPDATE SET last_attempt=excluded.last_attempt, "
            "last_error=excluded.last_error, last_error_at=excluded.last_error_at, "
            "error_count=error_count+1",
            (source, now, message[:500], now),
        )


def is_running(job: str) -> bool:
    with _running_lock:
        t = _running.get(job)
        return t is not None and t.is_alive()


def any_running() -> list[str]:
    with _running_lock:
        return [j for j, t in _running.items() if t is not None and t.is_alive()]


def run_job(job: str, fn: Callable[[JobContext], None]) -> str:
    """Run synchronously in the calling thread. Returns final status."""
    with get_conn() as c:
        cur = c.execute("INSERT INTO job_runs(job, started_at, status) VALUES(?,?, 'running')",
                        (job, utcnow()))
        run_id = cur.lastrowid
    ctx = JobContext(job=job, run_id=run_id)
    log.info("job %s started (run %s)", job, run_id)
    status, message = "ok", ""
    try:
        fn(ctx)
        if ctx.errors:
            status = "partial"
        message = "; ".join(ctx.notes)[:500] or f"{ctx.items} items"
    except Exception as e:
        status = "error"
        message = f"{type(e).__name__}: {e}"[:500]
        log.error("job %s failed: %s\n%s", job, e, traceback.format_exc())
        with get_conn() as c:
            c.execute("INSERT INTO errors(at, job, source, item, message) VALUES(?,?,?,?,?)",
                      (utcnow(), job, "job", job, message))
    with get_conn() as c:
        c.execute(
            "UPDATE job_runs SET finished_at=?, status=?, items=?, errors=?, message=? WHERE id=?",
            (utcnow(), status, ctx.items, ctx.errors, message, run_id),
        )
    log.info("job %s finished: %s (%d items, %d errors)", job, status, ctx.items, ctx.errors)
    # Hook for alerts (phase 7) on failure.
    for cb in JOB_FAILURE_HOOKS:
        if status == "error":
            try:
                cb(job, message)
            except Exception:  # never let alerting break jobs
                log.exception("job failure hook failed")
    return status


JOB_FAILURE_HOOKS: list[Callable[[str, str], None]] = []


def start_job_thread(job: str, fn: Callable[[JobContext], None]) -> bool:
    """Start a job in a background thread unless it's already running."""
    with _running_lock:
        t = _running.get(job)
        if t is not None and t.is_alive():
            return False
        t = threading.Thread(target=run_job, args=(job, fn), name=f"job-{job}", daemon=True)
        _running[job] = t
        t.start()
        return True


def run_exclusive(job: str, fn: Callable[[JobContext], None]) -> None:
    """For the scheduler: run in the scheduler's thread, skip if already running."""
    with _running_lock:
        t = _running.get(job)
        if t is not None and t.is_alive():
            log.info("job %s already running; skipping", job)
            return
        _running[job] = threading.current_thread()
    try:
        run_job(job, fn)
    finally:
        with _running_lock:
            if _running.get(job) is threading.current_thread():
                _running[job] = None

"""Weekly maintenance: online database backup (gzipped, rotated), log-table pruning, PRAGMA optimize."""
from __future__ import annotations

import gzip
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

from app.config import MARKET_TZ, get_settings
from app.db import get_conn
from app.jobs import JobContext

BACKUP_PREFIX = "asx-auto-"
KEEP_ERRORS_DAYS = 90
KEEP_JOB_RUNS_DAYS = 180


def backup_dir():
    return get_settings().data_dir / "backups"


def list_backups() -> list[dict]:
    d = backup_dir()
    if not d.exists():
        return []
    return [{"name": p.name, "size": p.stat().st_size,
             "at": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()}
            for p in sorted(d.glob(f"{BACKUP_PREFIX}*.db.gz"), reverse=True)]


def backup_db(keep: int) -> dict:
    """Consistent online copy (sqlite backup API, safe while the app writes), then gzip.
    Only files named asx-auto-*.db.gz are ever rotated; anything else in backups/ is left alone."""
    s = get_settings()
    d = backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(MARKET_TZ).strftime("%Y-%m-%d-%H%M")
    tmp = d / f".{BACKUP_PREFIX}{stamp}.db"
    out = d / f"{BACKUP_PREFIX}{stamp}.db.gz"
    src = sqlite3.connect(s.db_path, timeout=60)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst, pages=4096)
    finally:
        dst.close()
        src.close()
    with open(tmp, "rb") as fi, gzip.open(out, "wb", compresslevel=3) as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    tmp.unlink()
    removed = []
    for old in sorted(d.glob(f"{BACKUP_PREFIX}*.db.gz"), reverse=True)[max(keep, 1):]:
        old.unlink()
        removed.append(old.name)
    return {"file": out.name, "size": out.stat().st_size, "removed": removed}


def prune(c) -> dict:
    now = datetime.now(timezone.utc)
    e = c.execute("DELETE FROM errors WHERE at < ?", ((now - timedelta(days=KEEP_ERRORS_DAYS)).isoformat(),)).rowcount
    j = c.execute("DELETE FROM job_runs WHERE started_at < ? AND status != 'running'",
                  ((now - timedelta(days=KEEP_JOB_RUNS_DAYS)).isoformat(),)).rowcount
    return {"errors": e, "job_runs": j}


def job_maintenance(ctx: JobContext) -> None:
    keep = get_settings().backup_keep
    with get_conn() as c:
        p = prune(c)
    ctx.notes.append(f"pruned {p['errors']} old errors, {p['job_runs']} old job runs")
    if keep > 0:
        ctx.progress("backing up the database")
        b = backup_db(keep)
        ctx.items += 1
        ctx.notes.append(f"backup {b['file']} ({b['size'] / 1e6:.0f} MB)"
                         + (f", removed {len(b['removed'])} old" if b["removed"] else ""))
    else:
        ctx.notes.append("automatic backups off (BACKUP_KEEP=0)")
    with get_conn() as c:
        c.execute("PRAGMA optimize")

"""In-process APScheduler. All times Australia/Sydney.

  daily_update     Mon-Fri 16:10  prices, fundamentals, announcements, shorts, macro -> tags, sentiment
                                  -> indicators, scores -> forecasts -> paper portfolios (fills + model rebalance)
  announcements    every 30 min 10:00-16:00 Mon-Fri, hourly otherwise (+ RSS news, tags, sentiment)
  shorts           daily 09:05 (ASIC publishes with a lag)
  universe         Sunday 06:00
  train            Saturday 02:00 (walk-forward backtests + retrain)
  maintenance      Sunday 03:30 (DB backup, prune old log rows, PRAGMA optimize)
One-off on start:
  initial_backfill if never completed (resumable)
  news_backfill    if announcement sentiment is incomplete
  train            if no models exist, or the last training is more than 8 days old (missed Saturday)
  daily_update     if the last one predates the latest 16:10 weekday slot (server was off / app restarted)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import market_calendar as mcal
from app.config import MARKET_TZ
from app.db import get_conn, kv_get
from app.jobs import is_running, run_exclusive
from app.pipeline.ingest import JOBS

log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None

# job -> (when, what) for the Status page
JOB_INFO = {
    "daily_update": ("ASX trading days 16:10", "Prices, fundamentals, dividends, announcements, director trades, shorts, RBA; "
                     "then tags, sentiment, indicators, scores, forecasts and paper portfolios"),
    "announcements": ("Every 30 min 10:00–16:00 Mon–Fri, hourly otherwise", "ASX announcements, director notices, RSS news, tags, sentiment"),
    "shorts": ("Daily 09:05", "ASIC short position reports"),
    "universe": ("Sunday 06:00", "Index constituents (ETF holdings files)"),
    "train": ("Saturday 02:00", "Walk-forward backtests and model retraining (~20 min, low priority)"),
    "maintenance": ("Sunday 03:30", "Database backup, prune old log rows, optimise"),
    "forecast": ("End of daily_update", "Forecasts and Buy/Hold/Sell signals from the saved models"),
    "paper": ("End of daily_update", "Fill paper orders; the model portfolio follows the signals"),
    "analysis": ("End of daily_update", "Technical indicators and fundamental scores"),
    "new_codes": ("When you add a stock", "Price history for watchlist/holdings/paper stocks outside the index"),
    "news_backfill": ("Once, after install", "Tag and score every stored announcement headline"),
    "initial_backfill": ("Once, on first start", "10+ years of history for every company"),
}
CATCHUP_DELAY = timedelta(minutes=2)
TRAIN_MAX_AGE = timedelta(days=8)


def _guarded(job: str, trading_days_only: bool = False):
    def run():
        if trading_days_only and not mcal.is_trading_day(datetime.now(MARKET_TZ)):
            log.info("skipping %s: ASX closed today (%s)", job, mcal.holiday_name(datetime.now(MARKET_TZ)) or "weekend")
            return
        if job != "initial_backfill" and is_running("initial_backfill") and job in ("daily_update", "shorts"):
            log.info("skipping %s: initial backfill in progress", job)
            return
        run_exclusive(job, JOBS[job])
    run.__name__ = f"run_{job}"
    return run


def _announcements_poll():
    """Every 30 min in market hours (Mon-Fri 10:00-16:00), otherwise only on the hour."""
    now = datetime.now(MARKET_TZ)
    open_day = mcal.is_trading_day(now)
    in_market = open_day and (10 <= now.hour < 16 or (now.hour == 16 and now.minute == 0))
    if not in_market and now.minute >= 30:
        return
    _guarded("announcements")()


def _flush_alerts():
    from app.alerts import flush_queued
    try:
        flush_queued()
    except Exception:  # noqa: BLE001
        log.exception("alert flush failed")


def last_daily_slot(now: datetime) -> datetime:
    """The most recent 16:10 (Sydney) on an ASX trading day at or before `now`."""
    now = now.astimezone(MARKET_TZ)
    slot = now.replace(hour=16, minute=10, second=0, microsecond=0)
    if slot > now:
        slot -= timedelta(days=1)
    while not mcal.is_trading_day(slot):
        slot -= timedelta(days=1)
    return slot


def needs_daily_catchup(last_update: str | None, now: datetime) -> bool:
    if not last_update:
        return False                       # never ran: the initial backfill covers it
    return datetime.fromisoformat(last_update) < last_daily_slot(now)


def needs_train(meta: dict | None, now: datetime) -> bool:
    if meta is None:
        return True
    try:
        return datetime.fromisoformat(meta["trained_at"]) < now - TRAIN_MAX_AGE
    except (KeyError, ValueError):
        return True


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler:
        return _scheduler
    sch = BackgroundScheduler(
        timezone=MARKET_TZ,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
    )
    sch.add_job(_guarded("daily_update", trading_days_only=True), CronTrigger(day_of_week="mon-fri", hour=16, minute=10),
                id="daily_update", name="Daily update (after close)")
    sch.add_job(_announcements_poll, CronTrigger(minute="0,30"), id="announcements",
                name="ASX announcements + news poll")
    sch.add_job(_guarded("shorts"), CronTrigger(hour=9, minute=5), id="shorts", name="ASIC short positions")
    sch.add_job(_guarded("universe"), CronTrigger(day_of_week="sun", hour=6, minute=0), id="universe",
                name="Index constituents")
    sch.add_job(_guarded("train"), CronTrigger(day_of_week="sat", hour=2, minute=0), id="train",
                name="Retrain models + walk-forward backtests")
    sch.add_job(_flush_alerts, CronTrigger(minute="5,20,35,50"), id="alerts_flush",
                name="Send alerts queued during quiet hours")
    sch.add_job(_guarded("maintenance"), CronTrigger(day_of_week="sun", hour=3, minute=30), id="maintenance",
                name="Backup + maintenance")
    now = datetime.now(MARKET_TZ)
    with get_conn() as c:
        done = kv_get(c, "initial_backfill_done")
        news_done = kv_get(c, "news_backfill_done")
        last_update = kv_get(c, "last_daily_update")
    from app.pipeline.forecast import models_root
    meta_p = models_root() / "current" / "meta.json"
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else None
    if done and needs_daily_catchup(last_update, now):
        sch.add_job(_guarded("daily_update"), "date", run_date=now + CATCHUP_DELAY,
                    id="daily_catchup", name="Daily update (catch-up: missed while stopped)")
    if done and needs_train(meta, now):
        sch.add_job(_guarded("train"), "date", run_date=now + timedelta(minutes=5),
                    id="train_first", name="Model training (none recent)")
    if not news_done:
        sch.add_job(_guarded("news_backfill"), "date", run_date=now + CATCHUP_DELAY,
                    id="news_backfill", name="News tags + sentiment backfill")
    if not done:
        sch.add_job(_guarded("initial_backfill"), "date", run_date=now + timedelta(seconds=15),
                    id="initial_backfill", name="Initial backfill")
    sch.start()
    _scheduler = sch
    log.info("scheduler started with %d jobs", len(sch.get_jobs()))
    return sch


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def scheduled_jobs() -> list[dict]:
    if not _scheduler:
        return []
    return [{"id": j.id, "name": j.name,
             "next_run": j.next_run_time.isoformat() if j.next_run_time else None}
            for j in _scheduler.get_jobs()]

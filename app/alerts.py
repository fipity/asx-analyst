"""Alerts via ntfy and/or a generic webhook (both off unless configured in .env).

Triggers:
- the signal changes on a watchlist or held stock (after the daily forecasts)
- a price-sensitive announcement lands for a watchlist or held stock (every announcements poll)
- a scheduled job fails (at most one alert per job per 6 hours)
Every alert is logged in `alerts` with a dedupe key, so nothing is sent twice. During quiet hours
alerts are queued and sent when quiet hours end (job failures too); the test button ignores them.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, time, timedelta, timezone

import httpx

from app.config import MARKET_TZ, get_settings
from app.db import get_conn, kv_get, kv_set, utcnow

log = logging.getLogger(__name__)
EXPIRE_AFTER = timedelta(days=2)          # queued alerts older than this are dropped, not sent late
JOB_ALERT_EVERY_H = 6


def channels() -> list[str]:
    s = get_settings()
    out = []
    if s.ntfy_url and s.ntfy_topic:
        out.append("ntfy")
    if s.webhook_url:
        out.append("webhook")
    return out


def _parse_hhmm(v: str) -> time | None:
    try:
        h, m = v.strip().split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        return None


def in_quiet_hours(now: datetime | None = None) -> bool:
    s = get_settings()
    a, b = _parse_hhmm(s.quiet_hours_start), _parse_hhmm(s.quiet_hours_end)
    if a is None or b is None or a == b:
        return False
    t = (now or datetime.now(timezone.utc)).astimezone(MARKET_TZ).time()
    return a <= t < b if a < b else (t >= a or t < b)


def _link(path: str | None) -> str | None:
    base = get_settings().app_url.rstrip("/")
    if not path:
        return None
    if path.startswith("http"):
        return path
    return f"{base}{path}" if base else None


def deliver(alert: dict) -> tuple[list[str], str | None]:
    """Send to every configured channel. Returns (channels that worked, error text)."""
    s = get_settings()
    ok, errors = [], []
    click = _link(alert.get("url"))
    if "ntfy" in channels():
        payload = {"topic": s.ntfy_topic, "title": alert["title"], "message": alert.get("body") or alert["title"],
                   "priority": int(alert.get("priority") or 3), "tags": [alert.get("kind") or "info"]}
        if click:
            payload["click"] = click
        headers = {"Authorization": f"Bearer {s.ntfy_token}"} if s.ntfy_token else {}
        try:
            r = httpx.post(s.ntfy_url.rstrip("/"), json=payload, headers=headers, timeout=10)
            r.raise_for_status()
            ok.append("ntfy")
        except httpx.HTTPError as e:
            errors.append(f"ntfy: {e}")
    if "webhook" in channels():
        payload = {"kind": alert.get("kind"), "code": alert.get("code"), "title": alert["title"],
                   "body": alert.get("body"), "url": click, "priority": alert.get("priority"),
                   "created_at": alert.get("created_at"), "text": f"{alert['title']}\n{alert.get('body') or ''}".strip()}
        try:
            r = httpx.post(s.webhook_url, json=payload, timeout=10)
            r.raise_for_status()
            ok.append("webhook")
        except httpx.HTTPError as e:
            errors.append(f"webhook: {e}")
    return ok, ("; ".join(errors)[:500] or None)


def notify(key: str, kind: str, title: str, body: str = "", url: str | None = None, code: str | None = None,
           priority: int = 3, force: bool = False, now: datetime | None = None) -> str:
    """Log + send (or queue) one alert. Returns its status, or 'duplicate'."""
    created = (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
    chans = channels()
    with get_conn() as c:
        cur = c.execute("INSERT OR IGNORE INTO alerts(key, kind, code, title, body, url, priority, created_at, status) "
                        "VALUES(?,?,?,?,?,?,?,?,?)", (key, kind, code, title[:200], body[:2000], url, priority, created,
                                                      "queued" if chans else "skipped"))
        if cur.rowcount == 0:
            return "duplicate"
        aid = cur.lastrowid
    if not chans:
        return "skipped"                 # logged so the Alerts page shows what would have been sent
    if in_quiet_hours(now) and not force:
        return "queued"
    return _send(aid)


def _send(aid: int) -> str:
    with get_conn() as c:
        a = dict(c.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone())
    ok, err = deliver(a)
    status = "sent" if ok else "failed"
    with get_conn() as c:
        c.execute("UPDATE alerts SET status=?, sent_at=?, channels=?, error=? WHERE id=?",
                  (status, utcnow(), ",".join(ok) or None, err, aid))
    if err:
        log.warning("alert %s: %s", a["key"], err)
    return status


def flush_queued(now: datetime | None = None) -> int:
    """Send queued alerts once quiet hours are over; drop ones older than EXPIRE_AFTER."""
    if in_quiet_hours(now) or not channels():
        return 0
    now = now or datetime.now(timezone.utc)
    with get_conn() as c:
        rows = [dict(r) for r in c.execute("SELECT id, created_at FROM alerts WHERE status='queued' ORDER BY id")]
        old = [r["id"] for r in rows if datetime.fromisoformat(r["created_at"]) < now - EXPIRE_AFTER]
        if old:
            c.execute(f"UPDATE alerts SET status='expired' WHERE id IN ({','.join('?' * len(old))})", old)
    n = 0
    for r in rows:
        if r["id"] not in old:
            _send(r["id"])
            n += 1
    return n


def recent(c: sqlite3.Connection, limit: int = 100) -> list[dict]:
    return [dict(r) for r in c.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,))]


# --- triggers ------------------------------------------------------------------------

def followed_codes(c: sqlite3.Connection) -> dict[str, str]:
    """code -> 'held' | 'watchlist'."""
    from app.portfolio import holdings, watchlist
    out = {k: "watchlist" for k in watchlist.codes(c)}
    out.update({k: "held" for k in holdings.held_codes(c)})
    return out


def check_signal_changes(c: sqlite3.Connection, now: datetime | None = None) -> int:
    """Commits `c` before sending (alerts are written through separate connections)."""
    if not get_settings().alert_signals:
        return 0
    dates = [r[0] for r in c.execute("SELECT DISTINCT as_of_date FROM forecasts WHERE horizon=21 "
                                     "ORDER BY as_of_date DESC LIMIT 2")]
    if len(dates) < 2:
        return 0
    follow = followed_codes(c)
    if not follow:
        return 0
    last, prev = dates
    ph = ",".join("?" * len(follow))
    cur = {r["code"]: dict(r) for r in c.execute(
        f"SELECT code, signal, confidence, p_beat FROM forecasts WHERE horizon=21 AND as_of_date=? AND code IN ({ph})",
        [last, *follow])}
    old = {r["code"]: r["signal"] for r in c.execute(
        f"SELECT code, signal FROM forecasts WHERE horizon=21 AND as_of_date=? AND code IN ({ph})", [prev, *follow])}
    cgt = {}
    if any(follow[k] == "held" and v["signal"] == "Sell" for k, v in cur.items()):
        from app.portfolio import holdings
        cgt = {f["code"]: f for f in holdings.summary(c)["cgt_flags"]}
    c.commit()          # notify() writes through its own connection: release any write lock we hold
    n = 0
    for code, f in sorted(cur.items()):
        before = old.get(code)
        if not before or before == f["signal"] or not f["signal"]:
            continue
        body = (f"{'You hold it' if follow[code] == 'held' else 'On your watchlist'}. Chance of beating the ASX 200 over "
                f"the next month: {f['p_beat'] * 100:.0f}% ({f['confidence']} confidence)." if f["p_beat"] is not None else "")
        if f["confidence"] == "low":
            body += " The model showed no reliable skill in testing; treat this as an experiment."
        flag = cgt.get(code)
        if flag and not flag["all_eligible"]:
            body += (f" CGT: {flag['young_units']:,.0f} of your shares were held less than 12 months; selling them now "
                     f"would miss the 50% CGT discount (first eligible {flag['next_date']}).")
            if flag["young_gain"] <= 0:
                body += " Those shares are at a loss, so the discount wouldn't apply anyway."
        status = notify(f"signal:{code}:{last}", "signal", f"{code}: signal {before} → {f['signal']}", body,
                        url=f"/company/{code}", code=code, priority=4 if follow[code] == "held" else 3, now=now)
        n += status != "duplicate"
    return n


def check_announcements(c: sqlite3.Connection, now: datetime | None = None) -> int:
    if not get_settings().alert_announcements:
        return 0
    now = now or datetime.now(timezone.utc)
    since = kv_get(c, "alerts_ann_since")
    if since is None:      # first run: start from now, don't replay history
        kv_set(c, "alerts_ann_since", now.isoformat())
        return 0
    follow = followed_codes(c)
    newest = c.execute("SELECT MAX(fetched_at) FROM announcements").fetchone()[0]
    n = 0
    if follow:
        ph = ",".join("?" * len(follow))
        rows = [dict(r) for r in c.execute(
            f"SELECT ids_id, code, headline, published_at, url, pdf_url FROM announcements WHERE price_sensitive=1 "
            f"AND fetched_at > ? AND published_at > ? AND code IN ({ph}) ORDER BY published_at",
            [since, (now - timedelta(days=2)).isoformat(), *follow])]
        from app.news.ai import for_ids
        ai = for_ids(c, [a["ids_id"] for a in rows])
        c.commit()      # notify() writes through its own connection
        for a in rows:
            when = datetime.fromisoformat(a["published_at"]).astimezone(MARKET_TZ).strftime("%d %b %H:%M")
            status = notify(f"ann:{a['ids_id']}", "announcement", f"{a['code']}: {a['headline']}",
                            f"Price-sensitive announcement, {when}. "
                            f"{'You hold it' if follow[a['code']] == 'held' else 'On your watchlist'}."
                            + (f"\n\nAI summary ({ai[a['ids_id']]['tone']}): {ai[a['ids_id']]['summary']}" if a["ids_id"] in ai else ""),
                            url=a["pdf_url"] or a["url"] or f"/company/{a['code']}", code=a["code"], priority=4, now=now)
            n += status != "duplicate"
    if newest and newest > since:
        kv_set(c, "alerts_ann_since", newest)
    return n


def on_job_failure(job: str, message: str) -> None:
    if not get_settings().alert_jobs:
        return
    now = datetime.now(timezone.utc)
    bucket = now.strftime("%Y%m%d") + f"-{now.hour // JOB_ALERT_EVERY_H}"
    notify(f"job:{job}:{bucket}", "job", f"ASX Analyst: job {job} failed", message[:500], url="/status", priority=4)


def step_alerts(ctx) -> None:
    """Run the triggers (end of daily_update and of each announcements poll) and flush the queue."""
    try:
        with get_conn() as c:
            n = check_announcements(c)
            if ctx.job == "daily_update":
                n += check_signal_changes(c)
        flushed = flush_queued()
        if n or flushed:
            ctx.notes.append(f"alerts: {n} new, {flushed} sent from the queue")
    except Exception as e:  # noqa: BLE001 - alerts must never break data jobs
        ctx.error("alerts", "alerts", e)

"""Watchlist: codes you follow, with price, signal, news and upcoming dates in one place."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

from app.analysis import market
from app.db import utcnow
from app.news import queries as newsq


def codes(c: sqlite3.Connection) -> list[str]:
    return [r[0] for r in c.execute("SELECT code FROM watchlist ORDER BY code")]


def contains(c: sqlite3.Connection, code: str) -> bool:
    return c.execute("SELECT 1 FROM watchlist WHERE code=?", (code.upper(),)).fetchone() is not None


def add(c: sqlite3.Connection, code: str, note: str | None = None) -> bool:
    code = code.strip().upper().removesuffix(".AX")
    if not c.execute("SELECT 1 FROM companies WHERE code=?", (code,)).fetchone():
        raise ValueError(f"unknown ASX code {code}")
    return c.execute("INSERT OR IGNORE INTO watchlist(code, added_at, note) VALUES(?,?,?)",
                     (code, utcnow(), (note or "").strip()[:200] or None)).rowcount > 0


def remove(c: sqlite3.Connection, code: str) -> bool:
    return c.execute("DELETE FROM watchlist WHERE code=?", (code.strip().upper(),)).rowcount > 0


def rows(c: sqlite3.Connection) -> list[dict]:
    wl = {r["code"]: dict(r) for r in c.execute("SELECT * FROM watchlist")}
    if not wl:
        return []
    by_code = {r["code"]: r for r in market.screener_rows(c)}
    ph = ",".join("?" * len(wl))
    last_fc = c.execute("SELECT MAX(as_of_date) FROM forecasts").fetchone()[0]
    fc = {r["code"]: dict(r) for r in c.execute(
        f"SELECT code, signal, confidence, p_beat, ex50, rank FROM forecasts WHERE horizon=21 AND as_of_date=? "
        f"AND code IN ({ph})", [last_fc, *wl])} if last_fc else {}
    today = date.today().isoformat()
    nxt = {}
    for r in c.execute(f"SELECT code, event_type, event_date FROM calendar_events WHERE code IN ({ph}) "
                       f"AND event_date>=? ORDER BY event_date", [*wl, today]):
        nxt.setdefault(r["code"], dict(r))
    since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    ps = {}
    for r in c.execute(f"SELECT code, headline, published_at, url, pdf_url FROM announcements WHERE code IN ({ph}) "
                       f"AND price_sensitive=1 AND published_at>=? ORDER BY published_at DESC", [*wl, since]):
        ps.setdefault(r["code"], dict(r))
    out = []
    for code, w in sorted(wl.items()):
        base = by_code.get(code)
        if base is None:   # outside the universe: compute the price part directly
            comp = market.get_company(c, code) or {"symbol": f"{code}.AX"}
            bars = c.execute("SELECT * FROM prices WHERE symbol=? ORDER BY date DESC LIMIT 300",
                             (comp["symbol"],)).fetchall()[::-1]
            base = {**dict.fromkeys(market.COLUMNS), "code": code, "name": comp.get("name"),
                    "sector": comp.get("sector"), **market.price_metrics(bars), "outside": True}
        news = newsq.company_summary(c, code)
        out.append({**base, **w, "fc": fc.get(code), "next_event": nxt.get(code), "last_ps": ps.get(code),
                    "news_7d": news.get("news_7d"), "news_sent_7d": news.get("news_sent_7d"),
                    "ann_sent_90d": news.get("ann_sent_90d")})
    return out

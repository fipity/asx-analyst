"""Dashboard read-side: market overview cards (with sparklines) and sector performance."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from app.analysis.markets import SLUG_BY_SYMBOL
from app.sources.yahoo import MARKET_SERIES

CARDS = [
    ("^AXJO", "S&P/ASX 200", "{:,.1f}"),
    ("^AORD", "All Ordinaries", "{:,.1f}"),
    ("AUDUSD=X", "AUD/USD", "{:.4f}"),
    ("TIO=F", "Iron ore (USD/t)", "{:,.2f}"),
    ("GC=F", "Gold (USD/oz)", "{:,.0f}"),
    ("BZ=F", "Brent crude (USD/bbl)", "{:,.2f}"),
]
SPARK_DAYS = 66   # ~3 months


def _series(c, symbol: str, since: str) -> list[tuple[str, float]]:
    return [(d, v) for d, v in c.execute(
        "SELECT date, close FROM prices WHERE symbol=? AND date>=? AND close IS NOT NULL ORDER BY date", (symbol, since))]


def _chg(pts: list[tuple[str, float]], back: int) -> float | None:
    if len(pts) <= back:
        return None
    return pts[-1][1] / pts[-1 - back][1] - 1 if pts[-1 - back][1] else None


def _ytd(pts: list[tuple[str, float]]) -> float | None:
    year = pts[-1][0][:4] if pts else None
    prev = [v for d, v in pts if d[:4] < year] if year else []
    return pts[-1][1] / prev[-1] - 1 if prev and prev[-1] else None


def sparkline(values: list[float], w: int = 140, h: int = 32) -> str:
    """SVG path 'd' for a tiny line chart."""
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    step = w / (len(values) - 1)
    return "M" + " L".join(f"{i * step:.1f},{h - 2 - (v - lo) / span * (h - 4):.1f}" for i, v in enumerate(values))


def overview(c: sqlite3.Connection) -> dict:
    since = (date.today() - timedelta(days=400)).isoformat()
    cards = []
    for sym, label, fmt in CARDS:
        pts = _series(c, sym, since)
        if not pts:
            continue
        spark = [v for _, v in pts[-SPARK_DAYS:]]
        cards.append({"symbol": sym, "slug": SLUG_BY_SYMBOL.get(sym), "label": label, "value": fmt.format(pts[-1][1]), "date": pts[-1][0],
                      "chg_1d": _chg(pts, 1), "chg_1m": _chg(pts, 21), "ytd": _ytd(pts),
                      "spark": sparkline(spark), "spark_up": spark[-1] >= spark[0]})
    rate = c.execute("SELECT date, value FROM macro_series WHERE series='cash_rate' ORDER BY date DESC LIMIT 1").fetchone()
    prev = None
    if rate:
        prev = c.execute("SELECT date, value FROM macro_series WHERE series='cash_rate' AND value != ? "
                         "ORDER BY date DESC LIMIT 1", (rate["value"],)).fetchone()
        changed = c.execute("SELECT MIN(date) FROM macro_series WHERE series='cash_rate' AND date > ?",
                          (prev["date"],)).fetchone()[0] if prev else None
        cards.append({"symbol": "cash_rate", "slug": "cash-rate", "label": "RBA cash rate", "value": f"{rate['value']:.2f}%",
                      "date": rate["date"],
                      "note": f"{'up' if rate['value'] > prev['value'] else 'down'} from {prev['value']:.2f}% since {changed}"
                      if prev else None})
    sectors = []
    for sym, (label, kind) in MARKET_SERIES.items():
        if kind != "sector":
            continue
        pts = _series(c, sym, since)
        if pts:
            sectors.append({"symbol": sym, "label": label, "chg_1d": _chg(pts, 1), "chg_1m": _chg(pts, 21),
                            "ytd": _ytd(pts), "chg_1y": _chg(pts, 252)})
    sectors.sort(key=lambda s: -(s["chg_1m"] if s["chg_1m"] is not None else -9))
    return {"cards": cards, "sectors": sectors}

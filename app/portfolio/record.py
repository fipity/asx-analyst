"""Live track record of the signals, measured forward from the day each was issued.

Each *new* signal (the first day of a run of the same signal for a stock) counts once. Its
outcome is the stock's total return (dividend-adjusted close) over the next 21 trading days
against the ASX 200 ETF (STW, dividends included), close to close. Signals younger than 21
trading days are shown "so far" and are not counted in hit rates.
"""
from __future__ import annotations

import sqlite3

import pandas as pd

HORIZON = 21
INDEX = "^AXJO"
BENCH = "STW.AX"


def outcomes(c: sqlite3.Connection, code: str | None = None, episodes: bool = True) -> list[dict]:
    q = ("SELECT f.code, f.as_of_date, f.signal, f.p_beat, f.confidence, COALESCE(co.yahoo_symbol, f.code || '.AX') sym "
         "FROM forecasts f LEFT JOIN companies co ON co.code=f.code WHERE f.horizon=21 AND f.signal IS NOT NULL"
         + (" AND f.code=?" if code else "") + " ORDER BY f.code, f.as_of_date")
    rows = [dict(r) for r in c.execute(q, (code,) if code else ())]
    if not rows:
        return []
    if episodes:
        keep, prev = [], {}
        for r in rows:
            if prev.get(r["code"]) != r["signal"]:
                keep.append(r)
            prev[r["code"]] = r["signal"]
        rows = keep
    start = min(r["as_of_date"] for r in rows)
    cal = [d for (d,) in c.execute("SELECT date FROM prices WHERE symbol=? AND date>=? ORDER BY date", (INDEX, start))]
    if not cal:
        return []
    pos = {d: i for i, d in enumerate(cal)}
    syms = sorted({r["sym"] for r in rows} | {BENCH})
    df = pd.read_sql_query(f"SELECT symbol, date, adj_close FROM prices WHERE symbol IN ({','.join('?' * len(syms))}) "
                           f"AND date>=?", c, params=[*syms, start])
    adj = df.pivot_table(index="date", columns="symbol", values="adj_close").reindex(cal).ffill()
    out = []
    for r in rows:
        i = pos.get(r["as_of_date"])
        if i is None or r["sym"] not in adj.columns:
            continue
        j = min(i + HORIZON, len(cal) - 1)
        r["matured"] = i + HORIZON < len(cal)
        r["end_date"] = cal[j]
        r["days"] = j - i
        a0, a1 = adj[r["sym"]].iloc[i], adj[r["sym"]].iloc[j]
        b0, b1 = adj[BENCH].iloc[i], adj[BENCH].iloc[j]
        r["ret"] = float(a1 / a0 - 1) if a0 and pd.notna(a0) and pd.notna(a1) else None
        r["bench_ret"] = float(b1 / b0 - 1) if b0 and pd.notna(b0) and pd.notna(b1) else None
        r["excess"] = r["ret"] - r["bench_ret"] if r["ret"] is not None and r["bench_ret"] is not None else None
        if r["signal"] == "Buy" and r["excess"] is not None:
            r["hit"] = r["excess"] > 0
        elif r["signal"] == "Sell" and r["excess"] is not None:
            r["hit"] = r["excess"] < 0
        else:
            r["hit"] = None
        out.append(r)
    return out


def summarise(outs: list[dict]) -> dict:
    """Per signal: count, matured count, hit rate, average and worst excess return (matured only)."""
    res = {}
    for sig in ("Buy", "Hold", "Sell"):
        rs = [o for o in outs if o["signal"] == sig]
        m = [o for o in rs if o["matured"] and o["excess"] is not None]
        ex = [o["excess"] for o in m]
        hits = [o["hit"] for o in m if o["hit"] is not None]
        res[sig] = {"n": len(rs), "matured": len(m), "pending": len(rs) - len(m),
                    "hit_rate": sum(hits) / len(hits) if hits else None,
                    "avg_excess": sum(ex) / len(ex) if ex else None,
                    "worst": (min(ex) if sig != "Sell" else max(ex)) if ex else None}
    res["since"] = min((o["as_of_date"] for o in outs), default=None)
    return res

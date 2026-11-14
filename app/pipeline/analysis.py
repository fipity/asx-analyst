"""Analysis steps: daily technical snapshot + fundamental scores.

Run at the end of daily_update / initial_backfill, and on their own as the
`analysis` job (cheap: a few seconds for the ASX 200)."""
from __future__ import annotations

import pandas as pd

from app.analysis import indicators, scoring
from app.db import get_conn, upsert, utcnow
from app.jobs import JobContext

HISTORY_ROWS = 400   # enough for 252-day windows + the 200-day SMA warm-up


def step_technicals(ctx: JobContext, codes: list[str]) -> None:
    now = utcnow()
    rows = []
    with get_conn() as c:
        sym = dict(c.execute(
            f"SELECT code, COALESCE(yahoo_symbol, code || '.AX') FROM companies WHERE code IN ({','.join('?' * len(codes))})",
            codes).fetchall())
        for code in codes:
            s = sym.get(code, f"{code}.AX")
            df = pd.read_sql_query(
                "SELECT date, high, low, close, adj_close, volume FROM "
                "(SELECT * FROM prices WHERE symbol=? ORDER BY date DESC LIMIT ?) ORDER BY date",
                c, params=(s, HISTORY_ROWS))
            try:
                v = indicators.latest(df)
            except Exception as e:  # noqa: BLE001 - one bad series must not stop the rest
                ctx.error("technicals", code, e)
                continue
            if not v:
                continue
            rows.append({"code": code, **{k: v.get(k) for k in indicators.COLUMNS},
                         "date": v["date"], "trend": v["trend"], "computed_at": now})
        upsert(c, "technicals", rows, ["code", "date"])
    ctx.items += len(rows)


def step_scores(ctx: JobContext, codes: list[str], as_of) -> None:
    if not codes:
        return
    with get_conn() as c:
        inputs = scoring.gather(c, codes, as_of)
        scores = scoring.score(inputs)
        sectors = {k: v["sector"] for k, v in inputs.items()}
        upsert(c, "scores", scoring.score_rows(scores, as_of, utcnow(), sectors), ["code", "as_of_date"])
    ctx.items += len(scores)

"""Market detail pages (dashboard cards -> /market/{slug}): history, returns, ranges, what the
series is and why it matters for ASX stocks, and which ASX stocks move most with it."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import numpy as np
import pandas as pd

# slug -> definition. kind: index | fx | commodity | rate
MARKETS: dict[str, dict] = {
    "asx200": {
        "symbol": "^AXJO", "label": "S&P/ASX 200", "kind": "index", "fmt": "{:,.1f}",
        "about": [
            "The S&P/ASX 200 tracks the 200 largest companies on the ASX, weighted by the value of their freely "
            "traded shares. It covers most of the Australian market's value and is the benchmark most funds and "
            "this app compare against.",
            "It's a price index: dividends aren't included. ASX dividends add roughly 3–4% a year, so the "
            "return an investor actually gets is higher. Backtests and paper trading use the SPDR ASX 200 ETF "
            "(STW) as a total-return benchmark for that reason.",
            "Banks and other financials plus miners make up about half the index, so it leans heavily on "
            "interest rates, China and commodity prices. Membership is reviewed every quarter.",
        ],
        "links": [("Sector performance", "/#sectors"), ("Screener", "/screener")],
    },
    "allords": {
        "symbol": "^AORD", "label": "All Ordinaries", "kind": "index", "fmt": "{:,.1f}",
        "about": [
            "The All Ordinaries is the oldest ASX index (started in 1980 at 500 points). It holds the 500 largest "
            "ASX companies, again weighted by freely traded value.",
            "The largest 200 companies dominate it, so it moves almost exactly like the ASX 200. When the two "
            "drift apart, smaller companies (ranks 201–500) are doing better or worse than the big ones.",
            "Like the ASX 200 it's a price index without dividends.",
        ],
    },
    "audusd": {
        "symbol": "AUDUSD=X", "label": "AUD/USD", "kind": "fx", "fmt": "{:.4f}",
        "about": [
            "How many US dollars one Australian dollar buys. A rising number means a stronger Australian dollar.",
            "Many big ASX companies earn in US dollars: miners and energy producers sell commodities priced in "
            "USD, and companies like CSL, ResMed, Cochlear, Aristocrat and Computershare earn most of their money "
            "overseas. A weaker Australian dollar raises their earnings in A$; a stronger one cuts them. "
            "Importers and retailers that buy stock overseas are hit the other way.",
            "The Australian dollar tends to rise with commodity prices and global optimism and fall when "
            "markets are nervous, and it reacts to the gap between Australian and US interest rates.",
        ],
    },
    "iron-ore": {
        "symbol": "TIO=F", "label": "Iron ore (USD/t)", "kind": "commodity", "fmt": "{:,.2f}", "aud": True,
        "about": [
            "Price of iron ore with 62% iron content delivered to China, from the nearest-dated SGX futures "
            "contract, in US dollars per tonne.",
            "Iron ore is Australia's biggest export. It drives the profits of BHP, Rio Tinto, Fortescue, "
            "Mineral Resources and smaller iron ore miners, and through them a large part of the ASX 200 and "
            "of government tax revenue. Demand comes mostly from Chinese steel mills, so Chinese construction "
            "and property news moves it.",
            "Caveat: Yahoo's coverage of this contract is patchy. Gaps and jumps when one futures contract "
            "rolls into the next can show up as sudden moves that didn't happen in the physical market.",
        ],
    },
    "gold": {
        "symbol": "GC=F", "label": "Gold (USD/oz)", "kind": "commodity", "fmt": "{:,.0f}", "aud": True,
        "about": [
            "Gold price from the nearest-dated COMEX futures contract, in US dollars per troy ounce.",
            "Australia is one of the world's biggest gold producers. ASX gold miners such as Northern Star, "
            "Evolution, Perseus and Ramelius pay their costs in Australian dollars, so the price in A$ "
            "(shown above) is what drives their margins: gold can rise in A$ even when it's flat in US$ if "
            "the Australian dollar falls.",
            "Gold is seen as a safe haven. It tends to do well when markets are nervous, when the US dollar "
            "weakens and when inflation-adjusted interest rates fall.",
        ],
    },
    "brent": {
        "symbol": "BZ=F", "label": "Brent crude (USD/bbl)", "kind": "commodity", "fmt": "{:,.2f}", "aud": True,
        "about": [
            "Brent crude oil, the main global oil benchmark, from the nearest-dated ICE futures contract in US "
            "dollars per barrel.",
            "It matters for ASX energy producers such as Woodside and Santos: much of their LNG is sold on "
            "contracts linked to the oil price, usually with a lag of a few months. For airlines (Qantas), "
            "transport and chemicals companies oil is a cost, so higher prices hurt them.",
            "It also feeds petrol prices and inflation, which in turn affects interest rate expectations.",
        ],
    },
    "cash-rate": {
        "symbol": None, "label": "RBA cash rate", "kind": "rate", "fmt": "{:.2f}%",
        "about": [
            "The Reserve Bank of Australia's target for the overnight interest rate banks charge each other. "
            "The RBA sets it at its policy meetings (eight a year since 2024) to keep inflation in its 2–3% "
            "target band.",
            "It flows through to variable mortgage rates, deposit rates and business loans within weeks. "
            "Higher rates slow consumer spending (retailers, travel), weigh on property trusts (REITs), "
            "utilities and high-growth companies whose value depends on profits far in the future, and change "
            "how much banks earn on the gap between loan and deposit rates.",
            "Rate cuts usually help those same groups. Markets move on expectations, so a change that was "
            "widely expected often barely moves share prices on the day.",
        ],
    },
}
SLUG_BY_SYMBOL = {m["symbol"]: k for k, m in MARKETS.items() if m["symbol"]}
SLUG_BY_SYMBOL["cash_rate"] = "cash-rate"

PERIODS = [("1 day", None, 1), ("1 week", 7, None), ("1 month", 30, None), ("3 months", 91, None),
           ("YTD", "ytd", None), ("1 year", 365, None), ("3 years", 365 * 3, None), ("5 years", 365 * 5, None),
           ("10 years", 365 * 10, None)]
CORR_WEEKS = 104       # 2 years of weekly returns
CORR_MIN_WEEKS = 80
MIN_CORR = 0.2


def series(c: sqlite3.Connection, slug: str) -> pd.Series:
    """Full history as a date-indexed float Series (cash rate: one value per decision date)."""
    m = MARKETS[slug]
    if m["kind"] == "rate":
        rows = c.execute("SELECT date, value FROM macro_series WHERE series='cash_rate' AND value IS NOT NULL "
                         "ORDER BY date").fetchall()
    else:
        rows = c.execute("SELECT date, close FROM prices WHERE symbol=? AND close IS NOT NULL ORDER BY date",
                         (m["symbol"],)).fetchall()
    return pd.Series([r[1] for r in rows], index=[r[0] for r in rows], dtype=float)


def _value_on_or_before(s: pd.Series, d: str) -> tuple[str, float] | None:
    i = s.index.searchsorted(d, side="right") - 1
    return (s.index[i], float(s.iloc[i])) if i >= 0 else None


def returns(s: pd.Series, rate: bool = False) -> list[dict]:
    """Change over standard periods, measured from the last value on or before each start date
    (None when the history doesn't reach back that far). Rates: change in percentage points.
    3 years and longer are also annualised (prices only)."""
    if len(s) < 2:
        return []
    last_v = float(s.iloc[-1])
    end = date.fromisoformat(s.index[-1])
    out = []
    for label, days, bars in PERIODS:
        if bars:
            if rate:
                continue                       # "1 day" means nothing for a rate set 8 times a year
            start = (s.index[-1 - bars], float(s.iloc[-1 - bars]))
        else:
            target = f"{end.year - 1}-12-31" if days == "ytd" else (end - timedelta(days=days)).isoformat()
            start = _value_on_or_before(s, target) if target >= s.index[0] else None
        if start is None:
            out.append({"label": label})
            continue
        row = {"label": label, "from": start[0], "from_v": start[1]}
        if rate:
            row["pp"] = last_v - start[1]
        elif start[1]:
            row["chg"] = last_v / start[1] - 1
            if isinstance(days, int) and days >= 365 * 3 and row["chg"] > -1:
                row["ann"] = (1 + row["chg"]) ** (365 / days) - 1
        out.append(row)
    return out


def ranges(s: pd.Series) -> dict:
    end = date.fromisoformat(s.index[-1])
    y = s[s.index >= (end - timedelta(days=365)).isoformat()]
    return {"hi52": float(y.max()), "hi52_d": y.idxmax(), "lo52": float(y.min()), "lo52_d": y.idxmin(),
            "ath": float(s.max()), "ath_d": s.idxmax(), "atl": float(s.min()), "atl_d": s.idxmin(),
            "first": s.index[0], "pos52": (float(s.iloc[-1]) - float(y.min())) / (float(y.max()) - float(y.min()))
            if y.max() > y.min() else None}


def in_aud(c: sqlite3.Connection, s: pd.Series) -> dict | None:
    """Latest USD price in A$ (AUD/USD of the same or nearest earlier day) and its 1-year change in A$."""
    fx = series(c, "audusd")
    if fx.empty or s.empty:
        return None
    fx = fx.reindex(fx.index.union(s.index)).ffill().reindex(s.index)
    aud = (s / fx).dropna()
    if aud.empty:
        return None
    end = date.fromisoformat(aud.index[-1])
    target = (end - timedelta(days=365)).isoformat()
    y = _value_on_or_before(aud, target) if target >= aud.index[0] else None
    return {"value": float(aud.iloc[-1]), "date": aud.index[-1],
            "chg_1y": float(aud.iloc[-1]) / y[1] - 1 if y and y[1] else None}


def _weekly(s: pd.Series) -> pd.Series:
    """Friday-to-Friday returns (weekly, so markets closing in different time zones line up)."""
    idx = pd.to_datetime(s.index)
    return pd.Series(s.values, index=idx).resample("W-FRI").last().pct_change(fill_method=None)


def related_stocks(c: sqlite3.Connection, slug: str, universe: list[dict], n: int = 8) -> dict | None:
    """ASX stocks whose weekly returns moved most with (and against) this series over ~2 years.
    beta = typical % move in the stock per 1% move in the series."""
    m = MARKETS[slug]
    if m["kind"] not in ("commodity", "fx"):
        return None
    s = series(c, slug)
    if len(s) < 300:
        return None
    since = (date.fromisoformat(s.index[-1]) - timedelta(weeks=CORR_WEEKS + 2)).isoformat()
    x = _weekly(s[s.index >= since]).dropna()
    by_symbol = {r["symbol"]: r for r in universe if r.get("symbol")}
    if not by_symbol:
        return None
    q = ("SELECT symbol, date, COALESCE(adj_close, close) FROM prices WHERE date >= ? AND symbol IN (%s)"
         % ",".join("?" * len(by_symbol)))
    df = pd.DataFrame(c.execute(q, [since, *by_symbol]).fetchall(), columns=["symbol", "date", "px"])
    if df.empty:
        return None
    wide = df.pivot(index="date", columns="symbol", values="px").sort_index()
    wide.index = pd.to_datetime(wide.index)
    rets = wide.resample("W-FRI").last().pct_change(fill_method=None)
    rets = rets.loc[rets.index.isin(x.index)]
    xa = x.reindex(rets.index)
    rows = []
    for sym in rets.columns:
        y = rets[sym]
        ok = y.notna() & xa.notna()
        if ok.sum() < CORR_MIN_WEEKS:
            continue
        xv, yv = xa[ok].to_numpy(), y[ok].to_numpy()
        if np.std(xv) == 0 or np.std(yv) == 0:
            continue
        r = float(np.corrcoef(xv, yv)[0, 1])
        beta = float(np.cov(xv, yv)[0, 1] / np.var(xv, ddof=1))
        u = by_symbol[sym]
        rows.append({"code": u["code"], "name": u.get("name"), "sector": u.get("sector"), "corr": r, "beta": beta,
                     "weeks": int(ok.sum())})
    if not rows:
        return None
    rows.sort(key=lambda r: -r["corr"])
    # Below |0.2| a 2-year weekly correlation is mostly noise: don't present it as a relationship.
    pos = [r for r in rows if r["corr"] >= MIN_CORR][:n]
    neg = [r for r in reversed(rows) if r["corr"] <= -MIN_CORR][:5]
    return {"pos": pos, "neg": neg, "best": rows[0]["corr"], "weeks": max(r["weeks"] for r in rows), "since": x.index[0].date().isoformat()}


def rate_decisions(s: pd.Series, n: int = 24) -> list[dict]:
    """Cash-rate changes, newest first."""
    out = []
    prev = None
    for d, v in s.items():
        if prev is not None and v != prev:
            out.append({"date": d, "rate": float(v), "change": float(v - prev)})
        prev = v
    return out[::-1][:n]


def detail(c: sqlite3.Connection, slug: str, universe: list[dict]) -> dict | None:
    m = MARKETS.get(slug)
    if m is None:
        return None
    s = series(c, slug)
    out = {"slug": slug, **m, "has_data": not s.empty}
    if s.empty:
        return out
    rate = m["kind"] == "rate"
    # A rate holds until the next decision: measure its changes up to today, not to the last decision.
    s_now = _carry_to_today(s) if rate else s
    out.update({"value": m["fmt"].format(float(s.iloc[-1])), "raw": float(s.iloc[-1]), "date": s.index[-1],
                "returns": returns(s_now, rate=rate), "ranges": ranges(s_now), "first": s.index[0]})
    if rate:
        out["decisions"] = rate_decisions(s)
        out["since_change"] = out["decisions"][0] if out["decisions"] else None
    if m.get("aud"):
        out["aud"] = in_aud(c, s)
    out["related"] = related_stocks(c, slug, universe)
    return out


def _carry_to_today(s: pd.Series) -> pd.Series:
    today = date.today().isoformat()
    if s.empty or s.index[-1] >= today:
        return s
    return pd.concat([s, pd.Series([s.iloc[-1]], index=[today])])


def chart_points(c: sqlite3.Connection, slug: str) -> dict:
    s = series(c, slug)
    rate = MARKETS[slug]["kind"] == "rate"
    if rate and not s.empty:
        # One point per business day up to today: the chart spaces points evenly, so decision dates
        # alone would stretch busy years (2022) and squash quiet ones.
        s = _carry_to_today(s)
        days = pd.bdate_range(s.index[0], s.index[-1]).strftime("%Y-%m-%d")
        s = s.reindex(s.index.union(days)).ffill()
    digits = 4 if slug == "audusd" else 2
    return {"slug": slug, "step": rate, "digits": digits,
            "points": [[d, round(float(v), digits)] for d, v in s.items()]}

"""Point-in-time feature builder for the forecasting models.

Every feature for (code, date t) uses only information available at the close of
trading day t:
- prices/indicators: rows with date <= t (indicators.compute is causal)
- annual financials: available_at <= t (period end + 60 days)
- dividends: ex_date <= t (amounts as reported by Yahoo; never future declarations)
- ASIC shorts: available_at <= t (report date + publication lag)
- director trades: published_at <= t; NaN before collection started (missing != zero)
- ASX announcements: Sydney time of publication; anything released at/after 16:00
  counts from the next day
- macro series: date <= t
Things that are only known "as of today" (current franking, the earnings calendar,
RSS news) are deliberately NOT features.

Targets (fwd_*) look forward and are only used as labels.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import numpy as np
import pandas as pd

from app.analysis import indicators
from app.analysis.market import SECTOR_INDEX
from app.config import MARKET_TZ
from app.sources.sectors import SECTORS

HORIZONS = (5, 21, 63)
# A stock is only a candidate on a date if it was realistically tradable then. This also
# trims (not removes) survivorship bias: many of today's ASX 200 were penny stocks years ago.
MIN_PRICE = 0.20
MIN_TURNOVER = 1_000_000   # A$ average daily value traded over 20 days
BENCH_TR = "STW.AX"        # total-return proxy for "the market" (ASX 200 ETF, dividends in adj_close)
INDEX = "^AXJO"
BASELINE_WINDOW = 756      # ~3 years of daily h-day returns for baseline A

TECH = ["rsi14", "macd_hist_pct", "bb_pctb", "bb_width", "vol_z", "dist_52w_high", "dist_52w_low",
        "mom_1m", "mom_3m", "mom_6m", "mom_12m", "mom_12_1", "vol_20d", "vol_60d", "px_sma50", "px_sma200",
        "sma50_sma200", "log_turnover", "ret_5d"]
FUND = ["earnings_yield", "book_yield", "fcf_yield", "roe", "net_margin", "debt_equity", "rev_growth",
        "eps_growth", "div_yield_ttm", "div_count_ttm", "days_since_exdiv"]
POSITIONING = ["short_pct", "short_chg20", "dir_net_buy_90d", "dir_buys_90d", "dir_sells_90d"]
NEWS = ["ann_30d", "ann_activity", "ps_7d", "ps_30d", "ps_sent_7d", "ps_sent_30d", "ps_pos_30d", "ps_neg_30d",
        "ev_earnings_30d", "ev_guidance_30d", "ev_raising_30d", "ev_takeover_30d", "ev_halt_30d",
        "ev_dividend_30d", "days_since_results"]
MARKET = ["xjo_mom_21", "xjo_mom_63", "xjo_vol_60", "sector_mom_63", "sector_rel_63", "audusd_chg_63",
          "cash_rate", "cash_rate_chg_6m", "iron_chg_63", "gold_chg_63", "oil_chg_63"]
RANKS = ["mom_12_1_rank", "mom_1m_rank", "vol_60d_rank"]
STATIC = ["sector_id"]
FEATURES = TECH + FUND + POSITIONING + NEWS + MARKET + RANKS + STATIC
FEATURES_NO_NEWS = [f for f in FEATURES if f not in NEWS]
CATEGORICAL = ["sector_id"]
EVENT_FEATURES = {"earnings": "ev_earnings_30d", "guidance": "ev_guidance_30d", "raising": "ev_raising_30d",
                  "takeover": "ev_takeover_30d", "halt": "ev_halt_30d", "dividend": "ev_dividend_30d"}


def _dt(s) -> np.ndarray:
    return np.asarray(pd.to_datetime(pd.Series(s)).values, dtype="datetime64[D]")


def _window_counts(event_days: np.ndarray, weights: np.ndarray, at: np.ndarray, days: int) -> np.ndarray:
    """Sum of weights for events with day in (at - days, at]. event_days must be sorted."""
    if len(event_days) == 0:
        return np.zeros(len(at))
    cs = np.concatenate([[0.0], np.cumsum(weights)])
    hi = np.searchsorted(event_days, at, side="right")
    lo = np.searchsorted(event_days, at - np.timedelta64(days, "D"), side="right")
    return cs[hi] - cs[lo]


def market_calendar(c: sqlite3.Connection, start: str | None = None) -> list[str]:
    q = "SELECT date FROM prices WHERE symbol=? AND close IS NOT NULL" + (" AND date>=?" if start else "") + " ORDER BY date"
    return [r[0] for r in c.execute(q, (INDEX, start) if start else (INDEX,))]


def _series(c, symbol: str, since: str) -> pd.DataFrame:
    df = pd.read_sql_query("SELECT date, open, high, low, close, adj_close, volume FROM prices "
                           "WHERE symbol=? AND date>=? ORDER BY date", c, params=(symbol, since))
    num = ["open", "high", "low", "close", "adj_close", "volume"]
    df[num] = df[num].astype(float)          # empty / all-NULL series must still be float
    return df


def _market_frame(c, since: str) -> pd.DataFrame:
    """Daily market/macro features on the index calendar."""
    xjo = _series(c, INDEX, since).set_index("date")["close"]
    out = pd.DataFrame(index=xjo.index)
    out["xjo_mom_21"] = xjo / xjo.shift(21) - 1
    out["xjo_mom_63"] = xjo / xjo.shift(63) - 1
    out["xjo_vol_60"] = np.log(xjo / xjo.shift(1)).rolling(60).std() * np.sqrt(252)
    for sym, col in (("AUDUSD=X", "audusd_chg_63"), ("TIO=F", "iron_chg_63"), ("GC=F", "gold_chg_63"),
                     ("BZ=F", "oil_chg_63")):
        s = _series(c, sym, since).set_index("date")["close"].reindex(out.index).ffill(limit=5)
        out[col] = s / s.shift(63) - 1
    fx = _series(c, "AUDUSD=X", since).set_index("date")["close"].reindex(out.index).ffill(limit=10)
    out["audusd"] = fx
    cr = pd.read_sql_query("SELECT date, value FROM macro_series WHERE series='cash_rate' ORDER BY date", c)
    if not cr.empty:
        cr_days, cr_vals = _dt(cr["date"]), cr["value"].values.astype(float)
        at = _dt(pd.Series(out.index))

        def asof(days: np.ndarray) -> np.ndarray:
            i = np.searchsorted(cr_days, days, side="right") - 1
            return np.where(i >= 0, cr_vals[np.clip(i, 0, None)], np.nan)

        out["cash_rate"] = asof(at)
        out["cash_rate_chg_6m"] = out["cash_rate"].values - asof(at - np.timedelta64(182, "D"))
    else:
        out["cash_rate"] = np.nan
        out["cash_rate_chg_6m"] = np.nan
    stw = _series(c, BENCH_TR, since).set_index("date")["adj_close"].reindex(out.index).ffill(limit=5)
    out["stw_adj"] = stw
    for sec, sym in SECTOR_INDEX.items():
        s = _series(c, sym, since).set_index("date")["close"].reindex(out.index).ffill(limit=5)
        out[f"sec63::{sec}"] = s / s.shift(63) - 1
    return out


def _stock_frame(px: pd.DataFrame, mkt: pd.DataFrame, horizons=HORIZONS) -> pd.DataFrame:
    """Daily technicals, baseline-A stats and forward targets for one stock (index = date)."""
    ind = indicators.compute(px)
    d = px.dropna(subset=["close"]).set_index("date")
    adj = d["adj_close"].fillna(d["close"]).astype(float)
    close = d["close"].astype(float)
    f = pd.DataFrame(index=ind.index)
    for k in ("rsi14", "bb_pctb", "bb_width", "vol_z", "dist_52w_high", "dist_52w_low", "mom_1m", "mom_3m",
              "mom_6m", "mom_12m", "mom_12_1", "vol_20d", "vol_60d"):
        f[k] = ind[k]
    f["macd_hist_pct"] = ind["macd_hist"] / close
    f["px_sma50"] = close / ind["sma50"] - 1
    f["px_sma200"] = close / ind["sma200"] - 1
    f["sma50_sma200"] = ind["sma50"] / ind["sma200"] - 1
    f["log_turnover"] = np.log1p((close * d["volume"].fillna(0)).rolling(20, min_periods=10).mean())
    f["ret_5d"] = adj / adj.shift(5) - 1
    f["close"] = close
    f["adj_close"] = adj
    vol = d["volume"].fillna(0)
    f["turnover_20d"] = (close * vol).rolling(20, min_periods=10).mean()
    # traded, with price movement, in the last 5 days (excludes suspended / stale series)
    f["active5"] = ((vol.rolling(5).sum() > 0) & (close.rolling(5).std() > 0)).astype(float)
    # benchmark aligned to the stock's own dates
    stw = mkt["stw_adj"].reindex(f.index).ffill(limit=5)
    for h in horizons:
        fwd = adj.shift(-h) / adj - 1
        fwd_b = stw.shift(-h) / stw - 1
        f[f"fwd_ret_{h}"] = fwd
        f[f"fwd_ex_{h}"] = fwd - fwd_b
        # Baseline A: distribution of this stock's past h-day returns that ENDED by t
        past = adj / adj.shift(h) - 1
        past_ex = past - (stw / stw.shift(h) - 1)
        roll = past.rolling(BASELINE_WINDOW, min_periods=252)
        f[f"base_q10_{h}"] = roll.quantile(0.10)
        f[f"base_q50_{h}"] = roll.quantile(0.50)
        f[f"base_q90_{h}"] = roll.quantile(0.90)
        f[f"base_pup_{h}"] = (past > 0).astype(float).where(past.notna()).rolling(BASELINE_WINDOW, min_periods=252).mean()
        f[f"base_pbeat_{h}"] = (past_ex > 0).astype(float).where(past_ex.notna()).rolling(
            BASELINE_WINDOW, min_periods=252).mean()
    return f


def build(c: sqlite3.Connection, codes: list[str], start: str = "2017-03-01", end: str | None = None,
          sample_every: int = 5, dates: list[str] | None = None, horizons=HORIZONS) -> pd.DataFrame:
    """Feature + target panel. One row per (code, sample date).

    Sample dates are every `sample_every`-th trading day counted back from the last
    one (so the latest date is always included), or exactly `dates` if given."""
    first = dates[0] if dates else start
    since = (date.fromisoformat(first) - timedelta(days=int(400 + BASELINE_WINDOW * 1.5))).isoformat()
    cal = market_calendar(c, since)
    if end:
        cal = [d for d in cal if d <= end]
    if dates:
        sample = sorted(set(dates) & set(cal))
    else:
        eligible = [d for d in cal if d >= start]
        sample = eligible[::-1][::sample_every][::-1]
    if not sample:
        return pd.DataFrame()
    mkt = _market_frame(c, since)
    if end:
        mkt = mkt[mkt.index <= end]
    ph = ",".join("?" * len(codes))
    meta = {r[0]: (r[1], r[2]) for r in c.execute(
        f"SELECT code, COALESCE(yahoo_symbol, code || '.AX'), sector FROM companies WHERE code IN ({ph})", codes)}
    rep_ccy = dict(c.execute(
        f"""SELECT f.code, f.currency FROM fundamentals f JOIN (SELECT code, MAX(as_of_date) d FROM fundamentals
            WHERE source='yahoo' GROUP BY code) m ON m.code=f.code AND m.d=f.as_of_date
            WHERE f.source='yahoo' AND f.code IN ({ph})""", codes).fetchall())
    sample_set = set(sample)
    frames = []
    for code in codes:
        sym, sector = meta.get(code, (f"{code}.AX", None))
        px = _series(c, sym, since)
        if end:
            px = px[px["date"] <= end]
        if len(px) < 260:
            continue
        sf = _stock_frame(px, mkt, horizons)
        sf = sf[sf.index.isin(sample_set)]
        if sf.empty:
            continue
        sf = sf.reset_index().rename(columns={"index": "date"})
        sf.insert(0, "code", code)
        sf["sector"] = sector
        sf["sector_id"] = float(SECTORS.index(sector)) if sector in SECTORS else np.nan
        sf["rep_ccy"] = rep_ccy.get(code, "AUD")
        frames.append(sf)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df.merge(mkt.reset_index().rename(columns={"index": "date"}), on="date", how="left")
    df["sector_mom_63"] = np.nan
    for sec in SECTOR_INDEX:
        mask = df["sector"] == sec
        df.loc[mask, "sector_mom_63"] = df.loc[mask, f"sec63::{sec}"]
    df["sector_rel_63"] = df["mom_3m"] - df["sector_mom_63"]
    df = df.drop(columns=[c for c in df.columns if c.startswith("sec63::")])
    _add_fundamentals(c, df, end)
    _add_shorts(c, df)
    _add_director(c, df)
    _add_announcements(c, df, end)
    for col, src in (("mom_12_1_rank", "mom_12_1"), ("mom_1m_rank", "mom_1m"), ("vol_60d_rank", "vol_60d")):
        df[col] = df.groupby("date")[src].rank(pct=True)
    for col in FEATURES:
        if col not in df.columns:
            df[col] = np.nan
    df["eligible"] = ((df["close"] >= MIN_PRICE) & (df["turnover_20d"] >= MIN_TURNOVER)
                      & (df["active5"] > 0)).astype(bool)
    return df.sort_values(["date", "code"]).reset_index(drop=True)


def _fx_to_aud(df: pd.DataFrame) -> np.ndarray:
    """Multiplier converting the company's reporting currency to AUD at each row's date.
    Only USD is supported (AUDUSD); other non-AUD currencies -> NaN (valuation features skipped)."""
    m = np.where(df["rep_ccy"].fillna("AUD") == "AUD", 1.0, np.nan)
    usd = (df["rep_ccy"] == "USD").values
    m[usd] = 1.0 / df.loc[usd, "audusd"].values
    return m


def _add_fundamentals(c, df: pd.DataFrame, end: str | None) -> None:
    fin = pd.read_sql_query("SELECT code, period_end, available_at, revenue, net_income, total_debt, total_equity, "
                            "free_cash_flow, diluted_eps, shares FROM financials_annual WHERE revenue IS NOT NULL "
                            "ORDER BY code, period_end", c)
    if end:
        fin = fin[fin["available_at"] <= end]
    for col in ("roe", "net_margin", "debt_equity", "rev_growth", "eps_growth", "eps", "fcf", "equity", "shares"):
        df[col] = np.nan
    if not fin.empty:
        g = fin.groupby("code")
        fin["rev_growth"] = fin["revenue"] / g["revenue"].shift(1) - 1
        prev_eps = g["diluted_eps"].shift(1)
        fin["eps_growth"] = np.where(prev_eps > 0, fin["diluted_eps"] / prev_eps - 1, np.nan)
        fin["roe"] = np.where(fin["total_equity"] > 0, fin["net_income"] / fin["total_equity"], np.nan)
        fin["net_margin"] = np.where(fin["revenue"] > 0, fin["net_income"] / fin["revenue"], np.nan)
        fin["debt_equity"] = np.where(fin["total_equity"] > 0, fin["total_debt"] / fin["total_equity"], np.nan)
        fin = fin.rename(columns={"diluted_eps": "eps", "free_cash_flow": "fcf", "total_equity": "equity"})
        fin["avail_dt"] = pd.to_datetime(fin["available_at"])
        left = df[["code", "date"]].copy()
        left["d"] = pd.to_datetime(left["date"])
        left["_i"] = np.arange(len(df))
        m = pd.merge_asof(left.sort_values("d"), fin.sort_values("avail_dt"), left_on="d", right_on="avail_dt",
                          by="code", direction="backward").sort_values("_i")
        for col in ("roe", "net_margin", "debt_equity", "rev_growth", "eps_growth", "eps", "fcf", "equity", "shares"):
            df[col] = m[col].values
    fx = _fx_to_aud(df)
    mcap = df["shares"] * df["close"]
    df["earnings_yield"] = df["eps"] * fx / df["close"]
    df["book_yield"] = np.where(mcap > 0, df["equity"] * fx / mcap, np.nan)
    df["fcf_yield"] = np.where(mcap > 0, df["fcf"] * fx / mcap, np.nan)
    # dividends: trailing 12 months, ex_date <= t
    divs = pd.read_sql_query("SELECT code, ex_date, amount FROM dividends WHERE amount > 0 ORDER BY code, ex_date", c)
    if end:
        divs = divs[divs["ex_date"] <= end]
    df["div_ttm"] = 0.0
    df["div_count_ttm"] = 0.0
    df["days_since_exdiv"] = np.nan
    by = {k: v for k, v in divs.groupby("code")}
    at_all = _dt(df["date"])
    for code, idx in df.groupby("code").groups.items():
        d = by.get(code)
        if d is None:
            continue
        ev = _dt(d["ex_date"])
        at = at_all[idx]
        df.loc[idx, "div_ttm"] = _window_counts(ev, d["amount"].values.astype(float), at, 365)
        df.loc[idx, "div_count_ttm"] = _window_counts(ev, np.ones(len(ev)), at, 365)
        last = np.searchsorted(ev, at, side="right") - 1
        days = (at - ev[np.clip(last, 0, None)]).astype(float)
        df.loc[idx, "days_since_exdiv"] = np.where(last >= 0, np.minimum(days, 730), np.nan)
    # Yahoo dividends for USD reporters are in USD
    df["div_yield_ttm"] = df["div_ttm"] * _fx_to_aud(df) / df["close"]


def _add_shorts(c, df: pd.DataFrame) -> None:
    sh = pd.read_sql_query("SELECT code, date, available_at, short_pct FROM short_positions "
                           "WHERE short_pct IS NOT NULL ORDER BY code, date", c)
    df["short_pct"] = np.nan
    df["short_chg20"] = np.nan
    if sh.empty:
        return
    sh["short_chg20"] = sh["short_pct"] - sh.groupby("code")["short_pct"].shift(20)
    sh["avail_dt"] = pd.to_datetime(sh["available_at"])
    left = df[["code", "date"]].copy()
    left["d"] = pd.to_datetime(left["date"])
    left["_i"] = np.arange(len(df))
    m = pd.merge_asof(left.sort_values("d"), sh.sort_values("avail_dt")[["code", "avail_dt", "short_pct", "short_chg20"]],
                      left_on="d", right_on="avail_dt", by="code", direction="backward",
                      tolerance=pd.Timedelta(days=14)).sort_values("_i")
    df["short_pct"] = m["short_pct"].values
    df["short_chg20"] = m["short_chg20"].values


def _local_effective_day(published_at: pd.Series) -> np.ndarray:
    """Sydney date an announcement can be acted on: same day if before 16:00, else next day."""
    ts = pd.to_datetime(published_at, utc=True).dt.tz_convert(MARKET_TZ)
    day = ts.dt.normalize().dt.tz_localize(None)
    day = day + pd.to_timedelta((ts.dt.hour >= 16).astype(int), unit="D")
    return np.asarray(day.values, dtype="datetime64[D]")


def _add_director(c, df: pd.DataFrame) -> None:
    dt = pd.read_sql_query("SELECT code, published_at, trade_type, value FROM director_trades "
                           "WHERE trade_type IN ('buy','sell') ORDER BY published_at", c)
    start = c.execute("SELECT MIN(published_at) FROM director_trades").fetchone()[0]
    for col in ("dir_net_buy_90d", "dir_buys_90d", "dir_sells_90d"):
        df[col] = np.nan
    if not start:
        return
    covered_from = _local_effective_day(pd.Series([start]))[0] + np.timedelta64(90, "D")
    at_all = _dt(df["date"])
    ok = at_all >= covered_from          # before a full 90 days of collection the feature is unknown
    df.loc[ok, ["dir_net_buy_90d", "dir_buys_90d", "dir_sells_90d"]] = 0.0
    if dt.empty:
        return
    dt["day"] = _local_effective_day(dt["published_at"])
    dt["signed"] = np.where(dt["trade_type"] == "buy", 1, -1) * dt["value"].fillna(0)
    for code, idx in df[ok].groupby("code").groups.items():
        d = dt[dt["code"] == code].sort_values("day")
        if d.empty:
            continue
        ev = d["day"].values.astype("datetime64[D]")
        at = at_all[idx]
        df.loc[idx, "dir_net_buy_90d"] = _window_counts(ev, d["signed"].values.astype(float), at, 90)
        df.loc[idx, "dir_buys_90d"] = _window_counts(ev, (d["trade_type"] == "buy").values.astype(float), at, 90)
        df.loc[idx, "dir_sells_90d"] = _window_counts(ev, (d["trade_type"] == "sell").values.astype(float), at, 90)


def _add_announcements(c, df: pd.DataFrame, end: str | None) -> None:
    codes = sorted(df["code"].unique())
    ph = ",".join("?" * len(codes))
    ann = pd.read_sql_query(
        f"""SELECT a.ids_id, a.code, a.published_at, a.price_sensitive, s.score
            FROM announcements a LEFT JOIN text_sentiment s ON s.text_hash=a.sent_key
            WHERE a.code IN ({ph}) ORDER BY a.published_at""", c, params=codes)
    tags = pd.read_sql_query("SELECT ref, tag FROM item_tags WHERE kind='ann' AND tag_type='event'", c)
    first = c.execute("SELECT MIN(published_at) FROM announcements").fetchone()[0]
    for col in NEWS:
        df[col] = np.nan
    if ann.empty or not first:
        return
    ann["day"] = _local_effective_day(ann["published_at"])
    if end:
        ann = ann[ann["day"] <= np.datetime64(end)]
    tag_sets = tags.groupby("ref")["tag"].apply(set).to_dict()
    ann["ps"] = ann["price_sensitive"].astype(float)
    ann["sent"] = ann["score"].astype(float)
    ann["ps_sent"] = np.where(ann["ps"] > 0, ann["sent"].fillna(0), 0.0)
    ann["ps_scored"] = ((ann["ps"] > 0) & ann["sent"].notna()).astype(float)
    ann["ps_pos"] = ((ann["ps"] > 0) & (ann["sent"] >= 0.25)).astype(float)
    ann["ps_neg"] = ((ann["ps"] > 0) & (ann["sent"] <= -0.25)).astype(float)
    for tag in EVENT_FEATURES:
        ann[f"t_{tag}"] = [1.0 if tag in tag_sets.get(i, ()) else 0.0 for i in ann["ids_id"]]
    covered_from = _local_effective_day(pd.Series([first]))[0] + np.timedelta64(365, "D")
    at_all = _dt(df["date"])
    groups = {k: v for k, v in ann.groupby("code")}
    for code, idx in df.groupby("code").groups.items():
        idx = np.asarray(idx)
        at = at_all[idx]
        ok = at >= covered_from        # need a year of history for activity baselines
        if not ok.any():
            continue
        d = groups.get(code)
        if d is None:
            ev = np.array([], dtype="datetime64[D]")
            d = ann.iloc[0:0]
        else:
            d = d.sort_values("day")
            ev = d["day"].values.astype("datetime64[D]")
        w = lambda col, days: _window_counts(ev, d[col].values.astype(float), at, days)
        ones = np.ones(len(ev))
        a30 = _window_counts(ev, ones, at, 30)
        a365 = _window_counts(ev, ones, at, 365)
        with np.errstate(divide="ignore", invalid="ignore"):
            activity = np.where(a365 > 0, a30 / (a365 / 12.0), np.nan)
        vals = {
            "ann_30d": a30,
            "ann_activity": activity,
            "ps_7d": w("ps", 7), "ps_30d": w("ps", 30),
            "ps_pos_30d": w("ps_pos", 30), "ps_neg_30d": w("ps_neg", 30),
        }
        for days in (7, 30):
            n = w("ps_scored", days)
            vals[f"ps_sent_{days}d"] = np.where(n > 0, w("ps_sent", days) / np.where(n > 0, n, 1), np.nan)
        for tag, col in EVENT_FEATURES.items():
            vals[col] = w(f"t_{tag}", 30)
        er = ev[d["t_earnings"].values > 0] if len(ev) else ev
        last = np.searchsorted(er, at, side="right") - 1
        vals["days_since_results"] = np.where(last >= 0, np.minimum((at - er[np.clip(last, 0, None)]).astype(float), 400)
                                              if len(er) else np.nan, np.nan)
        for col, v in vals.items():
            df.loc[idx[ok], col] = np.asarray(v, dtype=float)[ok]

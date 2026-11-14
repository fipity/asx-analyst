"""Technical indicators (pure pandas, no I/O).

Conventions
- Level indicators (SMA/EMA, RSI, MACD, Bollinger, 52-week distance) use the traded
  `close`, the series people see on a chart.
- Return-based ones (momentum, volatility) use `adj_close` (dividend-adjusted), so a
  dividend drop is not counted as a loss.
- Every value at row t uses rows <= t only (rolling windows / EWMs, no centring and
  no backfill), so the same function is safe for point-in-time backtests.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 252
COLUMNS = [
    "close", "sma20", "sma50", "sma200", "ema20", "ema50", "rsi14", "macd", "macd_signal", "macd_hist",
    "bb_upper", "bb_lower", "bb_pctb", "bb_width", "vol_z", "dist_52w_high", "dist_52w_low",
    "mom_1m", "mom_3m", "mom_6m", "mom_12m", "mom_12_1", "vol_20d", "vol_60d",
]


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI. Flat stretches (no gains, no losses) read 50."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    out = 100 - 100 / (1 + avg_gain / avg_loss)
    out = out.where(avg_loss != 0, 100.0)
    out = out.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return out.where(avg_gain.notna())


def compute(df: pd.DataFrame) -> pd.DataFrame:
    """df: one symbol, columns date, high, low, close, adj_close, volume; any order.
    Returns a frame indexed by date with COLUMNS."""
    d = df.dropna(subset=["close"]).sort_values("date").set_index("date")
    close = d["close"].astype(float)
    adj = d["adj_close"].fillna(d["close"]).astype(float)
    high = d["high"].fillna(d["close"]).astype(float)
    low = d["low"].fillna(d["close"]).astype(float)
    vol = d["volume"].astype(float)
    out = pd.DataFrame(index=d.index)
    out["close"] = close
    for n in (20, 50, 200):
        out[f"sma{n}"] = close.rolling(n, min_periods=n).mean()
    for n in (20, 50):
        out[f"ema{n}"] = close.ewm(span=n, adjust=False, min_periods=n).mean()
    out["rsi14"] = rsi(close)
    ema12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
    out["macd"] = ema12 - ema26
    out["macd_signal"] = out["macd"].ewm(span=9, adjust=False, min_periods=9).mean()
    out["macd_hist"] = out["macd"] - out["macd_signal"]
    mid = out["sma20"]
    sd = close.rolling(20, min_periods=20).std(ddof=0)
    out["bb_upper"] = mid + 2 * sd
    out["bb_lower"] = mid - 2 * sd
    band = out["bb_upper"] - out["bb_lower"]
    out["bb_pctb"] = ((close - out["bb_lower"]) / band).where(band > 0)
    out["bb_width"] = (band / mid).where(mid > 0)
    # Volume surprise: today's log volume vs the previous 60 days (today excluded).
    lv = np.log1p(vol.where(vol > 0))
    prev_mean = lv.shift(1).rolling(60, min_periods=20).mean()
    prev_sd = lv.shift(1).rolling(60, min_periods=20).std()
    out["vol_z"] = ((lv - prev_mean) / prev_sd).where(prev_sd > 0)
    hi = high.rolling(TRADING_DAYS, min_periods=20).max()
    lo = low.rolling(TRADING_DAYS, min_periods=20).min()
    out["dist_52w_high"] = close / hi - 1
    out["dist_52w_low"] = close / lo - 1
    for k, n in (("1m", 21), ("3m", 63), ("6m", 126), ("12m", 252)):
        out[f"mom_{k}"] = adj / adj.shift(n) - 1
    out["mom_12_1"] = adj.shift(21) / adj.shift(252) - 1      # classic 12-1 momentum, skips last month
    logret = np.log(adj / adj.shift(1))
    out["vol_20d"] = logret.rolling(20, min_periods=20).std() * math.sqrt(TRADING_DAYS)
    out["vol_60d"] = logret.rolling(60, min_periods=60).std() * math.sqrt(TRADING_DAYS)
    return out[COLUMNS]


def latest(df: pd.DataFrame) -> dict | None:
    """Indicator values for the last row, NaN -> None."""
    if df.empty:
        return None
    ind = compute(df)
    if ind.empty:
        return None
    row = ind.iloc[-1]
    out = {k: (None if pd.isna(v) else float(v)) for k, v in row.items()}
    out["date"] = str(ind.index[-1])
    out["trend"] = trend(out)
    return out


def trend(v: dict) -> str | None:
    """Moving-average alignment: 'up' (price > 50d > 200d), 'down' (price < 50d < 200d), else 'mixed'."""
    c, s50, s200 = v.get("close"), v.get("sma50"), v.get("sma200")
    if None in (c, s50, s200):
        return None
    if c > s50 > s200:
        return "up"
    if c < s50 < s200:
        return "down"
    return "mixed"


def describe(v: dict) -> list[str]:
    """Plain-English observations. Descriptive only: these are not buy/sell signals."""
    if not v:
        return []
    out = []
    c = v.get("close")
    t = v.get("trend")
    if t == "up":
        out.append("Uptrend: price is above its 50-day average, which is above the 200-day average.")
    elif t == "down":
        out.append("Downtrend: price is below its 50-day average, which is below the 200-day average.")
    elif t == "mixed":
        out.append("No clear trend: the price and its 50- and 200-day averages are not lined up.")
    if c is not None and v.get("sma200"):
        gap = c / v["sma200"] - 1
        out.append(f"Price is {abs(gap) * 100:.1f}% {'above' if gap >= 0 else 'below'} its 200-day average.")
    r = v.get("rsi14")
    if r is not None:
        if r >= 70:
            out.append(f"RSI {r:.0f}: above 70, often read as overbought after a strong run.")
        elif r <= 30:
            out.append(f"RSI {r:.0f}: below 30, often read as oversold after a sharp fall.")
        else:
            out.append(f"RSI {r:.0f}: neutral range (30–70).")
    if v.get("macd") is not None and v.get("macd_signal") is not None:
        out.append("MACD is above its signal line (short-term momentum improving)." if v["macd_hist"] >= 0
                   else "MACD is below its signal line (short-term momentum fading).")
    pb = v.get("bb_pctb")
    if pb is not None:
        if pb > 1:
            out.append("Closed above the upper Bollinger band (unusually strong vs the last 20 days).")
        elif pb < 0:
            out.append("Closed below the lower Bollinger band (unusually weak vs the last 20 days).")
    dh = v.get("dist_52w_high")
    if dh is not None and dh > -0.03:
        out.append("Trading within 3% of its 52-week high.")
    dl = v.get("dist_52w_low")
    if dl is not None and dl < 0.03:
        out.append("Trading within 3% of its 52-week low.")
    vz = v.get("vol_z")
    if vz is not None and vz >= 2:
        out.append(f"Volume was unusually high on the last day ({vz:.1f} standard deviations above normal).")
    vol = v.get("vol_60d")
    if vol is not None:
        out.append(f"Volatility (60-day, annualised): {vol * 100:.0f}%.")
    return out


CHART_SERIES = ["sma20", "sma50", "sma200", "bb_upper", "bb_lower", "rsi14", "macd", "macd_signal", "macd_hist"]


INTERVALS = ("D", "W", "M")


def period_key(d: str, interval: str) -> str:
    """Bucket a YYYY-MM-DD trading date: ISO week (Mon-Fri) for W, calendar month for M, itself for D."""
    if interval == "W":
        y, w, _ = pd.Timestamp(d).isocalendar()
        return f"{y}-W{w:02d}"
    if interval == "M":
        return d[:7]
    return d


def period_labels(dates: list[str], interval: str) -> dict[str, str]:
    """Map each (sorted) trading date to its bar's date: the first trading day of its week/month,
    the usual charting convention. D maps every date to itself."""
    out: dict[str, str] = {}
    first: dict[str, str] = {}
    for d in dates:
        out[d] = first.setdefault(period_key(d, interval), d)
    return out


def resample(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Daily rows (date, [open,] high, low, close, adj_close, volume) -> weekly/monthly bars dated by
    the period's first trading day. Uses only days inside each period, so the last bar is the
    period to date."""
    if interval == "D" or df.empty:
        return df
    d = df.dropna(subset=["close"]).sort_values("date").copy()
    d["_p"] = d["date"].map(period_labels(list(d["date"]), interval))
    agg = {"date": "first", "close": "last", "volume": lambda v: v.sum(min_count=1)}
    if "open" in d:
        agg["open"] = "first"
    if "high" in d:
        agg["high"] = "max"
    if "low" in d:
        agg["low"] = "min"
    if "adj_close" in d:
        agg["adj_close"] = "last"
    return d.groupby("_p", sort=True).agg(agg).reset_index(drop=True)[[c for c in df.columns if c in agg]]


def chart_series(df: pd.DataFrame) -> dict:
    """Indicator arrays for the company chart, compact: {"dates": [...], name: [value|null, ...]}
    aligned to `dates`; leading warm-up rows (all null) are dropped."""
    ind = compute(df)[CHART_SERIES]
    ind = ind.loc[ind.notna().any(axis=1).idxmax():] if ind.notna().any().any() else ind.iloc[0:0]
    out: dict = {"dates": list(ind.index)}
    for col in CHART_SERIES:
        digits = 2 if col == "rsi14" else 4
        vals = ind[col].round(digits).astype(object)
        out[col] = vals.where(vals.notna(), None).tolist()
    return out

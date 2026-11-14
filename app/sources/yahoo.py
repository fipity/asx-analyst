"""Yahoo Finance via yfinance (unofficial; personal use). ASX stocks use the
`.AX` suffix. yfinance handles Yahoo's cookie/crumb with browser
impersonation (curl_cffi); we only add rate limiting and normalisation.

Note: Yahoo has no data for most delisted ASX tickers.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import date, timedelta

import pandas as pd
import yfinance as yf

from app.config import get_settings
from app.sources.base import RATE_LIMITER, NotFound, SourceError

log = logging.getLogger(__name__)

# Market-wide series stored in `prices` under their Yahoo symbol.
MARKET_SERIES: dict[str, tuple[str, str]] = {
    "^AXJO": ("S&P/ASX 200", "index"),
    "^AORD": ("All Ordinaries", "index"),
    "^AXEJ": ("Energy", "sector"),
    "^AXMJ": ("Materials", "sector"),
    "^AXNJ": ("Industrials", "sector"),
    "^AXDJ": ("Consumer Discretionary", "sector"),
    "^AXSJ": ("Consumer Staples", "sector"),
    "^AXHJ": ("Health Care", "sector"),
    "^AXFJ": ("Financials", "sector"),
    "^AXIJ": ("Information Technology", "sector"),
    "^AXTJ": ("Communication Services", "sector"),
    "^AXUJ": ("Utilities", "sector"),
    "^AXPJ": ("Real Estate", "sector"),
    "STW.AX": ("SPDR S&P/ASX 200 ETF (total-return proxy)", "benchmark"),
    "AUDUSD=X": ("AUD/USD", "fx"),
    "TIO=F": ("Iron ore 62% Fe (SGX futures, USD/t)", "commodity"),
    "GC=F": ("Gold (COMEX futures, USD/oz)", "commodity"),
    "BZ=F": ("Brent crude (USD/bbl)", "commodity"),
    "CL=F": ("WTI crude (USD/bbl)", "commodity"),
}


def yahoo_symbol(code: str) -> str:
    return f"{code}.AX"


def _throttle() -> None:
    RATE_LIMITER.wait("yahoo", max(1.0, get_settings().rate_limit_seconds))


def _call(fn, what: str, retries: int = 3):
    """Run a yfinance call with throttling and backoff on rate limits."""
    last: Exception | None = None
    for attempt in range(retries):
        _throttle()
        try:
            return fn()
        except Exception as e:  # yfinance raises a mix of types
            name = type(e).__name__
            last = e
            if "RateLimit" in name or "Too Many Requests" in str(e):
                wait = 30 * (attempt + 1)
                log.warning("Yahoo rate limited on %s; sleeping %ss", what, wait)
                time.sleep(wait)
                continue
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    raise SourceError(f"yahoo {what}: {type(last).__name__}: {last}")


def _fnum(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def normalise_history(df: pd.DataFrame) -> tuple[list[dict], list[dict], list[dict]]:
    """-> (price rows, dividend rows, split rows). Dates are exchange-local."""
    prices, divs, splits = [], [], []
    if df is None or df.empty:
        return prices, divs, splits
    for ts, r in df.iterrows():
        d = (ts.date() if hasattr(ts, "date") else pd.Timestamp(ts).date()).isoformat()
        close = _fnum(r.get("Close"))
        if close is not None:
            prices.append({
                "date": d,
                "open": _fnum(r.get("Open")), "high": _fnum(r.get("High")),
                "low": _fnum(r.get("Low")), "close": close,
                "adj_close": _fnum(r.get("Adj Close", close)),
                "volume": _fnum(r.get("Volume")),
            })
        dv = _fnum(r.get("Dividends"))
        if dv:
            divs.append({"ex_date": d, "amount": dv})
        sp = _fnum(r.get("Stock Splits"))
        if sp:
            splits.append({"date": d, "ratio": sp})
    return prices, divs, splits


def fetch_history(symbol: str, start: date | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    def go():
        t = yf.Ticker(symbol)
        if start is None:
            return t.history(period="max", auto_adjust=False, actions=True)
        return t.history(start=start.isoformat(), end=(date.today() + timedelta(days=1)).isoformat(),
                         auto_adjust=False, actions=True)
    df = _call(go, f"history {symbol}")
    rows = normalise_history(df)
    if not rows[0]:
        raise NotFound(f"no price data for {symbol}")
    return rows


_INFO_MAP = {
    "market_cap": "marketCap", "pe": "trailingPE", "forward_pe": "forwardPE",
    "eps": "trailingEps", "pb": "priceToBook", "revenue": "totalRevenue",
    "total_debt": "totalDebt", "total_cash": "totalCash", "debt_to_equity": "debtToEquity",
    "profit_margin": "profitMargins", "operating_margin": "operatingMargins",
    "roe": "returnOnEquity", "revenue_growth": "revenueGrowth",
    "earnings_growth": "earningsGrowth", "payout_ratio": "payoutRatio", "beta": "beta",
    "shares_outstanding": "sharesOutstanding", "week52_high": "fiftyTwoWeekHigh",
    "week52_low": "fiftyTwoWeekLow",
}


def map_info(info: dict) -> dict:
    out = {k: _fnum(info.get(v)) for k, v in _INFO_MAP.items()}
    # Yahoo reports dividendYield in percent (0.85 = 0.85%) in yfinance >= 0.2.5x,
    # including yields under 1%, so always divide.
    dy = _fnum(info.get("dividendYield"))
    out["dividend_yield"] = dy / 100.0 if dy is not None else None
    # Yahoo uses -99999.99 style sentinels for "not meaningful" ratios.
    for k in ("pe", "forward_pe", "pb"):
        if out.get(k) is not None and abs(out[k]) >= 99999:
            out[k] = None
    fcf = _fnum(info.get("freeCashflow"))
    out["fcf_yield"] = fcf / out["market_cap"] if fcf and out.get("market_cap") else None
    out["currency"] = info.get("financialCurrency") or info.get("currency")
    out["sector"] = info.get("sector")
    out["industry"] = info.get("industry")
    return out


def fetch_info(symbol: str) -> dict:
    info = _call(lambda: yf.Ticker(symbol).info, f"info {symbol}")
    if not info or (info.get("marketCap") is None and info.get("trailingPE") is None
                    and info.get("regularMarketPrice") is None):
        raise NotFound(f"no info for {symbol}")
    return info


def map_calendar(cal: dict) -> list[dict]:
    events = []
    if not cal:
        return events
    for d in cal.get("Earnings Date") or []:
        if d:
            events.append({"event_type": "earnings", "event_date": pd.Timestamp(d).date().isoformat()})
    for key, et in (("Ex-Dividend Date", "ex_dividend"), ("Dividend Date", "dividend_pay")):
        d = cal.get(key)
        if d:
            events.append({"event_type": et, "event_date": pd.Timestamp(d).date().isoformat()})
    return events


def fetch_calendar(symbol: str) -> list[dict]:
    return map_calendar(_call(lambda: yf.Ticker(symbol).calendar, f"calendar {symbol}") or {})


def _row(df: pd.DataFrame, names: list[str], col) -> float | None:
    if df is None or df.empty:
        return None
    for n in names:
        if n in df.index:
            return _fnum(df.at[n, col])
    return None


def map_financials(inc: pd.DataFrame, bal: pd.DataFrame, cf: pd.DataFrame) -> list[dict]:
    rows = []
    if inc is None or inc.empty:
        return rows
    for col in inc.columns:
        period_end = pd.Timestamp(col).date()
        bcol = col if bal is not None and col in getattr(bal, "columns", []) else None
        ccol = col if cf is not None and col in getattr(cf, "columns", []) else None
        rows.append({
            "period_end": period_end.isoformat(),
            "revenue": _row(inc, ["Total Revenue", "Operating Revenue"], col),
            "net_income": _row(inc, ["Net Income", "Net Income Common Stockholders"], col),
            "ebit": _row(inc, ["EBIT", "Operating Income"], col),
            "diluted_eps": _row(inc, ["Diluted EPS", "Basic EPS"], col),
            "shares": _row(inc, ["Diluted Average Shares", "Basic Average Shares"], col),
            "total_debt": _row(bal, ["Total Debt"], bcol) if bcol is not None else None,
            "total_equity": _row(bal, ["Stockholders Equity", "Common Stock Equity"], bcol) if bcol is not None else None,
            "operating_cash_flow": _row(cf, ["Operating Cash Flow"], ccol) if ccol is not None else None,
            "free_cash_flow": _row(cf, ["Free Cash Flow"], ccol) if ccol is not None else None,
            # Conservative point-in-time lag: ASX annual reports are due within ~2-3 months.
            "available_at": (period_end + timedelta(days=60)).isoformat(),
        })
    return rows


def fetch_financials(symbol: str) -> list[dict]:
    def go():
        t = yf.Ticker(symbol)
        return t.income_stmt, t.balance_sheet, t.cashflow
    inc, bal, cf = _call(go, f"financials {symbol}")
    return map_financials(inc, bal, cf)

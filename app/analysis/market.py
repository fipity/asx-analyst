"""Read-side queries for the screener and company pages.

Everything here is computed from stored rows on request (fast enough for a few
hundred stocks) and memoised on a cheap "data version" key, so a page load after
the daily update sees fresh numbers without any extra job.
"""
from __future__ import annotations

import bisect
import json
import sqlite3
import threading
from datetime import date, datetime, timedelta
from statistics import median

from app.config import MARKET_TZ, get_settings
from app.sources.yahoo import MARKET_SERIES

# Trading-day offsets used for returns.
PERIODS = {"1m": 21, "3m": 63, "6m": 126, "1y": 252, "3y": 756, "5y": 1260}
SECTOR_INDEX = {name: sym for sym, (name, kind) in MARKET_SERIES.items() if kind == "sector"}
BENCHMARK = "^AXJO"
CORPORATE_TAX = 0.30


def gross_up(yield_: float | None, franking_pct: float | None) -> float | None:
    """Grossed-up dividend yield: cash yield plus franking credits (30% company tax)."""
    if yield_ is None:
        return None
    f = (franking_pct or 0) / 100
    return yield_ * (1 + f * CORPORATE_TAX / (1 - CORPORATE_TAX))


def _ret(series: list[float], n: int) -> float | None:
    if len(series) <= n or not series[-1 - n]:
        return None
    return series[-1] / series[-1 - n] - 1


def price_metrics(rows: list[sqlite3.Row]) -> dict:
    """rows: one symbol's prices ordered by date ascending (at least the last year)."""
    if not rows:
        return {}
    adj = [r["adj_close"] or r["close"] for r in rows]
    close = [r["close"] for r in rows]
    last = rows[-1]
    year = rows[-252:]
    hi = max((r["high"] or r["close"]) for r in year)
    lo = min((r["low"] or r["close"]) for r in year)
    vols = [(r["close"] or 0) * (r["volume"] or 0) for r in rows[-20:]]
    m = {
        "price": last["close"],
        "price_date": last["date"],
        "chg_1d": _ret(close, 1),
        "high_52w": hi,
        "low_52w": lo,
        "off_high": (last["close"] / hi - 1) if hi else None,
        "turnover_20d": sum(vols) / len(vols) if vols else None,
    }
    for k, n in PERIODS.items():
        m[f"ret_{k}"] = _ret(adj, n)          # total return (Yahoo dividend-adjusted)
        m[f"pret_{k}"] = _ret(close, n)       # price only, comparable with indices
    return m


def latest_fundamentals(c: sqlite3.Connection, codes: list[str] | None = None,
                        as_of: str | None = None) -> dict[str, dict]:
    """Merge the latest Yahoo and ASX snapshots per code (latest with as_of_date <= as_of if given).

    Yahoo is the base; ASX key statistics supply franking and fill gaps."""
    where, args = "", []
    if codes is not None:
        where = f"AND f.code IN ({','.join('?' * len(codes))})"
        args = list(codes)
    cutoff = as_of or "9999-12-31"
    rows = c.execute(
        f"""SELECT f.* FROM fundamentals f
            JOIN (SELECT code, source, MAX(as_of_date) d FROM fundamentals WHERE as_of_date <= ?
                  GROUP BY code, source) m
              ON m.code=f.code AND m.source=f.source AND m.d=f.as_of_date
            WHERE 1=1 {where}""", [cutoff, *args]).fetchall()
    out: dict[str, dict] = {}
    by_src: dict[str, dict[str, dict]] = {}
    for r in rows:
        by_src.setdefault(r["code"], {})[r["source"]] = dict(r)
    for code, srcs in by_src.items():
        y, a = srcs.get("yahoo", {}), srcs.get("asx", {})
        merged = {}
        for k in ("market_cap", "pe", "forward_pe", "eps", "pb", "revenue", "net_income", "total_debt",
                  "total_cash", "debt_to_equity", "profit_margin", "operating_margin", "roe",
                  "revenue_growth", "earnings_growth", "dividend_yield", "payout_ratio", "fcf_yield",
                  "beta", "shares_outstanding", "currency", "industry"):
            v = y.get(k)
            merged[k] = v if v is not None else a.get(k)
        # ASX first for dividend data: franking and yield come from the same source then.
        for k in ("franking_pct", "dividend_yield"):
            merged[k] = a.get(k) if a.get(k) is not None else y.get(k)
        # A negative P/E (loss-maker) is "not meaningful"; blank it so "max P/E" filters behave.
        merged["loss_making"] = (merged["pe"] is not None and merged["pe"] <= 0) or (merged.get("eps") or 0) < 0
        for k in ("pe", "forward_pe"):
            if merged[k] is not None and merged[k] <= 0:
                merged[k] = None
        # Yahoo reports D/E in percent (50.5 = 0.505).
        if merged["debt_to_equity"] is not None:
            merged["debt_to_equity"] = merged["debt_to_equity"] / 100
        merged["gross_yield"] = gross_up(merged["dividend_yield"], merged["franking_pct"])
        merged["as_of"] = max((s.get("as_of_date") or "") for s in srcs.values()) or None
        out[code] = merged
    return out


def latest_shorts(c: sqlite3.Connection, codes: list[str] | None = None, lookback_rows: int = 20) -> dict[str, dict]:
    """Latest ASIC short % and its change over ~20 reports."""
    since = (date.today() - timedelta(days=60)).isoformat()
    where, args = "", [since]
    if codes is not None:
        where = f"AND code IN ({','.join('?' * len(codes))})"
        args += list(codes)
    series: dict[str, list[tuple[str, float]]] = {}
    for r in c.execute(f"SELECT code, date, short_pct FROM short_positions WHERE date>=? {where} ORDER BY code, date", args):
        series.setdefault(r[0], []).append((r[1], r[2]))
    out = {}
    for code, s in series.items():
        last_d, last = s[-1]
        prev = s[-1 - lookback_rows][1] if len(s) > lookback_rows else s[0][1]
        out[code] = {"short_pct": last, "short_date": last_d,
                     "short_chg": (last - prev) if last is not None and prev is not None else None}
    return out


def latest_technicals(c: sqlite3.Connection, codes: list[str]) -> dict[str, dict]:
    ph = ",".join("?" * len(codes))
    return {r["code"]: dict(r) for r in c.execute(
        f"""SELECT t.* FROM technicals t JOIN (SELECT code, MAX(date) d FROM technicals WHERE code IN ({ph})
            GROUP BY code) m ON m.code=t.code AND m.d=t.date""", codes)}


def latest_scores(c: sqlite3.Connection, codes: list[str]) -> dict[str, dict]:
    ph = ",".join("?" * len(codes))
    out = {}
    for r in c.execute(
            f"""SELECT s.* FROM scores s JOIN (SELECT code, MAX(as_of_date) d FROM scores WHERE code IN ({ph})
                GROUP BY code) m ON m.code=s.code AND m.d=s.as_of_date""", codes):
        d = dict(r)
        extra = json.loads(d.pop("detail_json") or "{}")
        d.update(extra)
        out[d["code"]] = d
    return out


def universe_members(c: sqlite3.Connection, index_code: str) -> list[sqlite3.Row]:
    return c.execute(
        """SELECT m.code, m.weight, m.approximate, m.as_of, c.name, c.sector, c.industry_group,
                  c.market_cap AS dir_market_cap, COALESCE(c.yahoo_symbol, m.code || '.AX') AS symbol
           FROM index_membership m LEFT JOIN companies c ON c.code=m.code
           WHERE m.index_code=? AND m.as_of=(SELECT MAX(as_of) FROM index_membership WHERE index_code=?)
           ORDER BY m.weight DESC""", (index_code, index_code)).fetchall()


def _data_version(c: sqlite3.Connection) -> tuple:
    """Changes whenever a job finishes (every data write goes through a job run)."""
    return (
        tuple(c.execute("SELECT MAX(finished_at), COUNT(finished_at) FROM job_runs").fetchone()),
        c.execute("SELECT MAX(fetched_at) FROM index_membership").fetchone()[0],
        str(get_settings().db_path),
    )


_cache_lock = threading.Lock()
_cache: dict = {"key": None, "rows": None}


def screener_rows(c: sqlite3.Connection) -> list[dict]:
    """One merged row per universe member (memoised until the data changes)."""
    key = _data_version(c) + (get_settings().universe,)
    with _cache_lock:
        if _cache["key"] == key:
            return _cache["rows"]
    rows = _build_screener_rows(c)
    with _cache_lock:
        _cache.update(key=key, rows=rows)
    return rows


def _build_screener_rows(c: sqlite3.Connection) -> list[dict]:
    members = universe_members(c, get_settings().universe)
    if not members:
        return []
    codes = [m["code"] for m in members]
    symbols = {m["symbol"]: m["code"] for m in members}
    since = (date.today() - timedelta(days=400)).isoformat()
    prices: dict[str, list] = {}
    ph = ",".join("?" * len(symbols))
    for r in c.execute(f"SELECT symbol, date, open, high, low, close, adj_close, volume FROM prices "
                       f"WHERE symbol IN ({ph}) AND date>=? ORDER BY symbol, date", [*symbols, since]):
        prices.setdefault(r["symbol"], []).append(r)
    fund = latest_fundamentals(c, codes)
    shorts = latest_shorts(c, codes)
    market_date = c.execute("SELECT MAX(date) FROM prices WHERE symbol=?", (BENCHMARK,)).fetchone()[0]
    tech = latest_technicals(c, codes)
    score = latest_scores(c, codes)
    out = []
    for m in members:
        row = {**dict.fromkeys(COLUMNS), "chg_1d": None, "price_date": None, "high_52w": None, "low_52w": None,
               "code": m["code"], "name": m["name"] or m["code"], "sector": m["sector"] or "Unknown",
               "industry_group": m["industry_group"], "weight": m["weight"], "symbol": m["symbol"]}
        row.update(price_metrics(prices.get(m["symbol"], [])))
        f = fund.get(m["code"], {})
        row.update({k: f.get(k) for k in ("market_cap", "pe", "forward_pe", "pb", "dividend_yield",
                                            "franking_pct", "gross_yield", "roe", "debt_to_equity",
                                            "profit_margin", "revenue_growth", "currency", "industry",
                                            "loss_making")})
        if row["market_cap"] is None:
            row["market_cap"] = m["dir_market_cap"]
        row.update(shorts.get(m["code"], {"short_pct": None, "short_chg": None, "short_date": None}))
        t = tech.get(m["code"], {})
        row.update({k: t.get(k) for k in ("rsi14", "trend", "vol_60d", "mom_12_1", "sma200", "macd_hist")})
        row["vs_sma200"] = (row["price"] / t["sma200"] - 1) if t.get("sma200") and row.get("price") else None
        sc = score.get(m["code"], {})
        row.update({k: sc.get(k) for k in ("composite", "valuation", "quality", "growth", "dividend", "sector_rank")})
        row["stale"] = bool(market_date and row.get("price_date") and row["price_date"] < market_date)
        out.append(row)
    return out


# --- screener filtering -----------------------------------------------------

# Column registry: key -> (label, format, help). Format drives templates + sorting.
COLUMNS: dict[str, tuple[str, str, str]] = {
    "code": ("Code", "text", "ASX code"),
    "name": ("Company", "text", ""),
    "sector": ("Sector", "text", "GICS sector"),
    "price": ("Price", "price", "Last close (A$)"),
    "chg_1d": ("1d", "pct", "Change on the last trading day"),
    "ret_1m": ("1m", "pct", "Total return incl. dividends (Yahoo-adjusted)"),
    "ret_3m": ("3m", "pct", "Total return incl. dividends"),
    "ret_1y": ("1y", "pct", "Total return incl. dividends"),
    "off_high": ("From 52w high", "pct", "How far below the 52-week high"),
    "market_cap": ("Mkt cap", "money", "Market capitalisation (A$)"),
    "pe": ("P/E", "ratio", "Trailing price/earnings"),
    "forward_pe": ("Fwd P/E", "ratio", "Forward P/E (analyst estimates, Yahoo)"),
    "pb": ("P/B", "ratio", "Price/book"),
    "dividend_yield": ("Yield", "pct", "Trailing cash dividend yield"),
    "franking_pct": ("Franking", "pct100", "Franking % of the latest dividend (ASX)"),
    "gross_yield": ("Gross yield", "pct", "Yield incl. franking credits at 30% tax"),
    "roe": ("ROE", "pct", "Return on equity"),
    "debt_to_equity": ("D/E", "ratio", "Total debt / equity"),
    "profit_margin": ("Margin", "pct", "Net profit margin"),
    "revenue_growth": ("Rev growth", "pct", "Revenue growth (latest, y/y)"),
    "short_pct": ("Short %", "pct100", "ASIC reported short position, % of shares on issue"),
    "short_chg": ("Short Δ", "pp", "Change in short % over ~20 reports (percentage points)"),
    "turnover_20d": ("Turnover", "money", "Average daily value traded, last 20 days"),
    "composite": ("Score", "score", "Overall fundamental score, 0-100, ranked within the sector (see a company page for the parts)"),
    "valuation": ("Value", "score", "Valuation score: earnings, forward earnings, book and free-cash-flow yields"),
    "quality": ("Quality", "score", "Quality score: ROE, margin, debt, profit record"),
    "growth": ("Growth", "score", "Growth score: revenue and earnings growth, latest and multi-year"),
    "dividend": ("Div score", "score", "Dividend score: gross yield, franking, dividend growth, consistency"),
    "ret_6m": ("6m", "pct", "Total return incl. dividends"),
    "mom_12_1": ("12-1m", "pct", "Return from 12 months ago to 1 month ago (classic momentum measure)"),
    "trend": ("Trend", "trend", "Up: price > 50-day > 200-day average. Down: the reverse. Otherwise mixed"),
    "vs_sma200": ("vs 200d", "pct", "Price vs its 200-day average"),
    "rsi14": ("RSI", "ratio0", "14-day RSI: above 70 often read as overbought, below 30 as oversold"),
    "vol_60d": ("Volatility", "pct0", "60-day volatility, annualised"),
}
# Columns where "lower is better" (or alphabetical): the first click sorts ascending.
ASC_FIRST = {"code", "name", "sector", "trend", "pe", "forward_pe", "pb", "debt_to_equity", "short_pct", "short_chg",
             "off_high", "vol_60d"}
DEFAULT_COLUMNS = ["code", "name", "sector", "price", "chg_1d", "ret_1m", "ret_1y", "market_cap",
                   "composite", "pe", "gross_yield", "franking_pct", "roe", "trend", "short_pct"]

# Screener views (column presets): key -> (label, columns)
VIEWS: dict[str, tuple[str, list[str]]] = {
    "overview": ("Overview", DEFAULT_COLUMNS),
    "scores": ("Scores", ["code", "name", "sector", "price", "composite", "valuation", "quality", "growth", "dividend",
                          "ret_1y", "market_cap"]),
    "valuation": ("Valuation", ["code", "name", "sector", "price", "pe", "forward_pe", "pb", "valuation",
                                "market_cap", "ret_1y"]),
    "quality": ("Quality & growth", ["code", "name", "sector", "roe", "profit_margin", "debt_to_equity",
                                     "revenue_growth", "quality", "growth", "market_cap"]),
    "dividends": ("Dividends", ["code", "name", "sector", "price", "dividend_yield", "franking_pct", "gross_yield",
                                "dividend", "market_cap"]),
    "technical": ("Technical", ["code", "name", "sector", "price", "chg_1d", "ret_1m", "ret_3m", "ret_6m", "ret_1y",
                                "mom_12_1", "trend", "vs_sma200", "rsi14", "vol_60d", "off_high", "short_pct"]),
    "shorts": ("Shorts", ["code", "name", "sector", "price", "short_pct", "short_chg", "ret_1m", "ret_1y",
                          "turnover_20d", "market_cap"]),
}

# Numeric filters: param -> (column, op, multiplier from the form value to stored units, label, unit)
NUM_FILTERS: dict[str, tuple[str, str, float, str, str]] = {
    "min_mcap": ("market_cap", ">=", 1e9, "Min market cap", "A$ bn"),
    "max_pe": ("pe", "<=", 1, "Max P/E", ""),
    "min_yield": ("gross_yield", ">=", 0.01, "Min gross yield", "%"),
    "min_franking": ("franking_pct", ">=", 1, "Min franking", "%"),
    "min_roe": ("roe", ">=", 0.01, "Min ROE", "%"),
    "max_de": ("debt_to_equity", "<=", 1, "Max D/E", "×"),
    "max_short": ("short_pct", "<=", 1, "Max short", "%"),
    "min_ret_1y": ("ret_1y", ">=", 0.01, "Min 1y return", "%"),
    "min_score": ("composite", ">=", 1, "Min score", "0–100"),
    "min_rsi": ("rsi14", ">=", 1, "Min RSI", ""),
    "max_rsi": ("rsi14", "<=", 1, "Max RSI", ""),
    "max_vol": ("vol_60d", "<=", 0.01, "Max volatility", "%"),
}
TRENDS = {"up": "Uptrend", "down": "Downtrend", "mixed": "Mixed"}


def parse_float(v) -> float | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def filter_rows(rows: list[dict], params: dict) -> list[dict]:
    q = (params.get("q") or "").strip().lower()
    sector = params.get("sector") or ""
    out = []
    for r in rows:
        if q and q not in r["code"].lower() and q not in (r["name"] or "").lower():
            continue
        if sector and r["sector"] != sector:
            continue
        if params.get("trend") and r.get("trend") != params["trend"]:
            continue
        ok = True
        for p, (col, op, mult, _l, _u) in NUM_FILTERS.items():
            v = parse_float(params.get(p))
            if v is None:
                continue
            x = r.get(col)
            if x is None or (op == ">=" and x < v * mult) or (op == "<=" and x > v * mult):
                ok = False
                break
        if ok:
            out.append(r)
    return out


def sort_rows(rows: list[dict], key: str, desc: bool) -> list[dict]:
    if key not in COLUMNS:
        key = "market_cap"
    fmt = COLUMNS[key][1]
    present = [r for r in rows if r.get(key) is not None]
    missing = [r for r in rows if r.get(key) is None]
    if fmt == "text":
        present.sort(key=lambda r: str(r[key]).lower(), reverse=desc)
    else:
        present.sort(key=lambda r: r[key], reverse=desc)
    return present + missing   # blanks always last


# --- company page -------------------------------------------------------------

def get_company(c: sqlite3.Connection, code: str) -> dict | None:
    row = c.execute("SELECT * FROM companies WHERE code=?", (code,)).fetchone()
    if row is None:
        return None
    comp = dict(row)
    comp["symbol"] = comp.get("yahoo_symbol") or f"{code}.AX"
    comp["indices"] = [r[0] for r in c.execute(
        """SELECT m.index_code FROM index_membership m
           WHERE m.code=? AND m.as_of=(SELECT MAX(as_of) FROM index_membership WHERE index_code=m.index_code)""",
        (code,))]
    return comp


def returns_table(c: sqlite3.Connection, symbol: str, sector: str | None) -> list[dict]:
    """Price returns for stock vs its sector index vs the ASX 200, plus the stock's total return."""
    since = (date.today() - timedelta(days=int(1260 * 1.5))).isoformat()

    def series(sym: str, col: str) -> list[float]:
        return [r[0] for r in c.execute(
            f"SELECT COALESCE({col}, close) FROM prices WHERE symbol=? AND date>=? ORDER BY date", (sym, since))]

    lines = [("This stock (total return)", series(symbol, "adj_close")),
             ("This stock (price only)", series(symbol, "close"))]
    sec_sym = SECTOR_INDEX.get(sector or "")
    if sec_sym:
        lines.append((f"{sector} sector index", series(sec_sym, "close")))
    lines.append(("S&P/ASX 200 index", series(BENCHMARK, "close")))
    return [{"label": label, **{k: _ret(s, n) for k, n in PERIODS.items()}} for label, s in lines]


def peers(rows: list[dict], code: str, sector: str | None, limit: int = 10) -> tuple[list[dict], dict]:
    """Up to `limit` same-sector universe members nearest in market cap, plus sector medians."""
    same = [r for r in rows if r["sector"] == sector]
    me = next((r for r in same if r["code"] == code), None)
    others = [r for r in same if r["code"] != code]
    if me and me.get("market_cap"):
        others.sort(key=lambda r: abs((r.get("market_cap") or 0) - me["market_cap"]))
    else:
        others.sort(key=lambda r: -(r.get("market_cap") or 0))
    chosen = others[: limit - (1 if me else 0)] + ([me] if me else [])
    chosen.sort(key=lambda r: -(r.get("market_cap") or 0))
    med = {}
    for k in COLUMNS:
        vals = [r[k] for r in same if isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool)]
        med[k] = median(vals) if vals else None
    med["n"] = len(same)
    return chosen, med


def to_local_date(ts: str) -> str:
    """UTC ISO timestamp -> Sydney YYYY-MM-DD (plain dates pass through)."""
    if not ts:
        return ts
    if len(ts) == 10:
        return ts
    return datetime.fromisoformat(ts).astimezone(MARKET_TZ).date().isoformat()


def _snap(dates: list[str], d: str) -> str | None:
    """Snap an event date to the same or next trading day present in the price series."""
    i = bisect.bisect_left(dates, d)
    return dates[i] if i < len(dates) else None


def _r(v: float | None) -> float | None:
    """Trim float noise from Yahoo (60.38999938964844 -> 60.39) to keep the chart JSON small."""
    if v is None:
        return None
    return round(v, 4 if abs(v) < 1 else 3)


from app.analysis.indicators import period_key, period_labels  # noqa: E402  (pure, no cycle)


def aggregate_payload(payload: dict, interval: str) -> dict:
    """Daily chart payload -> weekly/monthly: OHLCV bars per period (dated by its first trading day),
    events moved onto their period's bar, shorts = last reading in each period."""
    if interval == "D" or not payload["ohlcv"]:
        return {**payload, "interval": "D"}
    labels = period_labels([b[0] for b in payload["ohlcv"]], interval)
    bars: dict[str, list] = {}
    for d, o, h, lo, cl, v in payload["ohlcv"]:
        if cl is None:
            continue
        k = labels[d]
        b = bars.get(k)
        if b is None:
            bars[k] = [k, o if o is not None else cl, h if h is not None else cl, lo if lo is not None else cl, cl, v]
            continue
        if h is not None:
            b[2] = max(b[2], h)
        if lo is not None:
            b[3] = min(b[3], lo)
        b[4] = cl
        if v is not None:
            b[5] = (b[5] or 0) + v
    events = [{**e, "date": labels.get(e["date"], e["date"])} for e in payload["events"]]
    # Short readings can fall on days without a price bar: bucket by period, keep the last reading,
    # and drop periods that have no price bar.
    bar_of_period = {period_key(k, interval): k for k in bars}
    shorts: dict[str, float] = {}
    for d, v in payload["shorts"]:
        k = bar_of_period.get(period_key(d, interval))
        if k:
            shorts[k] = v
    return {**payload, "interval": interval, "ohlcv": list(bars.values()), "events": events,
            "shorts": sorted([[k, v] for k, v in shorts.items()])}


def chart_payload(c: sqlite3.Connection, code: str, symbol: str) -> dict:
    """Compact arrays for the company chart + overlay events snapped to trading days."""
    bars = c.execute("SELECT date, open, high, low, close, volume FROM prices WHERE symbol=? ORDER BY date",
                     (symbol,)).fetchall()
    dates = [b["date"] for b in bars]
    payload: dict = {
        "code": code,
        "ohlcv": [[b["date"], _r(b["open"]), _r(b["high"]), _r(b["low"]), _r(b["close"]), b["volume"]] for b in bars],
        "events": [],
        "shorts": [],
    }
    if not dates:
        return payload
    first = dates[0]

    def add(kind: str, d: str, text: str, url: str | None = None, extra: dict | None = None):
        sd = _snap(dates, d)
        if sd and d >= first:
            ev = {"kind": kind, "date": sd, "text": text, "url": url}
            if extra:
                ev.update(extra)
            payload["events"].append(ev)

    for r in c.execute("SELECT ex_date, amount, currency, franking_pct FROM dividends WHERE code=? ORDER BY ex_date", (code,)):
        fr = f", {r['franking_pct']:.0f}% franked" if r["franking_pct"] is not None else ""
        cur = f" {r['currency']}" if r["currency"] and r["currency"] != "AUD" else ""
        add("dividend", r["ex_date"], f"Ex-dividend {r['amount']:.4g}{cur}{fr}")
    for r in c.execute("SELECT published_at, headline, url FROM announcements WHERE code=? AND price_sensitive=1 "
                       "ORDER BY published_at", (code,)):
        add("announcement", to_local_date(r["published_at"]), r["headline"], r["url"])
    for r in c.execute("SELECT ids_id, published_at, change_date, director, trade_type, acquired, disposed, value "
                       "FROM director_trades WHERE code=? AND trade_type IN ('buy','sell') ORDER BY published_at", (code,)):
        qty = r["acquired"] if r["trade_type"] == "buy" else r["disposed"]
        val = f" (A${r['value']:,.0f})" if r["value"] else ""
        d = r["change_date"] or to_local_date(r["published_at"])
        add(r["trade_type"], d, f"Director {r['trade_type']}: {r['director'] or '?'} {qty or 0:,.0f} shares{val}",
            f"https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId={r['ids_id']}")
    payload["events"].sort(key=lambda e: e["date"])
    payload["shorts"] = [[r[0], r[1]] for r in c.execute(
        "SELECT date, ROUND(short_pct, 3) FROM short_positions WHERE code=? AND short_pct IS NOT NULL ORDER BY date", (code,))]
    return payload

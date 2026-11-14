"""Paper trading: no real money, never a broker.

Rules (the same as the backtest engine):
- Market orders only. An order fills at the OPEN of the first trading day on or after its
  `exec_after` date: placed on a weekday before 10:00 Sydney -> that day's open, otherwise the
  next session. Fills are recorded once that day's prices arrive (daily update / `paper` job).
- Slippage (bps, against us) and brokerage (A$ per trade) on every fill. Whole shares.
  Buys can't exceed cash (they're cut down, or rejected below MIN_TRADE).
- Sells fill before buys on the same day. Buys "by amount" on the same day share the cash.
- Cash, positions and P&L are always replayed from the filled orders plus dividends, so
  nothing is stored that can drift out of sync. Dividends are credited on the ex-date to
  shares held at the previous close (USD-declared dividends converted at that day's AUD/USD).
- Prices are stored split-adjusted to today, so fill quantities are converted to today's
  share units with the splits that happened after the fill.

Kinds of portfolio:
- manual: you place orders.
- signals: follows the model exactly like the backtest's model strategy (hold the top-N by
  chance of beating the ASX 200, rebalance every ~20 trading days). It runs forward from the
  day it was created, so its record is genuinely out of sample.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd

from app.config import MARKET_TZ, get_settings
from app.db import utcnow

INDEX = "^AXJO"
BENCH = "STW.AX"
FX = "AUDUSD=X"
MIN_TRADE = 500.0
REBALANCE_DAYS = 20          # the backtest rebalances every 4 weekly samples


@dataclass
class Costs:
    brokerage: float = 5.0
    slippage_bps: float = 10.0

    @classmethod
    def from_settings(cls) -> "Costs":
        s = get_settings()
        return cls(s.brokerage_per_trade, s.slippage_bps)


@dataclass
class Position:
    code: str
    qty: float = 0.0           # in today's share units
    cost: float = 0.0          # A$ paid incl. fees for the shares still held (average cost)
    first_date: str | None = None
    dividends: float = 0.0


@dataclass
class Ledger:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    realised: float = 0.0
    fees: float = 0.0
    dividends: float = 0.0
    trades: int = 0


# --- helpers -------------------------------------------------------------------

def exec_after_for(now: datetime | None = None) -> str:
    now = (now or datetime.now(timezone.utc)).astimezone(MARKET_TZ)
    d = now.date()
    if not (now.weekday() < 5 and (now.hour, now.minute) < (10, 0)):
        d += timedelta(days=1)
    from app.market_calendar import next_trading_day
    return next_trading_day(d).isoformat()       # weekends and ASX holidays roll forward


def symbol_for(c: sqlite3.Connection, code: str) -> str:
    r = c.execute("SELECT yahoo_symbol FROM companies WHERE code=?", (code,)).fetchone()
    return (r and r[0]) or f"{code}.AX"


def get_portfolio(c: sqlite3.Connection, pid: int) -> dict | None:
    r = c.execute("SELECT * FROM paper_portfolios WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def portfolios(c: sqlite3.Connection, active_only: bool = True) -> list[dict]:
    q = "SELECT * FROM paper_portfolios" + (" WHERE active=1" if active_only else "") + " ORDER BY kind DESC, id"
    return [dict(r) for r in c.execute(q)]


def create_portfolio(c: sqlite3.Connection, name: str, kind: str = "manual", capital: float | None = None,
                     top_n: int | None = None, now: str | None = None) -> int:
    if kind not in ("manual", "signals"):
        raise ValueError("kind must be manual or signals")
    s = get_settings()
    capital = float(capital if capital is not None else s.paper_capital)
    if not (1_000 <= capital <= 100_000_000):
        raise ValueError("starting cash must be between A$1,000 and A$100M")
    return c.execute("INSERT INTO paper_portfolios(name, kind, capital, top_n, created_at) VALUES(?,?,?,?,?)",
                     (name.strip()[:60] or "Paper portfolio", kind, capital,
                      top_n if kind == "signals" else None, now or utcnow())).lastrowid


def ensure_signals_portfolio(c: sqlite3.Connection) -> int:
    row = c.execute("SELECT id FROM paper_portfolios WHERE kind='signals' AND active=1 ORDER BY id LIMIT 1").fetchone()
    if row:
        return row[0]
    n = get_settings().backtest_top_n
    return create_portfolio(c, f"Model signals (top {n}, forward test)", "signals", top_n=n)


def place_order(c: sqlite3.Connection, pid: int, code: str, side: str, quantity: int | None = None,
                amount: float | None = None, reason: str = "manual", signal_date: str | None = None,
                now: datetime | None = None) -> int:
    code = code.strip().upper().removesuffix(".AX")
    if side not in ("buy", "sell"):
        raise ValueError("side must be buy or sell")
    pf = get_portfolio(c, pid)
    if pf is None or not pf["active"]:
        raise ValueError("unknown portfolio")
    if not c.execute("SELECT 1 FROM companies WHERE code=?", (code,)).fetchone():
        raise ValueError(f"unknown ASX code {code}")
    if quantity is not None and quantity <= 0:
        raise ValueError("quantity must be positive")
    if amount is not None and amount <= 0:
        raise ValueError("amount must be positive")
    if side == "buy" and quantity is None and amount is None:
        raise ValueError("a buy needs a quantity or an amount")
    if side == "buy" and amount is not None and amount < MIN_TRADE:
        raise ValueError(f"minimum buy is A${MIN_TRADE:,.0f}")
    now = now or datetime.now(timezone.utc)
    return c.execute(
        "INSERT INTO paper_orders(portfolio_id, code, side, quantity, amount, placed_at, exec_after, reason, signal_date) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (pid, code, side, quantity, amount, now.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
         exec_after_for(now), reason, signal_date)).lastrowid


def cancel_order(c: sqlite3.Connection, oid: int) -> bool:
    return c.execute("UPDATE paper_orders SET status='cancelled', message='cancelled' "
                     "WHERE id=? AND status='pending'", (oid,)).rowcount > 0


def orders(c: sqlite3.Connection, pid: int, status: str | None = None, limit: int = 200) -> list[dict]:
    q = "SELECT * FROM paper_orders WHERE portfolio_id=?" + (" AND status=?" if status else "") + \
        " ORDER BY COALESCE(filled_date, exec_after) DESC, id DESC LIMIT ?"
    args = [pid] + ([status] if status else []) + [limit]
    return [dict(r) for r in c.execute(q, args)]


# --- replay ----------------------------------------------------------------------

def _splits(c, codes) -> dict[str, list[tuple[str, float]]]:
    out: dict[str, list] = {}
    if not codes:
        return out
    for r in c.execute(f"SELECT code, date, ratio FROM splits WHERE code IN ({','.join('?' * len(codes))}) "
                       f"AND ratio > 0", list(codes)):
        out.setdefault(r["code"], []).append((r["date"], r["ratio"]))
    return out


def _units_factor(splits: list[tuple[str, float]], after: str) -> float:
    """Shares bought on `after` -> today's units (prices are split-adjusted to today)."""
    f = 1.0
    for d, ratio in splits:
        if d > after:
            f *= ratio
    return f


def _fx_series(c, since: str) -> pd.Series:
    df = pd.read_sql_query("SELECT date, close FROM prices WHERE symbol=? AND date>=? ORDER BY date", c,
                           params=(FX, (pd.Timestamp(since) - pd.Timedelta(days=10)).date().isoformat()))
    return df.set_index("date")["close"].astype(float)


def _dividend_events(c, codes: set[str], since: str, upto: str | None) -> list[dict]:
    """Per-share dividends (today's share units, A$) with ex_date > since."""
    if not codes:
        return []
    q = (f"SELECT code, ex_date, amount, currency FROM dividends WHERE code IN ({','.join('?' * len(codes))}) "
         f"AND ex_date > ? AND amount > 0" + (" AND ex_date <= ?" if upto else "") + " ORDER BY ex_date")
    rows = [dict(r) for r in c.execute(q, [*codes, since] + ([upto] if upto else []))]
    if not rows:
        return []
    fx = None
    for r in rows:
        cur = r["currency"] or "AUD"      # the currency of `amount` (Yahoo .AX amounts are AUD)
        if cur == "USD":
            if fx is None:
                fx = _fx_series(c, since)
            rate = fx[fx.index <= r["ex_date"]]
            r["aud"] = r["amount"] / float(rate.iloc[-1]) if len(rate) else None
        else:
            r["aud"] = r["amount"]
    return [r for r in rows if r["aud"]]


def _events(c, pf: dict, upto: str | None = None) -> tuple[list[dict], dict]:
    fills = [dict(r) for r in c.execute(
        "SELECT * FROM paper_orders WHERE portfolio_id=? AND status='filled'" + (" AND filled_date<=?" if upto else ""),
        [pf["id"]] + ([upto] if upto else []))]
    codes = {f["code"] for f in fills}
    splits = _splits(c, codes)
    first = min((f["filled_date"] for f in fills), default=None)
    events = []
    for f in fills:
        f["units"] = f["filled_qty"] * _units_factor(splits.get(f["code"], []), f["filled_date"])
        # dividends before sells before buys on the same day
        events.append((f["filled_date"], 1 if f["side"] == "sell" else 2, f["id"], "fill", f))
    if first:
        for d in _dividend_events(c, codes, first, upto):
            events.append((d["ex_date"], 0, 0, "div", d))
    events.sort(key=lambda e: e[:3])
    return events, splits


def _apply(led: Ledger, kind: str, e: dict) -> None:
    if kind == "div":
        p = led.positions.get(e["code"])
        if p and p.qty > 0:
            amt = p.qty * e["aud"]
            p.dividends += amt
            led.dividends += amt
            led.cash += amt
        return
    p = led.positions.setdefault(e["code"], Position(e["code"]))
    led.fees += e["fee"]
    led.trades += 1
    if e["side"] == "buy":
        led.cash -= e["value"] + e["fee"]
        p.qty += e["units"]
        p.cost += e["value"] + e["fee"]
        p.first_date = p.first_date or e["filled_date"]
    else:
        frac = min(1.0, e["units"] / p.qty) if p.qty else 1.0
        cost_out = p.cost * frac
        led.cash += e["value"] - e["fee"]
        led.realised += e["value"] - e["fee"] - cost_out
        p.cost -= cost_out
        p.qty -= e["units"]
        if p.qty <= 1e-6:
            del led.positions[e["code"]]


def replay(c: sqlite3.Connection, pf: dict, upto: str | None = None) -> Ledger:
    """State after all fills and dividends up to and including market date `upto`."""
    led = Ledger(cash=pf["capital"])
    events, _ = _events(c, pf, upto)
    for *_, kind, e in events:
        _apply(led, kind, e)
    return led


# --- fills -----------------------------------------------------------------------

def _bar_on_or_after(c, symbol: str, day: str) -> dict | None:
    r = c.execute("SELECT date, open, close FROM prices WHERE symbol=? AND date>=? AND close IS NOT NULL "
                  "ORDER BY date LIMIT 1", (symbol, day)).fetchone()
    return dict(r) if r else None


def fill_pending(c: sqlite3.Connection, pid: int, costs: Costs | None = None) -> dict:
    """Fill every pending order whose execution bar exists. Returns counts."""
    costs = costs or Costs.from_settings()
    pf = get_portfolio(c, pid)
    pend = [dict(r) for r in c.execute("SELECT * FROM paper_orders WHERE portfolio_id=? AND status='pending'", (pid,))]
    todo = []
    for o in pend:
        bar = _bar_on_or_after(c, symbol_for(c, o["code"]), o["exec_after"])
        if bar is None:
            continue                                   # day not in yet (or suspended): stays pending
        todo.append((bar["date"], 0 if o["side"] == "sell" else 1, o["id"], o, bar))
    todo.sort(key=lambda t: t[:3])
    out = {"filled": 0, "rejected": 0}
    slip = costs.slippage_bps / 10_000
    for i, (day, _, _, o, bar) in enumerate(todo):
        led = replay(c, pf, upto=day)
        px = bar["open"] if bar["open"] and bar["open"] > 0 else bar["close"]
        note = "" if bar["open"] and bar["open"] > 0 else "no opening price: filled at the close"
        status, qty, value, msg = "filled", 0, 0.0, note
        if o["side"] == "sell":
            held = led.positions.get(o["code"])
            have = math.floor(held.qty + 1e-6) if held else 0
            qty = min(o["quantity"] or have, have)
            if qty <= 0:
                status, msg = "rejected", "nothing to sell"
            else:
                value = qty * px * (1 - slip)
        else:
            p = px * (1 + slip)
            if o["quantity"]:
                qty = o["quantity"]
                if qty * p + costs.brokerage > led.cash:
                    qty = math.floor(max(0.0, led.cash - costs.brokerage) / p)
                    msg = "cut to the cash available"
            else:
                # buys by amount on the same day share the remaining cash evenly (as in the backtest)
                same_day = sum(1 for t in todo[i:] if t[0] == day and t[3]["side"] == "buy" and not t[3]["quantity"])
                budget = min(o["amount"], led.cash / max(same_day, 1)) - costs.brokerage
                qty = math.floor(max(0.0, budget) / p)
            value = qty * p
            if qty <= 0 or (not o["quantity"] and value < MIN_TRADE):
                msg = (f"not enough cash (A${led.cash:,.0f} available)" if led.cash < MIN_TRADE + costs.brokerage
                       or qty <= 0 else f"below the A${MIN_TRADE:,.0f} minimum")
                status, qty, value = "rejected", 0, 0.0
        if status == "filled":
            c.execute("UPDATE paper_orders SET status='filled', filled_date=?, price=?, filled_qty=?, fee=?, value=?, "
                      "message=? WHERE id=?", (day, value / qty, qty, costs.brokerage, value, msg or None, o["id"]))
            out["filled"] += 1
        else:
            c.execute("UPDATE paper_orders SET status='rejected', filled_date=?, message=? WHERE id=?",
                      (day, msg, o["id"]))
            out["rejected"] += 1
    return out


# --- the signal-following portfolio ------------------------------------------------

def rebalance_signals(c: sqlite3.Connection, pid: int, now: datetime | None = None) -> str:
    """Follow the latest forecasts like the backtest's model strategy. Returns a note."""
    pf = get_portfolio(c, pid)
    last = c.execute("SELECT MAX(as_of_date) FROM forecasts WHERE horizon=21").fetchone()[0]
    if not last:
        return "no forecasts yet"
    if c.execute("SELECT 1 FROM paper_orders WHERE portfolio_id=? AND status='pending'", (pid,)).fetchone():
        return "waiting for pending orders to fill"
    if pf["last_rebalance"]:
        n = c.execute("SELECT COUNT(*) FROM prices WHERE symbol=? AND date>? AND date<=?",
                      (INDEX, pf["last_rebalance"], last)).fetchone()[0]
        if n < REBALANCE_DAYS:
            return f"next rebalance in {REBALANCE_DAYS - n} trading days"
    n_top = pf["top_n"] or get_settings().backtest_top_n
    target = [r[0] for r in c.execute(
        "SELECT code FROM forecasts WHERE as_of_date=? AND horizon=21 AND eligible=1 AND rank IS NOT NULL "
        "AND rank<=? ORDER BY rank", (last, n_top))]
    if not target:
        return "no eligible forecasts"
    led = replay(c, pf)
    held = set(led.positions)
    value = led.cash + sum(p.qty * (_last_close(c, symbol_for(c, k)) or 0) for k, p in led.positions.items())
    for code in sorted(held - set(target)):
        place_order(c, pid, code, "sell", reason=f"left the top {n_top}", signal_date=last, now=now)
    for code in [t for t in target if t not in held]:
        place_order(c, pid, code, "buy", amount=max(value / n_top, MIN_TRADE), signal_date=last, now=now,
                    reason=f"entered the top {n_top}")
    c.execute("UPDATE paper_portfolios SET last_rebalance=? WHERE id=?", (last, pid))
    return f"rebalanced to forecasts of {last}: {len(held - set(target))} sells, {len(set(target) - held)} buys"


def _last_close(c, symbol: str, on: str | None = None) -> float | None:
    q = "SELECT close FROM prices WHERE symbol=? AND close IS NOT NULL" + (" AND date<=?" if on else "") + \
        " ORDER BY date DESC LIMIT 1"
    r = c.execute(q, (symbol, on) if on else (symbol,)).fetchone()
    return r[0] if r else None


# --- valuation -------------------------------------------------------------------

def start_date(c: sqlite3.Connection, pf: dict) -> str:
    """First market date the portfolio could trade (the session after creation)."""
    return exec_after_for(datetime.fromisoformat(pf["created_at"]))


def equity_curve(c: sqlite3.Connection, pf: dict) -> pd.Series:
    """Daily value (close prices, dividends credited) from the first trading day to the latest close."""
    start = start_date(c, pf)
    cal = [r[0] for r in c.execute("SELECT date FROM prices WHERE symbol=? AND date>=? ORDER BY date", (INDEX, start))]
    if not cal:
        return pd.Series(dtype=float)
    events, _ = _events(c, pf)
    codes = sorted({e[4]["code"] for e in events})
    closes = pd.DataFrame(index=cal)
    if codes:
        sym = {symbol_for(c, k): k for k in codes}
        df = pd.read_sql_query(f"SELECT symbol, date, close FROM prices WHERE symbol IN ({','.join('?' * len(sym))}) "
                               f"AND date>=?", c, params=[*sym, start])
        if not df.empty:
            closes = (df.assign(code=df["symbol"].map(sym)).pivot_table(index="date", columns="code", values="close")
                      .reindex(sorted(set(cal) | set(df["date"]))).ffill().reindex(cal))
    led = Ledger(cash=pf["capital"])
    vals, k = [], 0
    for d in cal:
        while k < len(events) and events[k][0] <= d:
            _apply(led, events[k][3], events[k][4])
            k += 1
        row = closes.loc[d] if len(closes.columns) else {}
        v = led.cash + sum(p.qty * float(row.get(code, 0) or 0) for code, p in led.positions.items())
        vals.append(v)
    return pd.Series(vals, index=cal, dtype=float)


def benchmark_curve(c: sqlite3.Connection, pf: dict, costs: Costs | None = None) -> pd.Series:
    """STW (ASX 200 ETF, dividends reinvested) bought at the same first open with the same costs."""
    from app.models.backtest import Config, buy_and_hold
    costs = costs or Costs.from_settings()
    start = start_date(c, pf)
    df = pd.read_sql_query("SELECT date, open, close, adj_close FROM prices WHERE symbol=? AND date>=? ORDER BY date",
                           c, params=(BENCH, start))
    if df.empty:
        return pd.Series(dtype=float)
    df = df.set_index("date").astype(float)
    dates = list(df.index)
    cfg = Config(capital=pf["capital"], brokerage=costs.brokerage, slippage_bps=costs.slippage_bps)
    return buy_and_hold("bench", df["adj_close"], df["open"], df["close"], dates, cfg, dates[0]).equity


def summary(c: sqlite3.Connection, pf: dict) -> dict:
    led = replay(c, pf)
    pos = []
    sig = {r["code"]: dict(r) for r in c.execute(
        "SELECT code, signal, p_beat, rank FROM forecasts WHERE horizon=21 AND as_of_date=(SELECT MAX(as_of_date) FROM forecasts)")}
    names = {r[0]: r[1] for r in c.execute("SELECT code, name FROM companies")}
    for code, p in sorted(led.positions.items()):
        px = _last_close(c, symbol_for(c, code))
        value = p.qty * (px or 0)
        pos.append({"code": code, "name": names.get(code), "qty": p.qty, "avg_cost": p.cost / p.qty if p.qty else None,
                    "cost": p.cost, "price": px, "value": value, "pnl": value - p.cost,
                    "pnl_pct": value / p.cost - 1 if p.cost else None, "dividends": p.dividends,
                    "since": p.first_date, "signal": (sig.get(code) or {}).get("signal")})
    invested = sum(p["value"] for p in pos)
    total = led.cash + invested
    for p in pos:
        p["weight"] = p["value"] / total if total else None
    curve = equity_curve(c, pf)
    bench = benchmark_curve(c, pf)
    out = {"cash": led.cash, "invested": invested, "value": total, "pnl": total - pf["capital"],
           "ret": total / pf["capital"] - 1, "realised": led.realised, "fees": led.fees, "dividends": led.dividends,
           "trades": led.trades, "positions": pos, "start": start_date(c, pf),
           "days": len(curve), "curve": curve, "bench": bench}
    if len(bench):
        out["bench_ret"] = float(bench.iloc[-1] / pf["capital"] - 1)
    if len(curve):
        out["max_dd"] = float((curve / curve.cummax() - 1).min())
    return out


def step_paper(ctx) -> None:
    """Fill pending orders, then let the signal portfolio follow the latest forecasts."""
    from app.db import get_conn
    with get_conn() as c:
        has_fc = c.execute("SELECT 1 FROM forecasts LIMIT 1").fetchone()
        if has_fc:
            ensure_signals_portfolio(c)
        pfs = portfolios(c)
    for pf in pfs:
        with get_conn() as c:
            r = fill_pending(c, pf["id"])
            ctx.items += r["filled"]
            if r["filled"] or r["rejected"]:
                ctx.notes.append(f"{pf['name']}: {r['filled']} filled, {r['rejected']} rejected")
            if pf["kind"] == "signals":
                ctx.notes.append(f"{pf['name']}: {rebalance_signals(c, pf['id'])}")

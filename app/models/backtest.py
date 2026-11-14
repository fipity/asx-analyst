"""Portfolio backtest engine (long-only, top-N, monthly rebalance).

Rules
- Signals are computed at the close of a rebalance date t; orders execute at the OPEN of
  the next trading day, with slippage (bps, against us) and brokerage (A$ per trade).
- Positions not in the new top-N are sold in full; new names are bought with a target of
  portfolio value / N (limited by cash). Kept positions are not topped up (fewer fees).
- Holdings are valued on dividend-adjusted prices (dividends reinvested). Execution uses
  open * adj_close / close, so trades and valuation are in the same adjusted units.
- Whole shares only; cash earns nothing (conservative).
- No price on the execution day (suspended): that trade is skipped.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class Config:
    capital: float = 50_000.0
    top_n: int = 10
    brokerage: float = 5.0
    slippage_bps: float = 10.0
    min_trade: float = 500.0


@dataclass
class Result:
    name: str
    equity: pd.Series                     # daily portfolio value, index = date
    trades: int = 0
    fees: float = 0.0
    traded_value: float = 0.0
    picks: dict = field(default_factory=dict)   # rebalance date -> list of codes


def load_prices(c, symbols: dict[str, str], start: str, end: str | None = None) -> dict[str, pd.DataFrame]:
    """symbols: code -> yahoo symbol. Returns {'open','adj','close'} pivots (date x code)."""
    ph = ",".join("?" * len(symbols))
    q = (f"SELECT symbol, date, open, close, adj_close FROM prices WHERE symbol IN ({ph}) AND date >= ?"
         + (" AND date <= ?" if end else ""))
    args = [*symbols.values(), start] + ([end] if end else [])
    df = pd.read_sql_query(q, c, params=args)
    inv = {v: k for k, v in symbols.items()}
    df["code"] = df["symbol"].map(inv)
    out = {}
    for col, key in (("open", "open"), ("close", "close"), ("adj_close", "adj")):
        out[key] = df.pivot_table(index="date", columns="code", values=col, aggfunc="last").sort_index()
    return out


def run(name: str, picks: dict[str, list[str]], px: dict[str, pd.DataFrame], dates: list[str], cfg: Config) -> Result:
    """picks: rebalance date -> ordered codes to hold. dates: trading calendar for the run."""
    adj = px["adj"].reindex(dates).ffill()
    factor = (px["adj"] / px["close"]).reindex(dates)
    open_adj = (px["open"].reindex(dates) * factor)
    open_adj = open_adj.where(open_adj > 0)
    cash = cfg.capital
    shares: dict[str, float] = {}
    res = Result(name, pd.Series(dtype=float))
    slip = cfg.slippage_bps / 10_000
    values = []
    pending: list[str] | None = None
    for i, d in enumerate(dates):
        if pending is not None:
            row_open = open_adj.loc[d]
            row_close = adj.loc[d]
            prices = row_open.fillna(px["adj"].reindex([d]).iloc[0])     # no open -> that day's close
            target = set(pending)
            for code in [k for k in shares if k not in target]:
                p = prices.get(code)
                if p is None or not np.isfinite(p):
                    continue          # suspended: can't sell today
                proceeds = shares[code] * p * (1 - slip)
                cash += proceeds - cfg.brokerage
                res.fees += cfg.brokerage
                res.trades += 1
                res.traded_value += proceeds
                del shares[code]
            # value at today's execution prices (open; last close if no open)
            pv = cash + sum(n * (prices.get(k) if np.isfinite(prices.get(k, np.nan))
                                 else row_close.get(k, 0.0)) for k, n in shares.items())
            new = [k for k in pending if k not in shares]
            if new:
                budget_each = min(pv / cfg.top_n, cash / len(new))
                for code in new:
                    p = prices.get(code)
                    if p is None or not np.isfinite(p) or p <= 0:
                        continue
                    spend = min(budget_each, cash) - cfg.brokerage
                    if spend < cfg.min_trade:
                        continue
                    n = math.floor(spend / (p * (1 + slip)))
                    if n <= 0:
                        continue
                    cost = n * p * (1 + slip)
                    cash -= cost + cfg.brokerage
                    res.fees += cfg.brokerage
                    res.trades += 1
                    res.traded_value += cost
                    shares[code] = n
            pending = None
        row = adj.loc[d]
        values.append(cash + sum(n * row.get(k, 0.0) for k, n in shares.items() if np.isfinite(row.get(k, np.nan))))
        if d in picks:
            pending = list(picks[d])[: cfg.top_n]
            res.picks[d] = pending
    res.equity = pd.Series(values, index=dates, dtype=float)
    return res


def buy_and_hold(name: str, symbol_adj: pd.Series, symbol_open: pd.Series, symbol_close: pd.Series,
                 dates: list[str], cfg: Config, start_exec: str) -> Result:
    """Buy once at the open of start_exec (one brokerage), hold to the end."""
    adj = symbol_adj.reindex(dates).ffill()
    factor = (symbol_adj / symbol_close).reindex(dates)
    p0 = (symbol_open.reindex(dates) * factor).get(start_exec)
    if p0 is None or not np.isfinite(p0) or p0 <= 0:
        p0 = adj.get(start_exec)
    n = math.floor((cfg.capital - cfg.brokerage) / (p0 * (1 + cfg.slippage_bps / 10_000)))
    cash = cfg.capital - cfg.brokerage - n * p0 * (1 + cfg.slippage_bps / 10_000)
    eq = pd.Series(np.where(np.array(dates) < start_exec, cfg.capital, cash + n * adj.values), index=dates)
    return Result(name, eq, trades=1, fees=cfg.brokerage, traded_value=n * p0)


def metrics(res: Result, bench: pd.Series | None, rf_annual: pd.Series | None, rebalance: list[str]) -> dict:
    eq = res.equity.dropna()
    if len(eq) < 2:
        return {}
    d0, d1 = pd.Timestamp(eq.index[0]), pd.Timestamp(eq.index[-1])
    years = max((d1 - d0).days / 365.25, 1e-9)
    total = eq.iloc[-1] / eq.iloc[0] - 1
    r = eq.pct_change().dropna()
    rf = (rf_annual.reindex(r.index).ffill().fillna(0) / 100 / 252) if rf_annual is not None else 0
    ex = r - rf
    dd = eq / eq.cummax() - 1
    out = {
        "start": eq.index[0], "end": eq.index[-1], "final_value": float(eq.iloc[-1]), "total_return": float(total),
        "cagr": float((1 + total) ** (1 / years) - 1), "volatility": float(r.std() * np.sqrt(252)),
        "sharpe": float(ex.mean() / ex.std() * np.sqrt(252)) if ex.std() > 0 else None,
        "max_drawdown": float(dd.min()), "trades": res.trades, "fees": round(res.fees, 2),
        "turnover": float(res.traded_value / eq.mean() / years) if eq.mean() else None,
    }
    if bench is not None and rebalance:
        b = bench.reindex(eq.index).ffill()
        pts = [d for d in rebalance if d in eq.index] + [eq.index[-1]]
        wins = [(eq[b1] / eq[a] - 1) > (b[b1] / b[a] - 1) for a, b1 in zip(pts, pts[1:]) if a != b1]
        out["hit_rate_vs_bench"] = float(np.mean(wins)) if wins else None
        out["excess_cagr"] = out["cagr"] - float((b.iloc[-1] / b.iloc[0]) ** (1 / years) - 1)
    return out


def top_n_picks(scores: pd.DataFrame, score_col: str, rebalance: list[str], n: int, higher_is_better=True) -> dict:
    """scores: rows with code, date, eligible, <score_col>. Returns rebalance date -> top-N codes."""
    out = {}
    s = scores[scores["eligible"] & scores[score_col].notna()]
    by_date = {d: g for d, g in s.groupby("date")}
    for d in rebalance:
        g = by_date.get(d)
        if g is None or g.empty:
            continue
        g = g.sort_values([score_col, "code"], ascending=[not higher_is_better, True])
        out[d] = g["code"].head(n).tolist()
    return out


def random_picks(scores: pd.DataFrame, rebalance: list[str], n: int, seed: int) -> dict:
    """Survivorship control: N eligible stocks chosen at random at each rebalance."""
    rng = np.random.default_rng(seed)
    s = scores[scores["eligible"]]
    by_date = {d: sorted(g["code"]) for d, g in s.groupby("date")}
    return {d: list(rng.choice(by_date[d], size=min(n, len(by_date[d])), replace=False))
            for d in rebalance if by_date.get(d)}

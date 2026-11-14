"""Your real holdings, entered by hand (no broker connection).

- Trades: buys and sells with date, shares, price and brokerage. Positions, parcels, P&L and
  dividends are always recalculated from the trades.
- Parcels are matched first-in, first-out when you sell. (For tax you may choose which parcel
  you sell; FIFO is the default shown here. Check with the ATO or your accountant.)
- CGT discount: individuals get the 50% discount on a gain if the shares were held at least
  12 months, not counting the days bought and sold, so the earliest discount day is
  buy date + 1 year + 1 day.
- Dividends: per share from the dividends table (split-adjusted to today's units, USD-declared
  ones converted at the AUD/USD rate on the ex-date) on the shares held at the close before
  each ex-date. Franking credits assume a 30% company tax rate.
- Prices are split-adjusted to today, so share counts are converted to today's units with the
  splits after each trade.
- "Same money in the ASX 200": every buy also buys STW (dividends reinvested) and every sell
  sells the same fraction, so the comparison includes timing, dividends and sales.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.db import utcnow
from app.portfolio.paper import BENCH, _fx_series, _last_close, _splits, _units_factor, symbol_for

FRANKING_TAX_RATE = 0.30
MAX_STOCK_WEIGHT = 0.20
MAX_SECTOR_WEIGHT = 0.40


def discount_date(buy_date: str) -> str:
    d = date.fromisoformat(buy_date)
    try:
        anniv = d.replace(year=d.year + 1)
    except ValueError:            # 29 Feb
        anniv = d.replace(year=d.year + 1, day=28) + timedelta(days=1)
    return (anniv + timedelta(days=1)).isoformat()


@dataclass
class Lot:
    trade_id: int
    code: str
    buy_date: str
    units: float              # remaining, today's units
    orig_units: float
    cost_per_unit: float      # incl. brokerage
    stw_units: float = 0.0    # mirror STW units still held for this parcel

    @property
    def cost(self) -> float:
        return self.units * self.cost_per_unit


@dataclass
class Book:
    lots: list[Lot] = field(default_factory=list)
    realised: list[dict] = field(default_factory=list)
    proceeds: float = 0.0
    invested: float = 0.0
    stw_cash: float = 0.0
    unit_events: list[tuple[str, str, float]] = field(default_factory=list)   # (code, date, +/- today's units)


SIDES = {
    "buy": "Buy",
    "sell": "Sell",
    "drp": "DRP (dividend reinvested)",
    "return": "Capital return",
    "demerger": "Demerger",
}
_ORDER = {"buy": 0, "drp": 0, "return": 1, "demerger": 1, "sell": 2}


def trades(c: sqlite3.Connection) -> list[dict]:
    rows = [dict(r) for r in c.execute("SELECT * FROM holding_trades")]
    return sorted(rows, key=_key)


def _key(t: dict):
    return t["trade_date"], _ORDER.get(t["side"], 3), t.get("id") or 0


def _stw_adj(c, day: str) -> float | None:
    r = c.execute("SELECT adj_close FROM prices WHERE symbol=? AND date<=? AND adj_close IS NOT NULL "
                  "ORDER BY date DESC LIMIT 1", (BENCH, day)).fetchone()
    if r is None:   # before STW history: first available
        r = c.execute("SELECT adj_close FROM prices WHERE symbol=? AND adj_close IS NOT NULL ORDER BY date LIMIT 1",
                      (BENCH,)).fetchone()
    return r[0] if r else None


def build(c: sqlite3.Connection, rows: list[dict] | None = None) -> Book:
    """Replay trades and corporate actions (FIFO). Raises ValueError on an impossible sequence.

    - buy / drp: a new parcel (a DRP is a buy at the DRP price, usually without brokerage).
    - sell: removes parcels first-in, first-out; realised gain per parcel.
    - return: a capital return of `price` A$ per share on `quantity` shares; lowers the cost base of the
      parcels held (shares unchanged). Any amount above a parcel's cost base is a capital gain.
    - demerger: `ratio` new `other_code` shares per share held; `cost_pct` % of each parcel's cost base
      moves to the new shares, which keep the original purchase dates (for the CGT discount).
    """
    rows = trades(c) if rows is None else sorted(rows, key=_key)
    codes = {t["code"] for t in rows} | {t["other_code"] for t in rows if t.get("other_code")}
    splits = _splits(c, codes)
    f = lambda code, day: _units_factor(splits.get(code, []), day)   # noqa: E731
    book = Book()
    for t in rows:
        side, code, day = t["side"], t["code"], t["trade_date"]
        units = t["quantity"] * f(code, day)
        adj = _stw_adj(c, day)
        open_lots = [l for l in book.lots if l.code == code and l.units > 1e-9]
        held = sum(l.units for l in open_lots)
        if side in ("buy", "drp"):
            total = t["quantity"] * t["price"] + t["fees"]
            book.invested += total
            book.lots.append(Lot(t.get("id") or 0, code, day, units, units, total / units, total / adj if adj else 0.0))
            book.unit_events.append((code, day, units))
            continue
        if held <= 1e-9:
            raise ValueError(f"no {code} shares held on {day} for this {SIDES.get(side, side).lower()}")
        if side == "return":
            if units > held + 1e-6:
                raise ValueError(f"capital return on {t['quantity']:g} {code} shares on {day}, but only {held:g} held then")
            cash = t["quantity"] * t["price"]
            book.proceeds += cash
            for lot in open_lots:
                part = cash * lot.units / held
                cut = min(part, lot.cost)
                if part > lot.cost + 1e-9:      # excess over the cost base is a capital gain now
                    gain = part - lot.cost
                    book.realised.append({"code": code, "sell_date": day, "buy_date": lot.buy_date, "units": 0.0,
                                          "cost": 0.0, "proceeds": gain, "gain": gain,
                                          "discount": day >= discount_date(lot.buy_date), "sell_id": t.get("id"),
                                          "lot_id": lot.trade_id, "kind": "capital return above cost base"})
                lot.cost_per_unit -= cut / lot.units
                if adj and lot.stw_units:       # mirror: take the same cash out of STW
                    take = min(lot.stw_units, part / adj)
                    book.stw_cash += take * adj
                    lot.stw_units -= take
            continue
        if side == "demerger":
            child = t.get("other_code")
            ratio, pct = t.get("ratio") or 0, (t.get("cost_pct") or 0) / 100
            if not child or ratio <= 0 or not 0 < pct < 1:
                raise ValueError("a demerger needs the new company's code, the new shares per share held, and the "
                                 "% of cost base moved to them (between 0 and 100)")
            for lot in open_lots:
                shares_then = lot.units / f(code, day)            # actual parent shares held on that day
                child_units = shares_then * ratio * f(child, day)
                moved = lot.cost * pct
                lot.cost_per_unit *= 1 - pct
                stw_moved = lot.stw_units * pct
                lot.stw_units -= stw_moved
                book.lots.append(Lot(lot.trade_id, child, lot.buy_date, child_units, child_units,
                                     moved / child_units if child_units else 0.0, stw_moved))
                book.unit_events.append((child, day, child_units))
            continue
        # sell
        if units > held + 1e-6:
            raise ValueError(f"selling {t['quantity']:g} {code} on {day}, but only {held:g} held then (today's share units)")
        net = t["quantity"] * t["price"] - t["fees"]
        book.proceeds += net
        book.unit_events.append((code, day, -units))
        left = units
        for lot in open_lots:
            if left <= 1e-9:
                break
            take = min(lot.units, left)
            frac_of_sale = take / units
            frac_of_lot = take / lot.units
            cost = take * lot.cost_per_unit
            gain = net * frac_of_sale - cost
            elig = day >= discount_date(lot.buy_date)
            book.realised.append({"code": code, "sell_date": day, "buy_date": lot.buy_date,
                                  "units": take, "cost": cost, "proceeds": net * frac_of_sale, "gain": gain,
                                  "discount": elig and gain > 0, "sell_id": t.get("id"), "lot_id": lot.trade_id,
                                  "kind": "sale"})
            if adj:
                book.stw_cash += lot.stw_units * frac_of_lot * adj
            lot.stw_units *= 1 - frac_of_lot
            lot.units -= take
            left -= take
    return book


def validate(c: sqlite3.Connection, new: dict) -> None:
    """Check a new trade or corporate action against the existing ones (raises ValueError)."""
    if new["side"] not in SIDES:
        raise ValueError("unknown trade type")
    for k in ("code",) + (("other_code",) if new["side"] == "demerger" else ()):
        if not new.get(k) or not c.execute("SELECT 1 FROM companies WHERE code=?", (new[k],)).fetchone():
            raise ValueError(f"unknown ASX code {new.get(k) or '(empty)'}")
    if new["side"] == "demerger" and new["other_code"] == new["code"]:
        raise ValueError("the demerged company must be a different code")
    try:
        d = date.fromisoformat(new["trade_date"])
    except ValueError:
        raise ValueError("date must be YYYY-MM-DD")
    if d > date.today():
        raise ValueError("the date is in the future")
    if d.year < 1980:
        raise ValueError("the date is too far back")
    if not new["quantity"] or new["quantity"] <= 0:
        raise ValueError("shares must be positive")
    if new["side"] != "demerger" and (not new["price"] or new["price"] <= 0):
        raise ValueError("price must be positive")
    if new["fees"] < 0:
        raise ValueError("brokerage can't be negative")
    build(c, trades(c) + [{**new, "id": 10**9}])


def add_trade(c: sqlite3.Connection, code: str, side: str, trade_date: str, quantity: float, price: float,
              fees: float, note: str = "", other_code: str | None = None, ratio: float | None = None,
              cost_pct: float | None = None) -> int:
    t = {"code": code.strip().upper().removesuffix(".AX"), "side": side, "trade_date": trade_date,
         "quantity": float(quantity), "price": float(price or 0), "fees": float(fees or 0),
         "other_code": (other_code or "").strip().upper().removesuffix(".AX") or None,
         "ratio": float(ratio) if ratio else None, "cost_pct": float(cost_pct) if cost_pct else None}
    if side in ("return", "demerger"):
        t["fees"] = 0.0
    validate(c, t)
    cols = ["code", "side", "trade_date", "quantity", "price", "fees", "other_code", "ratio", "cost_pct"]
    return c.execute(f"INSERT INTO holding_trades({','.join(cols)}, note, created_at) VALUES({','.join('?' * (len(cols) + 2))})",
                     [t[k] for k in cols] + [(note or "").strip()[:200] or None, utcnow()]).lastrowid


def delete_trade(c: sqlite3.Connection, tid: int) -> None:
    rest = [t for t in trades(c) if t["id"] != tid]
    build(c, rest)        # deleting a buy that later sells depend on is refused
    c.execute("DELETE FROM holding_trades WHERE id=?", (tid,))


def held_codes(c: sqlite3.Connection) -> list[str]:
    try:
        book = build(c)
    except ValueError:
        return []
    return sorted({l.code for l in book.lots if l.units > 1e-6})


def dividends(c: sqlite3.Connection, rows: list[dict] | None = None, today: str | None = None,
              book: Book | None = None) -> list[dict]:
    """Dividends on the shares held at the close before each ex-date (incl. DRP and demerged shares)."""
    rows = trades(c) if rows is None else rows
    if not rows:
        return []
    book = book or build(c, rows)
    today = today or date.today().isoformat()
    codes = sorted({code for code, _, _ in book.unit_events})
    first = min(t["trade_date"] for t in rows)
    divs = [dict(r) for r in c.execute(
        f"SELECT code, ex_date, amount, currency, franking_pct, pay_date FROM dividends WHERE code IN "
        f"({','.join('?' * len(codes))}) AND ex_date > ? AND amount > 0 ORDER BY ex_date", [*codes, first])]
    if not divs:
        return []
    from app.analysis.market import latest_fundamentals
    fund_fr = {k: v.get("franking_pct") for k, v in latest_fundamentals(c, codes).items()}
    fx = None
    out = []
    for d in divs:
        units = sum(delta for code, day, delta in book.unit_events if code == d["code"] and day < d["ex_date"])
        if units <= 1e-6:
            continue
        cur = d["currency"] or "AUD"      # the currency of `amount`
        per = d["amount"]
        if cur == "USD":
            fx = _fx_series(c, first) if fx is None else fx
            r = fx[fx.index <= d["ex_date"]]
            if not len(r):
                continue
            per = d["amount"] / float(r.iloc[-1])
        cash = units * per
        est = d["franking_pct"] is None
        fr = (d["franking_pct"] if not est else (fund_fr.get(d["code"]) or 0)) / 100
        out.append({**d, "currency": cur, "units": units, "per_share_aud": per, "cash": cash,
                    "franking_credit": cash * fr * FRANKING_TAX_RATE / (1 - FRANKING_TAX_RATE),
                    "franking_estimated": est, "received": (d["pay_date"] or d["ex_date"]) <= today})
    return out


def summary(c: sqlite3.Connection, today: str | None = None) -> dict:
    today = today or date.today().isoformat()
    rows = trades(c)
    book = build(c, rows)
    divs = dividends(c, rows, today, book)
    open_lots = [l for l in book.lots if l.units > 1e-6]
    codes = sorted({l.code for l in open_lots})
    comp = {r["code"]: dict(r) for r in c.execute(
        f"SELECT code, name, sector FROM companies WHERE code IN ({','.join('?' * len(codes))})", codes)} if codes else {}
    last_fc = c.execute("SELECT MAX(as_of_date) FROM forecasts").fetchone()[0]
    sig = {r["code"]: dict(r) for r in c.execute(
        "SELECT code, signal, confidence, p_beat FROM forecasts WHERE horizon=21 AND as_of_date=?", (last_fc,))} if last_fc else {}
    positions = []
    for code in codes:
        lots = [l for l in open_lots if l.code == code]
        px = _last_close(c, symbol_for(c, code))
        units = sum(l.units for l in lots)
        cost = sum(l.cost for l in lots)
        value = units * (px or 0)
        dv = [d for d in divs if d["code"] == code and d["received"]]
        lot_rows = []
        for l in lots:
            dd = discount_date(l.buy_date)
            lot_value = l.units * (px or 0)
            lot_rows.append({"trade_id": l.trade_id, "buy_date": l.buy_date, "units": l.units, "cost": l.cost,
                             "value": lot_value, "gain": lot_value - l.cost, "discount_date": dd,
                             "eligible": today >= dd, "days_to_discount": max(0, (date.fromisoformat(dd) - date.fromisoformat(today)).days)})
        s = sig.get(code) or {}
        positions.append({"code": code, "name": (comp.get(code) or {}).get("name"), "sector": (comp.get(code) or {}).get("sector") or "Unknown",
                          "units": units, "cost": cost, "avg_cost": cost / units if units else None, "price": px,
                          "value": value, "pnl": value - cost, "pnl_pct": value / cost - 1 if cost else None,
                          "dividends": sum(d["cash"] for d in dv), "franking": sum(d["franking_credit"] for d in dv),
                          "signal": s.get("signal"), "confidence": s.get("confidence"), "lots": lot_rows,
                          "first_buy": min(l.buy_date for l in lots)})
    total = sum(p["value"] for p in positions)
    sectors: dict[str, float] = {}
    for p in positions:
        p["weight"] = p["value"] / total if total else None
        sectors[p["sector"]] = sectors.get(p["sector"], 0) + p["value"]
    sector_rows = sorted(({"sector": k, "value": v, "weight": v / total if total else None} for k, v in sectors.items()),
                         key=lambda r: -r["value"])
    warnings = [f"{p['code']} is {p['weight'] * 100:.0f}% of your holdings (more than {MAX_STOCK_WEIGHT * 100:.0f}% in one stock)."
                for p in positions if p["weight"] and p["weight"] > MAX_STOCK_WEIGHT and len(positions) > 1]
    warnings += [f"{r['sector']} is {r['weight'] * 100:.0f}% of your holdings (more than {MAX_SECTOR_WEIGHT * 100:.0f}% in one sector)."
                 for r in sector_rows if r["weight"] and r["weight"] > MAX_SECTOR_WEIGHT and len(sector_rows) > 1]
    if len(positions) == 1:
        warnings.append("All your holdings are in one stock.")
    # Sell signals on held stocks: CGT timing
    cgt_flags = []
    for p in positions:
        if p["signal"] != "Sell":
            continue
        young = [l for l in p["lots"] if not l["eligible"]]
        gain_young = sum(l["gain"] for l in young)
        cgt_flags.append({"code": p["code"], "confidence": p["confidence"], "young": young,
                          "young_units": sum(l["units"] for l in young), "young_gain": gain_young,
                          "next_date": min((l["discount_date"] for l in young), default=None),
                          "all_eligible": not young})
    # same money in STW
    adj_now = _stw_adj(c, today)
    stw_value = book.stw_cash + sum(l.stw_units for l in open_lots) * (adj_now or 0)
    div_cash = sum(d["cash"] for d in divs if d["received"])
    mine = total + book.proceeds + div_cash
    return {"positions": positions, "value": total, "cost": sum(p["cost"] for p in positions),
            "unrealised": total - sum(p["cost"] for p in positions),
            "realised": sum(r["gain"] for r in book.realised), "realised_rows": book.realised[::-1],
            "dividends": div_cash, "franking": sum(d["franking_credit"] for d in divs if d["received"]),
            "dividend_rows": divs[::-1], "upcoming_dividends": [d for d in divs if not d["received"]],
            "sectors": sector_rows, "warnings": warnings, "cgt_flags": cgt_flags,
            "invested": book.invested, "proceeds": book.proceeds, "mine_total": mine, "stw_total": stw_value,
            "trades": rows}

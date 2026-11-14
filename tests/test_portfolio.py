"""Phase 6: paper trading engine, watchlist, live signal record."""
from datetime import datetime, timezone

import pandas as pd
import pytest

from app.config import MARKET_TZ
from app.db import get_conn, upsert
from app.portfolio import paper as P
from app.portfolio import record as R
from app.portfolio import watchlist as W

NOW = "2026-11-13T00:00:00+00:00"
DAYS = list(pd.bdate_range("2026-03-02", periods=60).strftime("%Y-%m-%d"))   # Mon 2 Mar 2026 ...


def syd(day: str, hh: int, mm: int = 0) -> datetime:
    return datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}").replace(tzinfo=MARKET_TZ)


def seed(prices: dict[str, list[tuple[float, float]]] | None = None):
    """AAA opens 10 / closes 10.5 every day, BBB 20/20, index + STW flat at 100 (STW +1%/day adj)."""
    rows = []
    for i, d in enumerate(DAYS):
        rows.append({"symbol": "^AXJO", "date": d, "open": 100, "close": 100, "adj_close": 100, "fetched_at": NOW})
        rows.append({"symbol": "STW.AX", "date": d, "open": 50, "close": 50, "adj_close": 50 * 1.001 ** i,
                     "fetched_at": NOW})
        for sym, (o, cl) in {"AAA.AX": (10.0, 10.5), "BBB.AX": (20.0, 20.0), **(prices or {})}.items():
            rows.append({"symbol": sym, "date": d, "open": o, "close": cl, "adj_close": cl, "fetched_at": NOW})
    with get_conn() as c:
        upsert(c, "prices", rows, ["symbol", "date"])
        upsert(c, "companies", [{"code": k, "name": f"{k} LTD", "sector": "Materials", "status": "listed",
                                 "yahoo_symbol": f"{k}.AX"} for k in ("AAA", "BBB", "CCC")], ["code"])


def test_exec_after_rules():
    assert P.exec_after_for(syd("2026-03-02", 9, 30)) == "2026-03-02"     # Monday before the open
    assert P.exec_after_for(syd("2026-03-02", 10, 0)) == "2026-03-03"     # at/after the open -> next session
    assert P.exec_after_for(syd("2026-03-07", 9, 0)) == "2026-03-09"      # Saturday -> Monday's open
    assert P.exec_after_for(syd("2026-03-06", 15, 0)) == "2026-03-09"     # Friday afternoon -> Monday


def test_buy_sell_costs_and_pnl():
    seed()
    costs = P.Costs(brokerage=5.0, slippage_bps=10.0)
    with get_conn() as c:
        pid = P.create_portfolio(c, "Test", capital=10_000, now="2026-03-01T00:00:00+00:00")
        P.place_order(c, pid, "AAA", "buy", quantity=100, now=syd("2026-03-02", 15))
        assert P.fill_pending(c, pid, costs) == {"filled": 1, "rejected": 0}
        o = P.orders(c, pid)[0]
        assert o["filled_date"] == "2026-03-03" and o["price"] == pytest.approx(10.01)
        led = P.replay(c, P.get_portfolio(c, pid))
        assert led.cash == pytest.approx(10_000 - 1001 - 5)
        assert led.positions["AAA"].qty == 100
        P.place_order(c, pid, "AAA", "sell", now=syd("2026-03-04", 12))     # sell all
        P.fill_pending(c, pid, costs)
        led = P.replay(c, P.get_portfolio(c, pid))
        assert not led.positions
        assert led.cash == pytest.approx(10_000 - 1006 + 100 * 10 * 0.999 - 5)
        assert led.realised == pytest.approx(100 * 10 * 0.999 - 5 - 1006)
        assert led.fees == 10 and led.trades == 2


def test_order_waits_for_its_bar_and_rejects():
    seed()
    with get_conn() as c:
        pid = P.create_portfolio(c, "Test", capital=2_000)
        P.place_order(c, pid, "AAA", "buy", quantity=10, now=syd(DAYS[-1], 15))   # next session not in the data yet
        assert P.fill_pending(c, pid) == {"filled": 0, "rejected": 0}
        P.place_order(c, pid, "BBB", "sell", quantity=5, now=syd("2026-03-02", 15))  # nothing held
        P.place_order(c, pid, "BBB", "buy", quantity=500, now=syd("2026-03-02", 15))  # 10k > cash: cut down
        r = P.fill_pending(c, pid)
        assert r == {"filled": 1, "rejected": 1}
        buy = [o for o in P.orders(c, pid) if o["side"] == "buy" and o["status"] == "filled"][0]
        assert buy["filled_qty"] == 99 and "cut" in buy["message"]
        with pytest.raises(ValueError):
            P.place_order(c, pid, "ZZZ", "buy", quantity=1)
        with pytest.raises(ValueError):
            P.place_order(c, pid, "AAA", "buy", amount=100)                  # below the minimum


def test_dividends_on_ex_date_and_splits():
    seed()
    with get_conn() as c:
        upsert(c, "dividends", [{"code": "AAA", "ex_date": "2026-03-03", "amount": 1.0, "currency": "AUD"},
                                {"code": "AAA", "ex_date": "2026-03-10", "amount": 0.5, "currency": "AUD"}],
               ["code", "ex_date"])
        upsert(c, "splits", [{"code": "AAA", "date": "2026-03-20", "ratio": 2.0}], ["code", "date"])
        pid = P.create_portfolio(c, "Test", capital=10_000)
        P.place_order(c, pid, "AAA", "buy", quantity=100, now=syd("2026-03-02", 15))   # fills 3 Mar = ex-date
        P.fill_pending(c, pid, P.Costs(0, 0))
        led = P.replay(c, P.get_portfolio(c, pid))
        # bought at the open of the 3 Mar ex-date: no 1.00 dividend; 10 Mar: 0.50 on 200 split-adjusted units
        assert led.positions["AAA"].qty == 200
        assert led.dividends == pytest.approx(100.0)


def test_amount_buys_share_cash_and_equity_curve():
    seed()
    with get_conn() as c:
        pid = P.create_portfolio(c, "Test", capital=10_000, now="2026-03-01T22:00:00+00:00")  # Mon 2 Mar 09:00 Sydney
        P.place_order(c, pid, "AAA", "buy", amount=8_000, now=syd("2026-03-02", 9))
        P.place_order(c, pid, "BBB", "buy", amount=8_000, now=syd("2026-03-02", 9))
        P.fill_pending(c, pid, P.Costs(5, 0))
        fills = {o["code"]: o for o in P.orders(c, pid)}
        assert fills["AAA"]["filled_date"] == "2026-03-02"
        assert fills["AAA"]["filled_qty"] == 499 and fills["BBB"]["filled_qty"] == 250
        pf = P.get_portfolio(c, pid)
        curve = P.equity_curve(c, pf)
        assert curve.index[0] == "2026-03-02"
        cash = 10_000 - 4990 - 5000 - 10
        assert curve.iloc[0] == pytest.approx(cash + 499 * 10.5 + 250 * 20)
        bench = P.benchmark_curve(c, pf)
        assert bench.index[0] == "2026-03-02" and bench.iloc[-1] > bench.iloc[0]
        s = P.summary(c, pf)
        assert s["trades"] == 2 and len(s["positions"]) == 2
        assert sum(p["weight"] for p in s["positions"]) + s["cash"] / s["value"] == pytest.approx(1)


def _forecasts(day: str, ranked: list[str], signals: dict[str, str] | None = None):
    with get_conn() as c:
        upsert(c, "forecasts", [{"code": k, "as_of_date": day, "horizon": 21, "rank": i + 1, "eligible": 1,
                                 "p_beat": 0.6 - i * 0.05, "signal": (signals or {}).get(k, "Hold")}
                                for i, k in enumerate(ranked)], ["code", "as_of_date", "horizon"])


def test_signals_portfolio_follows_ranks_and_waits_between_rebalances(monkeypatch):
    seed({"CCC.AX": (5.0, 5.0)})
    monkeypatch.setenv("BACKTEST_TOP_N", "2")
    from app.config import get_settings
    get_settings.cache_clear()
    _forecasts(DAYS[0], ["AAA", "BBB", "CCC"])
    with get_conn() as c:
        pid = P.ensure_signals_portfolio(c)
        assert P.ensure_signals_portfolio(c) == pid
        note = P.rebalance_signals(c, pid, now=syd(DAYS[0], 17))
        assert "2 buys" in note
        assert P.rebalance_signals(c, pid) == "waiting for pending orders to fill"
        P.fill_pending(c, pid)
        led = P.replay(c, P.get_portfolio(c, pid))
        assert set(led.positions) == {"AAA", "BBB"}
    _forecasts(DAYS[5], ["CCC", "BBB", "AAA"])
    with get_conn() as c:
        assert P.rebalance_signals(c, pid).startswith("next rebalance in 15")
    _forecasts(DAYS[20], ["CCC", "BBB", "AAA"])
    with get_conn() as c:
        assert "1 sells, 1 buys" in P.rebalance_signals(c, pid, now=syd(DAYS[20], 17))
        P.fill_pending(c, pid)
        assert set(P.replay(c, P.get_portfolio(c, pid)).positions) == {"BBB", "CCC"}


def test_watchlist_add_remove_rows():
    seed()
    with get_conn() as c:
        assert W.add(c, "aaa.ax") and not W.add(c, "AAA")
        with pytest.raises(ValueError):
            W.add(c, "ZZZ")
        rows = W.rows(c)
        assert [r["code"] for r in rows] == ["AAA"] and rows[0]["price"] == 10.5
        assert W.remove(c, "AAA") and W.rows(c) == []


def test_signal_record_episodes_and_hits():
    seed({"CCC.AX": (5.0, 5.0)})
    for k in range(3):        # AAA Buy for 3 days = one episode; CCC Sell
        _forecasts(DAYS[k], ["AAA", "BBB", "CCC"], {"AAA": "Buy", "CCC": "Sell"})
    _forecasts(DAYS[30], ["AAA", "BBB", "CCC"], {"AAA": "Buy", "CCC": "Sell"})
    with get_conn() as c:
        # make AAA rise 10% over the month after DAYS[0]
        c.execute("UPDATE prices SET adj_close=11.55 WHERE symbol='AAA.AX' AND date>=?", (DAYS[1],))
        outs = R.outcomes(c)
    aaa = [o for o in outs if o["code"] == "AAA"]
    assert len(aaa) == 1 and aaa[0]["matured"] and aaa[0]["end_date"] == DAYS[21]
    assert aaa[0]["ret"] == pytest.approx(0.10) and aaa[0]["hit"] is True
    ccc = [o for o in outs if o["code"] == "CCC"][0]
    assert ccc["hit"] is True        # flat while STW rose
    s = R.summarise(outs)
    assert s["Buy"]["hit_rate"] == 1.0 and s["Sell"]["matured"] == 1 and s["since"] == DAYS[0]


# --- scheduler, maintenance, pages ---------------------------------------------------

def test_daily_catchup_and_train_rules():
    from datetime import timedelta
    from app.scheduler import last_daily_slot, needs_daily_catchup, needs_train
    fri_eve = syd("2026-11-13", 20)                       # Friday 8pm
    assert last_daily_slot(fri_eve) == syd("2026-11-13", 16, 10)
    assert last_daily_slot(syd("2026-11-16", 9)) == syd("2026-11-13", 16, 10)   # Monday morning -> Friday's slot
    assert needs_daily_catchup(syd("2026-11-13", 16, 30).astimezone(timezone.utc).isoformat(), syd("2026-11-16", 9)) is False
    assert needs_daily_catchup(syd("2026-11-12", 16, 30).isoformat(), fri_eve) is True
    assert needs_daily_catchup(None, fri_eve) is False
    assert needs_train(None, fri_eve)
    assert not needs_train({"trained_at": (fri_eve - timedelta(days=6)).isoformat()}, fri_eve)
    assert needs_train({"trained_at": (fri_eve - timedelta(days=9)).isoformat()}, fri_eve)


def test_maintenance_backup_rotates_only_its_own_files(monkeypatch):
    import gzip
    import sqlite3
    from app.config import get_settings
    from app.jobs import run_job
    from app.pipeline import maintenance as M
    d = M.backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "asx-pre-phase2.db").write_bytes(b"keep me")
    for k in range(5):
        (d / f"asx-auto-2020-01-0{k + 1}-0330.db.gz").write_bytes(b"old")
    with get_conn() as c:
        c.execute("INSERT INTO errors(at, job, source, item, message) VALUES('2020-01-01T00:00:00+00:00','j','s','i','m')")
    monkeypatch.setenv("BACKUP_KEEP", "2")
    get_settings.cache_clear()
    assert run_job("maintenance", M.job_maintenance) == "ok"
    files = sorted(p.name for p in d.iterdir())
    autos = [f for f in files if f.startswith("asx-auto-")]
    assert "asx-pre-phase2.db" in files and len(autos) == 2 and not autos[-1].startswith("asx-auto-2020")
    raw = gzip.decompress((d / autos[-1]).read_bytes())
    tmp = d / "check.db"
    tmp.write_bytes(raw)
    assert sqlite3.connect(tmp).execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0
    with get_conn() as c:
        assert c.execute("SELECT COUNT(*) FROM errors WHERE at < '2021'").fetchone()[0] == 0


def test_pages_watchlist_paper_status_dashboard():
    from fastapi.testclient import TestClient
    from app.main import app
    seed()
    cl = TestClient(app)
    r = cl.post("/watchlist/add", data={"code": "aaa", "note": "test note"}, follow_redirects=False)
    assert r.status_code == 303
    page = cl.get("/watchlist").text
    assert "AAA" in page and "test note" in page
    assert "Unknown" in cl.get("/watchlist?error=Unknown").text
    r = cl.post("/watchlist/BBB/toggle", headers={"HX-Request": "true"})
    assert "Watching" in r.text
    assert "★ Watching" in cl.get("/company/BBB").text
    r = cl.post("/paper/new", data={"name": "Mine", "capital": "20000"}, follow_redirects=False)
    pid = int(r.headers["location"].split("id=")[1])
    r = cl.post(f"/paper/{pid}/order", data={"code": "AAA", "side": "buy", "size_type": "quantity", "size": "100"},
                follow_redirects=False)
    assert "ok=" in r.headers["location"]
    r = cl.post(f"/paper/{pid}/order", data={"code": "NOPE", "side": "buy", "size_type": "quantity", "size": "1"},
                follow_redirects=False)
    assert "error=" in r.headers["location"]
    page = cl.get(f"/paper?id={pid}").text
    assert "Mine" in page and "Pending orders" in page and "AAA" in page
    api = cl.get(f"/api/paper/{pid}").json()
    assert api["orders"][0]["status"] == "pending" and api["value"] == 20000
    oid = api["orders"][0]["id"]
    cl.post(f"/paper/{pid}/cancel/{oid}")
    assert cl.get(f"/api/paper/{pid}").json()["orders"][0]["status"] == "cancelled"
    st = cl.get("/status").text
    assert "Storage and memory" in st and "maintenance" in st and "Needs a look" in st
    assert cl.get("/api/status").json()["health"]["level"] in ("warn", "bad")
    home = cl.get("/").text
    assert "Sectors" in home and "Mine" in home and "AAA" in home
    assert cl.post("/jobs/nope/run").status_code == 404

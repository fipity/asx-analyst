"""Screener + company page: synthetic data with known answers."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.analysis import market
from app.db import get_conn, upsert

NOW = "2026-11-12T06:00:00+00:00"


def _bdays(n: int) -> list[str]:
    """n weekdays ending today."""
    out, d = [], date.today()
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


@pytest.fixture
def seeded():
    days = _bdays(300)
    with get_conn() as c:
        upsert(c, "companies", [
            {"code": "AAA", "name": "ALPHA MINING LTD", "sector": "Materials", "status": "listed", "yahoo_symbol": "AAA.AX"},
            {"code": "BBB", "name": "BETA BANK LTD", "sector": "Financials", "status": "listed", "yahoo_symbol": "BBB.AX"},
            {"code": "CCC", "name": "GAMMA RESOURCES", "sector": "Materials", "status": "listed", "yahoo_symbol": "CCC.AX"},
            {"code": "ZZZ", "name": "OUTSIDE CO", "sector": "Materials", "status": "listed", "yahoo_symbol": "ZZZ.AX"},
        ], ["code"])
        upsert(c, "index_membership", [
            {"index_code": "asx200", "code": k, "as_of": days[-2], "weight": w, "source": "test", "fetched_at": NOW}
            for k, w in (("AAA", 5.0), ("BBB", 3.0), ("CCC", 1.0))], ["index_code", "as_of", "code"])
        rows = []
        for i, d in enumerate(days):
            # AAA: +0.1%/day; BBB: flat at 10; CCC: falls from 20 to ~half; ^AXJO flat 8000
            for sym, px in (("AAA.AX", 10 * 1.001 ** i), ("BBB.AX", 10.0), ("CCC.AX", 20 * 0.9977 ** i),
                            ("^AXJO", 8000.0), ("ZZZ.AX", 1.0)):
                rows.append({"symbol": sym, "date": d, "open": px, "high": px * 1.01, "low": px * 0.99,
                             "close": px, "adj_close": px, "volume": 1000, "fetched_at": NOW})
        upsert(c, "prices", rows, ["symbol", "date"])
        for f in [
            {"code": "AAA", "as_of_date": days[-1], "source": "yahoo", "market_cap": 5e9, "pe": 12.0,
             "dividend_yield": 0.07, "roe": 0.2, "debt_to_equity": 40.0},
            {"code": "AAA", "as_of_date": days[-1], "source": "asx", "franking_pct": 100.0, "dividend_yield": 0.05},
            {"code": "BBB", "as_of_date": days[-1], "source": "yahoo", "market_cap": 3e9, "pe": 18.0,
             "dividend_yield": 0.04, "roe": 0.1},
            {"code": "CCC", "as_of_date": days[-1], "source": "yahoo", "market_cap": 1e9, "pe": -3.0, "eps": -0.5},
        ]:  # upsert() takes its column list from the first row, so one call per shape
            upsert(c, "fundamentals", [f], ["code", "as_of_date", "source"])
        upsert(c, "short_positions", [
            {"code": "AAA", "date": d, "short_pct": 1.0 + 0.01 * i, "available_at": d, "fetched_at": NOW}
            for i, d in enumerate(days[-25:])], ["code", "date"])
        # a Saturday ex-date must snap to the following Monday
        sat = next(d for d in (date.fromisoformat(days[-30]) + timedelta(days=k) for k in range(7)) if d.weekday() == 5)
        upsert(c, "dividends", [{"code": "AAA", "ex_date": sat.isoformat(), "amount": 0.5, "franking_pct": 100.0,
                                 "fetched_at": NOW}], ["code", "ex_date"])
        upsert(c, "announcements", [
            {"ids_id": f"0{i:07d}", "code": "AAA", "published_at": f"{days[-40 + i]}T00:30:00+00:00",
             "headline": f"Update {i}", "price_sensitive": int(i % 2 == 0),
             "url": f"https://example.invalid/{i}", "fetched_at": NOW} for i in range(30)], ["ids_id"])
        upsert(c, "director_trades", [
            {"ids_id": "09999999", "code": "AAA", "published_at": f"{days[-5]}T01:00:00+00:00", "director": "Jo Smith",
             "change_date": days[-6], "acquired": 1000.0, "disposed": 0.0, "value": 12000.0, "trade_type": "buy",
             "on_market": 1, "parse_ok": 1, "fetched_at": NOW}], ["ids_id"])
    return {"days": days, "monday": (sat + timedelta(days=2)).isoformat()}


def test_gross_up():
    assert market.gross_up(0.05, 100) == pytest.approx(0.05 / 0.7)
    assert market.gross_up(0.05, 0) == pytest.approx(0.05)
    assert market.gross_up(0.04, 50) == pytest.approx(0.04 * (1 + 0.5 * 0.3 / 0.7))
    assert market.gross_up(None, 100) is None


def test_screener_rows_merge_and_metrics(seeded):
    with get_conn() as c:
        rows = {r["code"]: r for r in market.screener_rows(c)}
    assert set(rows) == {"AAA", "BBB", "CCC"}          # ZZZ is outside the universe
    a = rows["AAA"]
    assert a["ret_1m"] == pytest.approx(1.001 ** 21 - 1)
    assert a["ret_1y"] == pytest.approx(1.001 ** 252 - 1)
    assert a["chg_1d"] == pytest.approx(0.001)
    assert a["dividend_yield"] == pytest.approx(0.05)    # ASX yield preferred
    assert a["gross_yield"] == pytest.approx(0.05 / 0.7)
    assert a["debt_to_equity"] == pytest.approx(0.4)     # Yahoo percent -> ratio
    assert a["short_pct"] == pytest.approx(1.24)
    assert a["short_chg"] == pytest.approx(0.20)         # 20 reports back
    assert rows["CCC"]["pe"] is None and rows["CCC"]["loss_making"]
    assert rows["CCC"]["off_high"] < -0.4
    assert rows["BBB"]["ret_1y"] == pytest.approx(0)


def test_filters_and_sorting(seeded):
    with get_conn() as c:
        rows = market.screener_rows(c)
    f = lambda **p: [r["code"] for r in market.filter_rows(rows, p)]
    assert f(sector="Materials") == ["AAA", "CCC"]
    assert f(q="bank") == ["BBB"]
    assert f(q="aaa") == ["AAA"]
    assert f(max_pe="15") == ["AAA"]                   # loss-maker (no P/E) excluded
    assert f(min_yield="6") == ["AAA"]                 # gross yield in %
    assert f(min_mcap="2") == ["AAA", "BBB"]           # A$ bn
    assert f(min_franking="50") == ["AAA"]
    assert f(min_mcap="abc") == ["AAA", "BBB", "CCC"]  # junk input ignored
    order = [r["code"] for r in market.sort_rows(rows, "pe", desc=False)]
    assert order == ["AAA", "BBB", "CCC"]              # blanks last
    order = [r["code"] for r in market.sort_rows(rows, "pe", desc=True)]
    assert order == ["BBB", "AAA", "CCC"]              # blanks still last
    assert [r["code"] for r in market.sort_rows(rows, "name", False)] == ["AAA", "BBB", "CCC"]


def test_peers_and_returns(seeded):
    with get_conn() as c:
        rows = market.screener_rows(c)
        peers, med = market.peers(rows, "AAA", "Materials")
        ret = market.returns_table(c, "AAA.AX", "Materials")
    assert [p["code"] for p in peers] == ["AAA", "CCC"]
    assert med["n"] == 2 and med["market_cap"] == pytest.approx(3e9)
    assert ret[0]["label"].startswith("This stock") and ret[0]["1y"] == pytest.approx(1.001 ** 252 - 1)
    assert ret[-1]["label"] == "S&P/ASX 200 index" and ret[-1]["1y"] == pytest.approx(0)
    assert ret[0]["5y"] is None                       # not enough history


def test_chart_payload_snaps_events(seeded):
    with get_conn() as c:
        p = market.chart_payload(c, "AAA", "AAA.AX")
    assert len(p["ohlcv"]) == 300
    divs = [e for e in p["events"] if e["kind"] == "dividend"]
    assert divs[0]["date"] == seeded["monday"]        # Saturday ex-date -> next trading day
    assert "100% franked" in divs[0]["text"]
    anns = [e for e in p["events"] if e["kind"] == "announcement"]
    assert len(anns) == 15                            # price-sensitive only
    assert [e["kind"] for e in p["events"]].count("buy") == 1
    assert [e["date"] for e in p["events"]] == sorted(e["date"] for e in p["events"])
    assert len(p["shorts"]) == 25


def test_pages(seeded):
    from app.main import app
    with TestClient(app) as c:
        r = c.get("/screener")
        assert r.status_code == 200 and "AAA" in r.text and "not financial advice" in r.text
        assert ">loss<" in r.text
        r = c.get("/screener?sector=Financials", headers={"HX-Request": "true", "HX-Target": "screener-results"})
        assert r.status_code == 200 and "<html" not in r.text and "BBB" in r.text and "AAA.AX" not in r.text
        assert "1 of 3 companies" in r.text
        assert c.get("/api/screener?min_yield=6").json()["count"] == 1
        r = c.get("/company/aaa")
        assert r.status_code == 200 and "Alpha Mining Ltd" in r.text and "Jo Smith" in r.text
        assert "delayed 20+ minutes" in r.text
        assert c.get("/company/ZZZ").status_code == 200      # outside the universe still renders
        assert c.get("/company/NOPE").status_code == 404
        part = c.get("/company/AAA/announcements?ps=1").text
        assert part.count("<tr>") == 15 and "Show more" not in part
        part = c.get("/company/AAA/announcements?ps=0").text
        assert "Show more" in part and "offset=25" in part
        more = c.get("/company/AAA/announcements?ps=0&offset=25").text
        assert "<table" not in more and more.count("<tr>") == 5
        chart = c.get("/api/company/AAA/chart").json()
        assert chart["code"] == "AAA" and len(chart["ohlcv"]) == 300
        r = c.get("/go?q=bbb", follow_redirects=False)
        assert r.headers["location"] == "/company/BBB"
        r = c.get("/go?q=beta bank", follow_redirects=False)
        assert r.headers["location"] == "/screener?q=beta%20bank"
        r = c.get("/")
        assert r.status_code == 200 and "Biggest rises" in r.text


def test_company_name_case():
    from app.main import company_name
    assert company_name("BHP GROUP LIMITED") == "BHP Group Limited"
    assert company_name("COMMONWEALTH BANK OF AUSTRALIA.") == "Commonwealth Bank Of Australia."
    assert company_name("JB HI-FI LIMITED") == "JB Hi-Fi Limited"
    assert company_name("NEW HOPE CORPORATION LIMITED") == "New Hope Corporation Limited"
    assert company_name("Block, Inc.") == "Block, Inc."
    assert company_name(None) == ""


def test_analysis_job_feeds_pages(seeded):
    from app.jobs import run_job
    from app.main import app
    from app.pipeline.ingest import JOBS
    assert run_job("analysis", JOBS["analysis"]) == "ok"
    with get_conn() as c:
        assert c.execute("SELECT COUNT(*) FROM technicals").fetchone()[0] == 3
        rows = {r["code"]: r for r in market.screener_rows(c)}
    assert rows["AAA"]["trend"] == "up" and rows["CCC"]["trend"] == "down"
    assert rows["AAA"]["rsi14"] == pytest.approx(100)
    assert rows["AAA"]["composite"] is not None or rows["AAA"]["quality"] is not None
    assert rows["AAA"]["sector_rank"] in (1, 2, None)
    with TestClient(app) as c:
        r = c.get("/screener?view=technical")
        assert r.status_code == 200 and "Uptrend" in r.text and "RSI" in r.text
        r = c.get("/screener?trend=down", headers={"HX-Request": "true", "HX-Target": "screener-results"})
        assert "CCC" in r.text and "/company/AAA" not in r.text
        r = c.get("/screener?view=scores&sort=valuation")
        assert r.status_code == 200 and "Div score" in r.text
        page = c.get("/company/AAA").text
        assert "Fundamental scores" in page and "Technical picture" in page and "Uptrend" in page
        assert "What&#39;s behind the scores" in page or "What's behind the scores" in page
        assert "Scores are only calculated" in c.get("/company/ZZZ").text
        ind = c.get("/api/company/AAA/indicators").json()
        assert len(ind["dates"]) == len(ind["sma50"]) == len(ind["rsi14"])
        assert sum(v is not None for v in ind["sma50"]) == 300 - 49 and ind["rsi14"][-1] == pytest.approx(100)
        assert "Fundamental scores" in c.get("/status").text


def test_aggregate_payload_weekly_monthly():
    daily = {"code": "X", "ohlcv": [
        ["2026-11-02", 10, 11, 9, 10.5, 100],     # Mon  (week 45, Nov)
        ["2026-11-03", 10.5, 12, 10, 11, None],   # Tue: missing volume
        ["2026-11-06", 11, 11.5, 8, 9, 50],       # Fri
        ["2026-11-10", 9, 9.5, 8.5, 9.2, 10],     # Tue (Mon 9 Nov missing) -> week dated 10th
        ["2026-12-01", 9.2, 9.3, 9.0, 9.1, 5],    # Dec
    ], "events": [{"kind": "dividend", "date": "2026-11-06", "text": "d", "url": None}],
       "shorts": [["2026-11-03", 1.0], ["2026-11-05", 1.5], ["2026-11-08", 2.0], ["2026-11-11", 2.5]]}
    w = market.aggregate_payload(daily, "W")
    assert w["interval"] == "W"
    assert w["ohlcv"] == [["2026-11-02", 10, 12, 8, 9, 150], ["2026-11-10", 9, 9.5, 8.5, 9.2, 10],
                          ["2026-12-01", 9.2, 9.3, 9.0, 9.1, 5]]
    assert w["events"][0]["date"] == "2026-11-02"
    assert w["shorts"] == [["2026-11-02", 2.0], ["2026-11-10", 2.5]]   # last reading per week (Sun 8th = week 45)
    m = market.aggregate_payload(daily, "M")
    assert [b[0] for b in m["ohlcv"]] == ["2026-11-02", "2026-12-01"]
    assert m["ohlcv"][0][1:] == [10, 12, 8, 9.2, 160]
    assert m["shorts"] == [["2026-11-02", 2.5]]
    assert market.aggregate_payload(daily, "D")["ohlcv"] == daily["ohlcv"]


def test_chart_intervals_api(seeded):
    from app.main import app
    with TestClient(app) as c:
        d = c.get("/api/company/AAA/chart").json()
        w = c.get("/api/company/AAA/chart?interval=w").json()
        m = c.get("/api/company/AAA/chart?interval=M").json()
        assert len(d["ohlcv"]) == 300 and 59 <= len(w["ohlcv"]) <= 61 and 13 <= len(m["ohlcv"]) <= 16
        assert w["ohlcv"][-1][4] == d["ohlcv"][-1][4]              # last close is the same
        assert sum(b[5] or 0 for b in w["ohlcv"]) == sum(b[5] or 0 for b in d["ohlcv"])
        assert {e["date"] for e in w["events"]} <= {b[0] for b in w["ohlcv"]}
        ind = c.get("/api/company/AAA/indicators?interval=W").json()
        assert set(ind["dates"]) <= {b[0] for b in w["ohlcv"]}
        assert sum(v is not None for v in ind["sma20"]) == len(w["ohlcv"]) - 19
        assert c.get("/api/company/AAA/chart?interval=5m").status_code == 400


def test_dates_displayed_dd_mm_yyyy():
    from app.main import _finalize, fmt_date, fmt_time
    from markupsafe import Markup
    assert fmt_date("2026-11-13") == "13/11/2026" and fmt_date("2026-06") == "06/2026" and fmt_date(None) == ""
    assert fmt_time("2026-11-13T02:00:00+00:00") == "13/11/2026 13:00"     # Sydney (AEDT)
    assert _finalize("forecasts of 2026-11-12: 10 buys, 2020-01-02 – 2023-12-28") == \
        "forecasts of 12/11/2026: 10 buys, 02/01/2020 – 28/12/2023"
    for keep in ("https://x.com/2026-11-12/a", "/x?d=2026-11-12", "2026-11-12T01:00:00", Markup("2026-11-12")):
        assert _finalize(keep) == keep


def test_market_detail_helpers():
    import pandas as pd
    from app.analysis import markets
    idx = pd.bdate_range("2014-01-01", "2026-11-13").strftime("%Y-%m-%d")
    s = pd.Series([100 * 1.0003 ** i for i in range(len(idx))], index=list(idx))
    r = {x["label"]: x for x in markets.returns(s)}
    assert r["1 day"]["chg"] == pytest.approx(0.0003)
    assert r["1 year"]["from"] <= "2025-11-13" and r["1 year"]["chg"] > 0
    assert r["10 years"]["ann"] == pytest.approx((1 + r["10 years"]["chg"]) ** (365 / 3650) - 1)
    short = s[s.index >= "2025-06-01"]
    assert "chg" not in {x["label"]: x for x in markets.returns(short)}["3 years"]     # history too short
    rate = pd.Series([4.35, 4.1, 3.85, 4.35], index=["2024-01-01", "2025-02-19", "2025-05-21", "2026-11-04"])
    assert [d["change"] for d in markets.rate_decisions(rate)] == pytest.approx([0.5, -0.25, -0.25])
    carried = markets._carry_to_today(rate)
    assert carried.index[-1] >= "2026-11-04" and carried.iloc[-1] == 4.35


def test_market_pages(seeded):
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/market/nope").status_code == 404
        for slug in ("asx200", "cash-rate", "gold"):
            r = c.get(f"/market/{slug}")
            assert r.status_code == 200 and "What it is and why it matters" in r.text
        pts = c.get("/api/market/asx200/series").json()
        assert pts["slug"] == "asx200" and not pts["step"]
        home = c.get("/").text
        assert 'href="/market/asx200"' in home


def test_header_refresh_and_data_bar(seeded, monkeypatch):
    import threading
    from app.main import app
    from app.pipeline.ingest import JOBS
    gate = threading.Event()

    def fake_update(ctx):
        ctx.progress("prices 40/200")
        gate.wait(5)
    monkeypatch.setitem(JOBS, "daily_update", fake_update)
    with TestClient(app) as c:
        page = c.get("/screener").text
        assert 'id="refresh-btn"' in page and 'id="data-bar"' in page
        st = c.get("/api/refresh").json()
        assert st["running"] is False and "price_date_text" in st
        r = c.post("/api/refresh")
        assert r.status_code == 202 and r.json()["started"] is True
        for _ in range(50):
            st = c.get("/api/refresh").json()
            if st["total"]:
                break
            threading.Event().wait(0.05)
        assert st["running"] and st["done"] == 40 and st["total"] == 200
        assert "Refreshing data" in c.get("/").text
        assert c.post("/api/refresh").status_code == 409            # already running
        gate.set()
        for _ in range(50):
            if not c.get("/api/refresh").json()["running"]:
                break
            threading.Event().wait(0.05)
        assert c.get("/api/refresh").json()["running"] is False
        r = c.post("/refresh", headers={"referer": "http://testserver/screener"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "http://testserver/screener"
        for _ in range(50):                                          # let that run finish before teardown
            if not c.get("/api/refresh").json()["running"]:
                break
            threading.Event().wait(0.05)

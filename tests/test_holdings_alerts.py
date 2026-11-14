"""Phase 7: holdings (FIFO, CGT discount dates, dividends, concentration), alerts, CSV export."""
from datetime import datetime, timezone

import pytest

from app import alerts as A
from app.config import get_settings
from app.db import get_conn, upsert
from app.portfolio import holdings as H
from app.portfolio import watchlist as W
from tests.test_portfolio import DAYS, seed, syd

TODAY = DAYS[-1]


def _trade(c, code, side, day, qty, price, fees=10.0):
    return H.add_trade(c, code, side, day, qty, price, fees)


def test_discount_date():
    assert H.discount_date("2025-01-01") == "2026-01-02"
    assert H.discount_date("2024-02-29") == "2025-03-02"


def test_fifo_realised_gains_and_validation():
    seed()
    with get_conn() as c:
        _trade(c, "AAA", "buy", DAYS[0], 100, 10)
        _trade(c, "AAA", "buy", DAYS[5], 100, 12)
        _trade(c, "AAA", "sell", DAYS[10], 150, 15)
        book = H.build(c)
        r1, r2 = book.realised
        assert r1["cost"] == pytest.approx(1010) and r1["proceeds"] == pytest.approx(2240 * 100 / 150)
        assert r2["units"] == 50 and r2["cost"] == pytest.approx(605)
        assert not r1["discount"]                              # held < 12 months
        sm = H.summary(c, today=TODAY)
        p = sm["positions"][0]
        assert p["units"] == 50 and p["cost"] == pytest.approx(605) and p["value"] == pytest.approx(525)
        assert sm["realised"] == pytest.approx(2240 - 1010 - 605)
        with pytest.raises(ValueError, match="only"):
            _trade(c, "AAA", "sell", DAYS[11], 51, 15)          # more than held
        with pytest.raises(ValueError, match="only"):
            _trade(c, "AAA", "sell", DAYS[2], 150, 15)          # before the second buy, after the first
        with pytest.raises(ValueError, match="future"):
            _trade(c, "AAA", "buy", "2999-01-01", 1, 1)
        with pytest.raises(ValueError, match="unknown"):
            _trade(c, "ZZZ", "buy", DAYS[0], 1, 1)
        first_buy = H.trades(c)[0]["id"]
        with pytest.raises(ValueError):
            H.delete_trade(c, first_buy)                        # later sell depends on it
        assert len(H.trades(c)) == 3


def test_dividends_franking_and_stw_mirror():
    seed()
    with get_conn() as c:
        upsert(c, "dividends", [
            {"code": "AAA", "ex_date": DAYS[3], "amount": 1.0, "currency": "AUD", "franking_pct": 100.0, "pay_date": DAYS[6]},
            {"code": "AAA", "ex_date": DAYS[12], "amount": 0.5, "currency": "AUD", "franking_pct": 0.0, "pay_date": "2099-01-01"},
        ], ["code", "ex_date"])
        _trade(c, "AAA", "buy", DAYS[0], 100, 10, fees=0)
        _trade(c, "AAA", "sell", DAYS[10], 50, 10, fees=0)
        divs = H.dividends(c, today=TODAY)
        d1, d2 = divs
        assert d1["units"] == 100 and d1["cash"] == pytest.approx(100) and d1["franking_credit"] == pytest.approx(100 * 0.3 / 0.7)
        assert d2["units"] == 50 and not d2["received"]
        sm = H.summary(c, today=TODAY)
        assert sm["dividends"] == pytest.approx(100) and len(sm["upcoming_dividends"]) == 1
        adj = lambda i: 50 * 1.001 ** i
        units = 1000 / adj(0)
        expected = units / 2 * adj(10) + units / 2 * adj(len(DAYS) - 1)
        assert sm["stw_total"] == pytest.approx(expected)
        assert sm["mine_total"] == pytest.approx(50 * 10.5 + 500 + 100)


def test_concentration_and_cgt_flag_on_sell_signal():
    seed()
    with get_conn() as c:
        _trade(c, "AAA", "buy", DAYS[0], 1000, 10)
        _trade(c, "BBB", "buy", DAYS[0], 10, 20)
        upsert(c, "forecasts", [{"code": "AAA", "as_of_date": TODAY, "horizon": 21, "signal": "Sell",
                                 "confidence": "low", "p_beat": 0.4}], ["code", "as_of_date", "horizon"])
        sm = H.summary(c, today=TODAY)
        assert any("AAA is" in w for w in sm["warnings"]) and any("Materials" in w for w in sm["warnings"]) is False
        f = sm["cgt_flags"][0]
        assert f["code"] == "AAA" and not f["all_eligible"] and f["next_date"] == H.discount_date(DAYS[0])
        assert f["young_gain"] == pytest.approx(1000 * 10.5 - 10010)
        assert H.held_codes(c) == ["AAA", "BBB"]


# --- alerts ----------------------------------------------------------------------

@pytest.fixture
def webhook(monkeypatch):
    sent = []

    class Resp:
        def raise_for_status(self):
            pass

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append((url, json))
        return Resp()
    monkeypatch.setenv("WEBHOOK_URL", "http://hook.test/x")
    monkeypatch.setenv("QUIET_HOURS_START", "22:00")
    monkeypatch.setenv("QUIET_HOURS_END", "07:00")
    get_settings.cache_clear()
    monkeypatch.setattr(A.httpx, "post", fake_post)
    return sent


def test_quiet_hours_parsing(monkeypatch):
    assert A.in_quiet_hours(syd("2026-03-02", 23)) and A.in_quiet_hours(syd("2026-03-02", 6, 59))
    assert not A.in_quiet_hours(syd("2026-03-02", 7))
    monkeypatch.setenv("QUIET_HOURS_START", "")
    get_settings.cache_clear()
    assert not A.in_quiet_hours(syd("2026-03-02", 23))


def test_notify_skipped_without_channels_then_sent_queued_and_deduped(webhook, monkeypatch):
    noon, night = syd("2026-03-02", 12), syd("2026-03-02", 23)
    assert A.notify("k1", "test", "Hello", now=noon) == "sent"
    assert A.notify("k1", "test", "Hello", now=noon) == "duplicate"
    assert webhook[0][1]["title"] == "Hello" and webhook[0][0] == "http://hook.test/x"
    assert A.notify("k2", "signal", "Night", now=night) == "queued"
    assert A.flush_queued(now=night) == 0
    assert A.flush_queued(now=syd("2026-03-03", 7, 5)) == 1 and webhook[-1][1]["title"] == "Night"
    assert A.notify("k3", "test", "Forced", now=night, force=True) == "sent"
    monkeypatch.setenv("WEBHOOK_URL", "")
    get_settings.cache_clear()
    assert A.notify("k4", "test", "Nobody", now=noon) == "skipped"


def test_signal_change_alert_for_followed_stock(webhook):
    seed()
    with get_conn() as c:
        W.add(c, "AAA")
        _trade(c, "BBB", "buy", DAYS[0], 10, 20)
        rows = []
        for day, sigs in ((DAYS[-2], {"AAA": "Hold", "BBB": "Hold", "CCC": "Hold"}),
                          (DAYS[-1], {"AAA": "Buy", "BBB": "Sell", "CCC": "Buy"})):
            rows += [{"code": k, "as_of_date": day, "horizon": 21, "signal": v, "confidence": "low", "p_beat": 0.5}
                     for k, v in sigs.items()]
        upsert(c, "forecasts", rows, ["code", "as_of_date", "horizon"])
        assert A.check_signal_changes(c, now=syd("2026-03-02", 17)) == 2       # CCC isn't followed
        assert A.check_signal_changes(c, now=syd("2026-03-02", 17)) == 0       # deduped
    titles = [j["title"] for _, j in webhook]
    assert "AAA: signal Hold → Buy" in titles and "BBB: signal Hold → Sell" in titles
    bbb = next(j for _, j in webhook if j["title"].startswith("BBB"))
    assert "CGT" in bbb["body"] and "12 months" in bbb["body"]


def test_announcement_alerts_start_from_now(webhook):
    seed()
    now = datetime.now(timezone.utc).replace(hour=3, minute=0)    # 2 pm Sydney-ish: not quiet hours
    with get_conn() as c:
        W.add(c, "AAA")
        old = {"ids_id": "a0", "code": "AAA", "published_at": now.isoformat(), "headline": "Old news",
               "price_sensitive": 1, "fetched_at": now.isoformat()}
        upsert(c, "announcements", [old], ["ids_id"])
        assert A.check_announcements(c, now=now) == 0                     # first run: just sets the start point
        later = now.replace(minute=30).isoformat()
        upsert(c, "announcements", [
            {**old, "ids_id": "a1", "headline": "Takeover offer", "fetched_at": later},
            {**old, "ids_id": "a2", "code": "BBB", "headline": "Not followed", "fetched_at": later},
            {**old, "ids_id": "a3", "headline": "Not price sensitive", "price_sensitive": 0, "fetched_at": later}],
            ["ids_id"])
        assert A.check_announcements(c, now=now.replace(minute=31)) == 1
        assert A.check_announcements(c, now=now.replace(minute=32)) == 0
    assert [j["title"] for _, j in webhook] == ["AAA: Takeover offer"]


def test_job_failure_alert_throttled(webhook, monkeypatch):
    monkeypatch.setattr(A, "in_quiet_hours", lambda now=None: False)
    A.on_job_failure("daily_update", "boom")
    A.on_job_failure("daily_update", "boom again")
    assert len(webhook) == 1 and "daily_update" in webhook[0][1]["title"]


def test_pages_and_csv_exports(webhook):
    from fastapi.testclient import TestClient
    from app.main import app
    seed()
    cl = TestClient(app)
    r = cl.post("/holdings/trade", data={"code": "aaa", "side": "buy", "trade_date": DAYS[0], "quantity": "100",
                                         "price": "10", "fees": "5"}, follow_redirects=False)
    assert "ok=" in r.headers["location"]
    r = cl.post("/holdings/trade", data={"code": "AAA", "side": "sell", "trade_date": DAYS[1], "quantity": "500",
                                         "price": "10", "fees": "5"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    page = cl.get("/holdings").text
    assert "Parcels" in page and "AAA" in page and "Same money in the ASX 200" in page
    assert cl.get("/api/holdings").json()["positions"][0]["units"] == 100
    for url, head in (("/export/holdings.csv", "code,name"), ("/export/holdings-trades.csv", "id,trade_date"),
                      ("/export/holdings-realised.csv", "sell_date"), ("/export/dividends.csv", "code,ex_date"),
                      ("/export/signals.csv", ""), ("/export/screener.csv?sector=Materials", "code,name")):
        r = cl.get(url)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv"), url
        assert r.text.startswith(head) and "attachment" in r.headers["content-disposition"]
    pid = int(cl.post("/paper/new", data={"name": "X", "capital": "10000"}, follow_redirects=False)
              .headers["location"].split("=")[1])
    assert cl.get(f"/export/paper/{pid}.csv").text.startswith("portfolio,id")
    assert "Alerts" in cl.get("/alerts").text
    r = cl.post("/alerts/test", follow_redirects=False)
    assert "ok=" in r.headers["location"] and webhook[-1][1]["title"] == "ASX Analyst test alert"


def test_dividend_currency_migration_logic():
    """Yahoo rows -> AUD; ASX-only rows whose amount still matches the ASX calendar text keep USD."""
    from app.db import MIGRATIONS
    with get_conn() as c:
        upsert(c, "dividends", [{"code": "BHP", "ex_date": "2025-09-04", "amount": 0.9193, "currency": "USD", "source": "yahoo"},
                                {"code": "NEM", "ex_date": "2026-11-24", "amount": 0.2598, "currency": "USD", "source": "asx"},
                                {"code": "AMC", "ex_date": "2026-11-24", "amount": 1.45, "currency": "USD", "source": "asx"}],
               ["code", "ex_date"])
        upsert(c, "calendar_events", [{"code": k, "event_type": "ex_dividend", "event_date": "2026-11-24",
                                       "detail": f"{a} USD 0% franked"} for k, a in (("NEM", 0.2598), ("AMC", 0.94))],
               ["code", "event_type", "event_date"])
        c.executescript(MIGRATIONS[8])
        cur = dict(c.execute("SELECT code, currency FROM dividends").fetchall())
    assert cur == {"BHP": "AUD", "NEM": "USD", "AMC": "AUD"}

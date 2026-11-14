"""ASX holiday calendar, corporate actions on holdings, AI announcement summaries (mocked API)."""
from datetime import date, datetime, timezone

import pytest

from app import market_calendar as M
from app.config import get_settings
from app.db import get_conn, upsert
from app.portfolio import holdings as H
from tests.test_portfolio import DAYS, seed, syd

# Weekdays with no ASX trading 2014-2026 (from ^AXJO history, minus three Yahoo data gaps).
CLOSED = """2014-01-01 2014-01-27 2014-04-18 2014-04-21 2014-04-25 2014-06-09 2014-12-25 2014-12-26 2015-01-01
2015-01-26 2015-04-03 2015-04-06 2015-06-08 2015-12-25 2015-12-28 2016-01-01 2016-01-26 2016-03-25 2016-03-28
2016-04-25 2016-06-13 2016-12-26 2016-12-27 2017-01-02 2017-01-26 2017-04-14 2017-04-17 2017-04-25 2017-06-12
2017-12-25 2017-12-26 2018-01-01 2018-01-26 2018-03-30 2018-04-02 2018-04-25 2018-06-11 2018-12-25 2018-12-26
2019-01-01 2019-01-28 2019-04-19 2019-04-22 2019-04-25 2019-06-10 2019-12-25 2019-12-26 2020-01-01 2020-01-27
2020-04-10 2020-04-13 2020-06-08 2020-12-25 2020-12-28 2021-01-01 2021-01-26 2021-04-02 2021-04-05 2021-06-14
2021-12-27 2021-12-28 2022-01-03 2022-01-26 2022-04-15 2022-04-18 2022-04-25 2022-06-13 2022-09-22 2022-12-26
2022-12-27 2023-01-02 2023-01-26 2023-04-07 2023-04-10 2023-04-25 2023-06-12 2023-12-25 2023-12-26 2024-01-01
2024-01-26 2024-03-29 2024-04-01 2024-04-25 2024-06-10 2024-12-25 2024-12-26 2025-01-01 2025-01-27 2025-04-18
2025-04-21 2025-04-25 2025-06-09 2025-12-25 2025-12-26 2026-01-01 2026-01-26 2026-04-03 2026-04-06 2026-06-08""".split()


def test_calendar_matches_asx_history():
    import pandas as pd
    days = pd.bdate_range("2014-01-01", "2026-11-12").strftime("%Y-%m-%d")
    assert sorted(d for d in days if not M.is_trading_day(d)) == sorted(CLOSED)
    assert M.is_trading_day("2026-04-27")            # Anzac Day on Saturday: not moved
    assert M.holiday_name("2026-12-25") == "Christmas Day" and M.holiday_name("2026-12-28") == "Boxing Day"
    assert M.next_trading_day("2026-12-24", include=False) == date(2026, 12, 29)
    assert M.trading_days_after("2026-12-24", "2026-12-29") == 1


def test_scheduler_and_paper_respect_holidays():
    from app.portfolio.paper import exec_after_for
    from app.scheduler import last_daily_slot
    assert last_daily_slot(syd("2026-06-08", 20)) == syd("2026-06-05", 16, 10)     # King's Birthday Monday
    assert exec_after_for(syd("2026-06-05", 15)) == "2026-06-09"                    # Friday -> Tuesday open


# --- corporate actions ------------------------------------------------------------------

def test_drp_capital_return_and_demerger():
    seed({"CCC.AX": (5.0, 5.0)})
    with get_conn() as c:
        H.add_trade(c, "AAA", "buy", DAYS[0], 100, 10, 0)
        H.add_trade(c, "AAA", "drp", DAYS[2], 10, 11, 0)
        H.add_trade(c, "AAA", "return", DAYS[4], 110, 1.0, 0)          # A$110 back
        H.add_trade(c, "AAA", "demerger", DAYS[6], 110, 0, 0, other_code="CCC", ratio=0.5, cost_pct=20)
        book = H.build(c)
        aaa = [l for l in book.lots if l.code == "AAA"]
        ccc = [l for l in book.lots if l.code == "CCC"]
        # cost bases: 1000 and 110 less a A$1/share return -> 900 and 100; then 80% stays, 20% moves
        assert [round(l.cost, 6) for l in aaa] == [720.0, 80.0]
        assert [l.units for l in ccc] == [50.0, 5.0] and [round(l.cost, 6) for l in ccc] == [180.0, 20.0]
        assert [l.buy_date for l in ccc] == [DAYS[0], DAYS[2]]          # keeps original dates (CGT discount)
        assert book.proceeds == pytest.approx(110)
        assert H.held_codes(c) == ["AAA", "CCC"]
        with pytest.raises(ValueError, match="demerger needs"):
            H.add_trade(c, "AAA", "demerger", DAYS[8], 110, 0, 0, other_code="BBB", ratio=1, cost_pct=0)
        with pytest.raises(ValueError, match="no BBB shares"):
            H.add_trade(c, "BBB", "return", DAYS[8], 10, 1, 0)


def test_capital_return_above_cost_base_is_a_gain_and_dividends_follow_units():
    seed({"CCC.AX": (5.0, 5.0)})
    with get_conn() as c:
        H.add_trade(c, "AAA", "buy", DAYS[0], 100, 1.0, 0)                # cost 100
        H.add_trade(c, "AAA", "return", DAYS[3], 100, 1.5, 0)             # 150 back: 50 above cost
        book = H.build(c)
        assert book.lots[0].cost == pytest.approx(0)
        assert book.realised[0]["gain"] == pytest.approx(50) and book.realised[0]["kind"].startswith("capital return")
        H.add_trade(c, "AAA", "drp", DAYS[5], 20, 1.0, 0)
        H.add_trade(c, "AAA", "demerger", DAYS[7], 120, 0, 0, other_code="CCC", ratio=1, cost_pct=50)
        upsert(c, "dividends", [{"code": "AAA", "ex_date": DAYS[6], "amount": 0.1, "currency": "AUD", "pay_date": DAYS[6]},
                                {"code": "CCC", "ex_date": DAYS[9], "amount": 0.2, "currency": "AUD", "pay_date": DAYS[9]}],
               ["code", "ex_date"])
        divs = {d["code"]: d for d in H.dividends(c, today=DAYS[-1])}
        assert divs["AAA"]["units"] == 120 and divs["CCC"]["units"] == 120


# --- tracked codes ------------------------------------------------------------------------

def test_tracked_codes_include_holdings_and_paper():
    from app.pipeline.ingest import tracked_codes
    from app.portfolio import paper
    seed({"CCC.AX": (5.0, 5.0)})
    with get_conn() as c:
        H.add_trade(c, "AAA", "buy", DAYS[0], 10, 10, 0)
        H.add_trade(c, "AAA", "demerger", DAYS[2], 10, 0, 0, other_code="CCC", ratio=1, cost_pct=10)
        pid = paper.create_portfolio(c, "P", capital=10_000)
        paper.place_order(c, pid, "BBB", "buy", quantity=1)
    assert {"AAA", "BBB", "CCC"} <= set(tracked_codes())


# --- AI summaries -----------------------------------------------------------------------------

class _Usage:
    input_tokens, output_tokens = 1200, 150


class _Resp:
    def __init__(self, parsed, stop="end_turn"):
        self.parsed_output, self.stop_reason, self.usage, self.model = parsed, stop, _Usage(), "claude-haiku-5-5"


class _Client:
    def __init__(self, stop="end_turn"):
        self.calls, self.stop = [], stop
        self.messages = self

    def parse(self, **kw):
        from app.news.ai import Summary
        self.calls.append(kw)
        return _Resp(Summary(summary="Profit rose 12% to A$1.2B; dividend up.", key_points=["Revenue A$5B", "DPS 50c"],
                             events=["earnings", "dividend"], tone="positive"), self.stop)


@pytest.fixture
def ai_on(monkeypatch):
    from app.jobs import JobContext
    from app.news import ai
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    get_settings.cache_clear()
    client = _Client()
    monkeypatch.setattr(ai, "_client", lambda: client)
    monkeypatch.setattr(ai.asx, "fetch_pdf", lambda http, ids: ("u", b"%PDF"))
    monkeypatch.setattr(ai, "PdfReader", lambda f: type("R", (), {"pages": [1, 2, 3]})())
    monkeypatch.setattr(ai, "pdf_to_text", lambda data, max_pages: "Half year results. Revenue A$5B. Profit up 12%. " * 5)
    return client, JobContext(job="announcements", run_id=0)


def test_ai_summaries_only_followed_ps_and_shown(ai_on):
    from app.news import ai
    from app.portfolio import watchlist
    client, ctx = ai_on
    seed()
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as c:
        watchlist.add(c, "AAA")
        upsert(c, "announcements", [
            {"ids_id": "x1", "code": "AAA", "published_at": now, "headline": "Half Year Results", "price_sensitive": 1},
            {"ids_id": "x2", "code": "AAA", "published_at": now, "headline": "Change of address", "price_sensitive": 0},
            {"ids_id": "x3", "code": "BBB", "published_at": now, "headline": "Results", "price_sensitive": 1}], ["ids_id"])
    ai.step_ai_summaries(ctx)
    assert len(client.calls) == 1 and "Half Year Results" in client.calls[0]["messages"][0]["content"]
    assert client.calls[0]["model"] == get_settings().llm_model
    with get_conn() as c:
        got = ai.for_ids(c, ["x1", "x2"])
        assert list(got) == ["x1"] and got["x1"]["tone"] == "positive" and got["x1"]["events"][0][0] == "earnings"
        assert ai.usage(c)["input_tokens"] == 1200
    ai.step_ai_summaries(ctx)                      # nothing new: no second call
    assert len(client.calls) == 1
    from fastapi.testclient import TestClient
    from app.main import app
    cl = TestClient(app)
    assert "AI summary" in cl.get("/company/AAA").text and "Profit rose 12%" in cl.get("/watchlist").text
    assert "summarises new price-sensitive" in cl.get("/status").text


def test_ai_refusal_is_recorded_not_retried(ai_on, monkeypatch):
    from app.news import ai
    client, ctx = ai_on
    client.stop = "refusal"
    seed()
    with get_conn() as c:
        from app.portfolio import watchlist
        watchlist.add(c, "AAA")
        upsert(c, "announcements", [{"ids_id": "r1", "code": "AAA", "published_at": datetime.now(timezone.utc).isoformat(),
                                     "headline": "Results", "price_sensitive": 1}], ["ids_id"])
    ai.step_ai_summaries(ctx)
    ai.step_ai_summaries(ctx)
    assert len(client.calls) == 1
    with get_conn() as c:
        assert "declined" in c.execute("SELECT error FROM ai_summaries WHERE ids_id='r1'").fetchone()[0]
        assert ai.for_ids(c, ["r1"]) == {}


def test_ai_off_without_key():
    from app.jobs import JobContext
    from app.news import ai
    assert not ai.enabled()
    ai.step_ai_summaries(JobContext(job="x", run_id=0))   # no-op, no error

"""Pipeline steps with adapters stubbed: storage, incremental updates, and
graceful handling of delisted tickers and outages."""
from app.db import get_conn, upsert
from app.jobs import run_job
from app.pipeline import ingest
from app.sources import universe, yahoo
from app.sources.base import NotFound, SourceError


def _seed_universe(codes):
    with get_conn() as c:
        upsert(c, "index_membership", [{"index_code": "asx200", "code": k, "as_of": "2026-11-11",
                                        "weight": 1.0, "source": "test", "approximate": 0,
                                        "fetched_at": "x"} for k in codes], pk=["index_code", "as_of", "code"])
        upsert(c, "companies", [{"code": k, "name": k, "status": "listed"} for k in codes], pk=["code"])


def test_delisted_ticker_marked_and_job_continues(monkeypatch):
    _seed_universe(["BHP", "GONE"])

    def fake_history(symbol, start=None):
        if symbol == "GONE.AX":
            raise NotFound("no price data")
        return ([{"date": "2026-11-11", "open": 1, "high": 1, "low": 1, "close": 1, "adj_close": 1, "volume": 5}],
                [{"ex_date": "2026-09-03", "amount": 0.9}], [])
    monkeypatch.setattr(yahoo, "fetch_history", fake_history)

    status = run_job("t", lambda ctx: ingest.step_stock_prices(ctx, ingest.tracked_codes()))
    assert status == "partial"
    with get_conn() as c:
        assert c.execute("SELECT status FROM companies WHERE code='GONE'").fetchone()[0] == "no_data"
        assert c.execute("SELECT COUNT(*) FROM prices WHERE symbol='BHP.AX'").fetchone()[0] == 1
        assert tuple(c.execute("SELECT amount, currency FROM dividends WHERE code='BHP'").fetchone()) == (0.9, "AUD")
        assert c.execute("SELECT COUNT(*) FROM errors WHERE item='GONE.AX'").fetchone()[0] == 1


def test_incremental_fetch_starts_near_last_date(monkeypatch):
    _seed_universe(["BHP"])
    with get_conn() as c:
        c.execute("INSERT INTO prices(symbol,date,close) VALUES('BHP.AX','2026-11-01',1)")
    seen = {}

    def fake_history(symbol, start=None):
        seen["start"] = start
        return ([{"date": "2026-11-11", "open": 1, "high": 1, "low": 1, "close": 2, "adj_close": 2, "volume": 1}], [], [])
    monkeypatch.setattr(yahoo, "fetch_history", fake_history)
    run_job("t", lambda ctx: ingest.step_stock_prices(ctx, ["BHP"]))
    assert seen["start"].isoformat() == "2026-10-22"


def test_universe_falls_back_and_records_errors(monkeypatch):
    monkeypatch.setattr(universe, "fetch_directory", lambda http: (_ for _ in ()).throw(SourceError("down")))
    snap = universe.IndexSnapshot("asx200", "2026-11-11", "ishares-ioz",
                                  [universe.Member("BHP", "BHP", 11.0, "Materials")])
    monkeypatch.setattr(universe, "fetch_index", lambda http, idx: snap)
    status = run_job("universe", ingest.step_universe)
    assert status == "partial"
    assert ingest.tracked_codes() == ["BHP"]
    with get_conn() as c:
        assert c.execute("SELECT sector FROM companies WHERE code='BHP'").fetchone()[0] == "Materials"


def test_job_exception_recorded_as_error():
    def boom(ctx):
        raise RuntimeError("kaput")
    assert run_job("boom", boom) == "error"
    with get_conn() as c:
        row = c.execute("SELECT status, message FROM job_runs WHERE job='boom'").fetchone()
    assert row["status"] == "error" and "kaput" in row["message"]


def test_upsert_keep_existing():
    with get_conn() as c:
        upsert(c, "companies", [{"code": "X", "sector": "Energy", "name": "a"}], pk=["code"])
        upsert(c, "companies", [{"code": "X", "sector": None, "name": "b"}], pk=["code"], keep_existing=["sector"])
        row = c.execute("SELECT sector, name FROM companies WHERE code='X'").fetchone()
    assert tuple(row) == ("Energy", "b")


def test_new_dividend_triggers_full_history_refetch(monkeypatch):
    """Yahoo re-scales adj_close for all earlier days after an ex-date; the stored history must follow."""
    _seed_universe(["BHP"])
    with get_conn() as c:
        for d, px in (("2026-10-28", 10.0), ("2026-10-29", 10.0), ("2026-11-02", 10.0)):
            c.execute("INSERT INTO prices(symbol,date,close,adj_close) VALUES('BHP.AX',?,?,?)", (d, px, px))
    calls = []

    def fake_history(symbol, start=None):
        calls.append(start)
        rows = [{"date": d, "open": 10, "high": 10, "low": 10, "close": 10, "adj_close": 9.5, "volume": 1}
                for d in ("2026-10-28", "2026-10-29")]
        rows.append({"date": "2026-11-11", "open": 10, "high": 10, "low": 10, "close": 10, "adj_close": 10, "volume": 1})
        if start is None:   # full history incl. an old day
            rows.insert(0, {"date": "2020-01-02", "open": 5, "high": 5, "low": 5, "close": 5, "adj_close": 4, "volume": 1})
        return rows, [{"ex_date": "2026-11-05", "amount": 0.5}], []
    monkeypatch.setattr(yahoo, "fetch_history", fake_history)
    run_job("t", lambda ctx: ingest.step_stock_prices(ctx, ["BHP"]))
    assert len(calls) == 2 and calls[1] is None
    with get_conn() as c:
        assert c.execute("SELECT adj_close FROM prices WHERE symbol='BHP.AX' AND date='2026-10-28'").fetchone()[0] == 9.5
        assert c.execute("SELECT COUNT(*) FROM prices WHERE symbol='BHP.AX' AND date='2020-01-02'").fetchone()[0] == 1


def test_unchanged_adjustment_does_not_refetch(monkeypatch):
    _seed_universe(["BHP"])
    with get_conn() as c:
        c.execute("INSERT INTO prices(symbol,date,close,adj_close) VALUES('BHP.AX','2026-10-29',10,9.5)")
        c.execute("INSERT INTO prices(symbol,date,close,adj_close) VALUES('BHP.AX','2026-11-02',10,9.0)")  # mid-session bar
    calls = []

    def fake_history(symbol, start=None):
        calls.append(start)
        return ([{"date": "2026-10-29", "open": 10, "high": 10, "low": 10, "close": 10, "adj_close": 9.5, "volume": 1},
                 {"date": "2026-11-02", "open": 10, "high": 10, "low": 10, "close": 10.4, "adj_close": 10.4, "volume": 1}],
                [], [])
    monkeypatch.setattr(yahoo, "fetch_history", fake_history)
    run_job("t", lambda ctx: ingest.step_stock_prices(ctx, ["BHP"]))
    assert len(calls) == 1

"""Phase 5: point-in-time features, purged walk-forward, backtest engine, metrics, signals."""
import math

import numpy as np
import pandas as pd
import pytest

from app.db import get_conn, upsert
from app.models import backtest as B
from app.models import features as F
from app.models import signals as S
from app.models import train as T

NOW = "2026-11-13T00:00:00+00:00"


def _bdays(start: str, n: int) -> list[str]:
    return list(pd.bdate_range(start, periods=n).strftime("%Y-%m-%d"))


def seed(n_stocks: int = 25, n_days: int = 1600, start: str = "2019-01-01", seed_: int = 0) -> list[str]:
    """Synthetic market: index + STW + n stocks with random walks, announcements, shorts, dividends."""
    rng = np.random.default_rng(seed_)
    days = _bdays(start, n_days)
    rows = []
    def series(sym, px, vol=1e6):
        for d, p in zip(days, px):
            rows.append({"symbol": sym, "date": d, "open": p * 0.999, "high": p * 1.01, "low": p * 0.99, "close": p,
                         "adj_close": p, "volume": vol, "fetched_at": NOW})
    mkt = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, n_days)))
    series("^AXJO", mkt * 70)
    series("STW.AX", mkt * 0.6)
    codes = [f"S{i:02d}" for i in range(n_stocks)]
    comps = []
    for i, code in enumerate(codes):
        px = 5 + i * 0.5 + np.cumsum(rng.normal(0, 0.05, n_days))
        px = np.maximum(px, 1.0)
        series(f"{code}.AX", px)
        comps.append({"code": code, "name": f"STOCK {i}", "sector": "Materials" if i % 2 else "Financials",
                      "status": "listed", "yahoo_symbol": f"{code}.AX"})
    with get_conn() as c:
        upsert(c, "prices", rows, ["symbol", "date"])
        upsert(c, "companies", comps, ["code"])
        upsert(c, "index_membership", [{"index_code": "asx200", "code": k, "as_of": days[-1], "weight": 1.0}
                                       for k in codes], ["index_code", "as_of", "code"])
        upsert(c, "macro_series", [{"series": "cash_rate", "date": days[0], "value": 1.5},
                                   {"series": "cash_rate", "date": days[600], "value": 4.0}], ["series", "date"])
        anns, shorts, divs = [], [], []
        for i, code in enumerate(codes):
            for k in range(0, n_days, 37):
                anns.append({"ids_id": f"{code}{k:05d}", "code": code, "published_at": f"{days[k]}T02:00:00+00:00",
                             "headline": "Half Year Results" if k % 2 else "Trading Halt",
                             "price_sensitive": 1, "tagged": 0})
            for k in range(0, n_days, 5):
                shorts.append({"code": code, "date": days[k], "short_pct": 1 + (k % 50) / 10,
                               "available_at": days[min(k + 4, n_days - 1)], "fetched_at": NOW})
            for k in range(100, n_days, 125):
                divs.append({"code": code, "ex_date": days[k], "amount": 0.1, "fetched_at": NOW})
        upsert(c, "announcements", anns, ["ids_id"])
        upsert(c, "short_positions", shorts, ["code", "date"])
        upsert(c, "dividends", divs, ["code", "ex_date"])
    from app.jobs import JobContext
    from app.pipeline import news
    news.step_tag_announcements(JobContext("t", 0))
    return codes


# --- features ------------------------------------------------------------------

def test_features_are_point_in_time():
    codes = seed(n_stocks=6, n_days=900)
    with get_conn() as c:
        cal = F.market_calendar(c)
        cutoff = cal[700]
        dates = cal[600:701:5]
        full = F.build(c, codes, dates=dates)
        cut = F.build(c, codes, end=cutoff, dates=dates)
    assert not full.empty and len(full) == len(cut)
    cols = [f for f in F.FEATURES if f not in ("sector_id",)]
    a = full.set_index(["code", "date"])[cols].sort_index()
    b = cut.set_index(["code", "date"])[cols].sort_index()
    pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=1e-9)       # future data changes nothing
    # targets do look forward: they exist with full data and not after the cutoff
    assert full[full.date == dates[-1]]["fwd_ret_21"].notna().all()
    assert cut[cut.date == dates[-1]]["fwd_ret_21"].isna().all()


def test_targets_and_baseline():
    codes = seed(n_stocks=3, n_days=900)
    with get_conn() as c:
        df = F.build(c, codes, sample_every=1, start="2020-06-01")
        px = pd.read_sql_query("SELECT date, adj_close FROM prices WHERE symbol='S01.AX' ORDER BY date", c).set_index("date")
        stw = pd.read_sql_query("SELECT date, adj_close FROM prices WHERE symbol='STW.AX' ORDER BY date", c).set_index("date")
    r = df[df.code == "S01"].set_index("date")
    d = r.index[100]
    i = px.index.get_loc(d)
    exp = px["adj_close"].iloc[i + 21] / px["adj_close"].iloc[i] - 1
    assert r.loc[d, "fwd_ret_21"] == pytest.approx(exp)
    assert r.loc[d, "fwd_ex_21"] == pytest.approx(exp - (stw["adj_close"].iloc[i + 21] / stw["adj_close"].iloc[i] - 1))
    assert 0 <= r["base_pbeat_21"].dropna().min() and r["base_pbeat_21"].dropna().max() <= 1
    assert (r["base_q10_21"] <= r["base_q90_21"]).all()


def test_announcement_after_close_counts_next_day():
    s = pd.Series(["2026-03-02T04:00:00+00:00",    # 15:00 Sydney -> same day
                   "2026-03-02T05:30:00+00:00",    # 16:30 Sydney -> next day
                   "2026-03-02T22:00:00+00:00"])   # 09:00 Sydney on 3 Mar
    days = F._local_effective_day(s)
    assert [str(d) for d in days] == ["2026-03-02", "2026-03-03", "2026-03-03"]


def test_window_counts():
    ev = np.array(["2026-01-01", "2026-01-10", "2026-01-20"], dtype="datetime64[D]")
    at = np.array(["2026-01-10", "2026-01-31", "2025-12-31"], dtype="datetime64[D]")
    assert list(F._window_counts(ev, np.ones(3), at, 30)) == [2, 2, 0]   # (t-30, t]
    assert list(F._window_counts(ev, np.array([1.0, 2.0, 4.0]), at, 10)) == [3, 0, 0]   # (t-10, t]


# --- walk-forward purge -----------------------------------------------------------

def test_walk_forward_purges_overlapping_labels(monkeypatch):
    dates = _bdays("2020-01-01", 400)
    pos = {d: i for i, d in enumerate(dates)}
    sample = dates[::5]
    df = pd.DataFrame([{"code": f"C{k}", "date": d, "eligible": True, "fwd_ret_21": 0.01 * (k - 5),
                        "fwd_ex_21": 0.0, **{f: 0.0 for f in F.FEATURES}} for d in sample for k in range(30)])
    seen = []

    class Dummy:
        def predict(self, X):
            return np.zeros(len(X))

    def fake_fit(tr, feats, spec, h):
        seen.append((tr["date"].max(), spec.name))
        return Dummy()
    monkeypatch.setattr(T, "fit", fake_fit)
    monkeypatch.setattr(T, "MIN_TRAIN_ROWS", 100)
    retrain = [d for d in sample if d >= dates[300]][::4]
    T.walk_forward(df, pos, 21, "news", retrain)
    assert seen
    for i, r in enumerate(retrain):
        latest_train = [d for d, _ in seen[i * 6:(i + 1) * 6]]
        assert all(pos[d] + 21 <= pos[r] for d in latest_train)            # labels known at R
        assert any(pos[d] + 21 > pos[r] - 5 for d in latest_train)          # but nothing more is dropped


# --- metrics ------------------------------------------------------------------------

def test_auc_pinball_calibration():
    assert T.auc(np.array([0, 0, 1, 1]), np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    assert T.auc(np.array([0, 0, 1, 1]), np.array([0.9, 0.8, 0.2, 0.1])) == 0.0
    assert T.auc(np.array([0, 1, 0, 1]), np.array([0.5, 0.5, 0.5, 0.5])) == 0.5
    assert T.auc(np.array([1, 1]), np.array([0.1, 0.2])) is None
    assert T.pinball(np.array([1.0]), np.array([0.0]), 0.9) == pytest.approx(0.9)
    assert T.pinball(np.array([0.0]), np.array([1.0]), 0.9) == pytest.approx(0.1)
    cal = T.calibration(np.linspace(0, 1, 100), np.linspace(0, 1, 100) > 0.5, bins=4)
    assert sum(b["n"] for b in cal) == 100 and cal[0]["realised"] == 0 and cal[-1]["realised"] == 1


# --- calibration ------------------------------------------------------------------------

def test_probability_calibration_fixes_overconfidence_and_flattens_no_skill():
    from app.models import calibrate as C
    rng = np.random.default_rng(1)
    true = rng.uniform(0.3, 0.7, 40_000)
    y = rng.random(40_000) < true
    raw = 0.5 + 2.0 * (true - 0.5)                    # overconfident: 30% shown as 10%
    knots = C.fit_prob(raw[:20_000], y[:20_000])
    cal = C.apply_prob(raw[20_000:], knots)
    assert np.all(np.diff(knots[1]) >= 0)             # monotone: order of stocks unchanged
    assert np.mean(np.abs(cal - true[20_000:])) < 0.03 < np.mean(np.abs(raw[20_000:] - true[20_000:]))
    noise = rng.uniform(0.3, 0.7, 20_000)            # no skill: outcome unrelated to the score
    yb = rng.random(20_000) < 0.47
    flat = C.apply_prob(noise, C.fit_prob(noise, yb))
    assert flat.max() - flat.min() < 0.06 and abs(flat.mean() - 0.47) < 0.01


def test_range_calibration_reaches_80_percent():
    from app.models import calibrate as C
    rng = np.random.default_rng(2)
    sd = rng.uniform(0.05, 0.2, 40_000)
    y = rng.normal(0, sd)
    q10, q50, q90 = -0.6 * sd, np.zeros_like(sd), 0.6 * sd   # too narrow (true 10/90% = ±1.28 sd)
    assert ((y >= q10) & (y <= q90)).mean() < 0.5
    off = C.fit_range(q10[:20_000], q90[:20_000], y[:20_000])
    lo, hi = C.apply_range(q10[20_000:], q50[20_000:], q90[20_000:], off)
    yt = y[20_000:]
    assert abs((yt < lo).mean() - 0.10) < 0.015 and abs((yt > hi).mean() - 0.10) < 0.015


def test_calibration_walk_forward_uses_only_known_outcomes():
    from app.models import calibrate as C
    days = _bdays("2020-01-01", 300)
    cal_pos = {d: i for i, d in enumerate(days)}
    rng = np.random.default_rng(3)
    rows = [{"code": f"C{k}", "date": d} for d in days for k in range(30)]
    preds = pd.DataFrame(rows)
    preds["pup"] = rng.uniform(0.3, 0.7, len(preds))
    preds["q10"], preds["q50"], preds["q90"] = -0.05, 0.0, 0.05
    df = preds[["code", "date"]].assign(eligible=True, fwd_ret_21=rng.normal(0, 0.1, len(preds)))
    df["fwd_ex_21"] = df["fwd_ret_21"]
    retrain = days[::20]
    monkey = C.MIN_ROWS
    try:
        C.MIN_ROWS = 500
        a = C.walk_forward(preds, df, cal_pos, 21, retrain)
        late = df["date"] >= days[200]                       # change outcomes after day 200 only
        df2 = df.copy()
        df2.loc[late, "fwd_ret_21"] = 5.0
        b = C.walk_forward(preds, df2, cal_pos, 21, retrain)
    finally:
        C.MIN_ROWS = monkey
    key = ["code", "date"]
    a, b = a.sort_values(key).reset_index(drop=True), b.sort_values(key).reset_index(drop=True)
    early = a["date"] < days[221]                             # their calibrators only saw outcomes known by then
    assert np.allclose(a.loc[early, "q90_cal"], b.loc[early, "q90_cal"])
    assert not np.allclose(a.loc[~early, "q90_cal"], b.loc[~early, "q90_cal"])
    assert not a.loc[a["date"] < days[40], "cal_ok"].any() and a.loc[a["date"] >= days[100], "cal_ok"].all()


def test_signals_rank_on_raw_score_not_calibrated_ties():
    rows = [{"code": "A", "eligible": True, "p_beat": 0.48, "score": 0.55, "ex50": 0.0},
            {"code": "B", "eligible": True, "p_beat": 0.48, "score": 0.60, "ex50": 0.0}]
    S.assign(rows, top_n=1, hurdle=0.01, model_auc=0.5)
    assert {r["code"]: r["rank"] for r in rows} == {"B": 1, "A": 2}
    assert all(r["signal"] == "Hold" for r in rows)         # calibrated 48% is not a Buy


# --- backtest engine ------------------------------------------------------------------

def _px(dates, series: dict[str, list[float]], opens: dict[str, list[float]] | None = None):
    close = pd.DataFrame(series, index=dates)
    op = pd.DataFrame(opens, index=dates) if opens else close.copy()
    return {"open": op, "close": close, "adj": close.copy()}


def test_backtest_engine_known_answer():
    dates = _bdays("2024-01-01", 6)
    px = _px(dates, {"A": [10, 10, 11, 12, 13, 14], "B": [20, 20, 20, 20, 20, 20]},
             opens={"A": [10, 10.5, 11, 12, 13, 14], "B": [20] * 6})
    cfg = B.Config(capital=10_000, top_n=1, brokerage=5, slippage_bps=0, min_trade=100)
    res = B.run("t", {dates[0]: ["A"], dates[3]: ["B"]}, px, dates, cfg)
    # day1: buy A at the open (10.5): floor((10000-5)/10.5)=951 shares, cash 10000-5-951*10.5
    cash = 10_000 - 5 - 951 * 10.5
    assert res.equity.iloc[0] == 10_000
    assert res.equity.iloc[1] == pytest.approx(cash + 951 * 10)
    # day4 open: sell A at 13, buy B at 20
    cash += 951 * 13 - 5
    n_b = math.floor((min(cash / 1, cash) - 5) / 20)
    cash -= n_b * 20 + 5
    assert res.equity.iloc[4] == pytest.approx(cash + n_b * 20)
    assert res.trades == 3 and res.fees == 15
    assert res.picks == {dates[0]: ["A"], dates[3]: ["B"]}


def test_backtest_slippage_and_skip_suspended():
    dates = _bdays("2024-01-01", 4)
    px = _px(dates, {"A": [10, 10, 10, 10], "C": [5, np.nan, np.nan, 5]})
    cfg = B.Config(capital=1_000, top_n=2, brokerage=0, slippage_bps=100, min_trade=10)
    res = B.run("t", {dates[0]: ["A", "C"]}, px, dates, cfg)
    n = math.floor(500 / (10 * 1.01))
    assert res.trades == 1                                   # C has no price on the execution day
    assert res.equity.iloc[-1] == pytest.approx(1_000 - n * 10 * 1.01 + n * 10)


def test_buy_and_hold_and_metrics():
    dates = _bdays("2020-01-01", 520)
    adj = pd.Series(100 * 1.0005 ** np.arange(520), index=dates)
    cfg = B.Config(capital=10_000, brokerage=5, slippage_bps=0)
    bh = B.buy_and_hold("bh", adj, adj, adj, dates, cfg, dates[1])
    m = B.metrics(bh, None, None, [])
    assert m["max_drawdown"] == pytest.approx(-5 / 10_000, abs=1e-4)   # only the day-1 brokerage
    assert m["cagr"] > 0 and m["trades"] == 1
    eq = pd.Series([100, 120, 90, 110], index=_bdays("2020-01-01", 4))
    m = B.metrics(B.Result("x", eq), None, None, [])
    assert m["max_drawdown"] == pytest.approx(-0.25) and m["total_return"] == pytest.approx(0.10)


def test_top_n_picks_only_eligible():
    s = pd.DataFrame({"code": ["A", "B", "C"], "date": ["d"] * 3, "eligible": [True, False, True],
                      "score": [0.9, 0.99, 0.1]})
    assert B.top_n_picks(s, "score", ["d"], 2) == {"d": ["A", "C"]}
    assert B.top_n_picks(s, "score", ["d"], 1, higher_is_better=False) == {"d": ["C"]}


# --- signals ------------------------------------------------------------------------

def test_signal_rules():
    hurdle = S.cost_hurdle(50_000, 10, 5, 10)
    assert hurdle == pytest.approx(2 * 5 / 5_000 + 0.002)
    rows = [{"code": f"X{i}", "eligible": True, "p_beat": p, "ex50": e}
            for i, (p, e) in enumerate([(0.62, 0.02), (0.58, 0.001), (0.50, 0.0), (0.42, -0.03), (0.40, -0.001)])]
    rows.append({"code": "ILLQ", "eligible": False, "p_beat": 0.9, "ex50": 0.1})
    S.assign(rows, top_n=2, hurdle=hurdle, model_auc=0.55)
    sig = {r["code"]: (r["signal"], r["confidence"]) for r in rows}
    assert sig["X0"] == ("Buy", "high")
    assert sig["X1"][0] == "Hold"            # top-2 but below the cost hurdle
    assert sig["X3"] == ("Sell", "medium")
    assert sig["X4"][0] == "Hold"            # bottom-2 but the expected loss is below the hurdle
    assert sig["ILLQ"][0] == "Hold" and rows[-1]["rank"] is None
    S.assign(rows, top_n=2, hurdle=hurdle, model_auc=0.50)
    assert all(r["confidence"] == "low" for r in rows)        # no proven skill -> low confidence


def test_explain_orders_contributions():
    feats = ["mom_12_1", "ps_sent_30d", "vol_60d"]
    why = S.explain(np.array([0.3, -0.2, 0.1]), feats, {"mom_12_1": 0.25, "ps_sent_30d": -0.5, "vol_60d": 0.3})
    assert [x["feature"] for x in why["for"]] == ["mom_12_1", "vol_60d"]
    assert why["against"][0]["feature"] == "ps_sent_30d" and why["against"][0]["news"] is True
    assert why["for"][0]["value"] == "+25.0%"                       # returns are signed
    assert S.fmt_value("vol_60d", 0.3) == "30.0%"                  # volatility isn't
    assert S.fmt_value("days_since_exdiv", float("nan")) == "no dividend on record"


# --- end to end ----------------------------------------------------------------------

def test_train_job_end_to_end(monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.jobs import run_job
    from app.main import app
    from app.pipeline.ingest import JOBS

    monkeypatch.setattr(T, "ROUNDS", 15)
    monkeypatch.setattr(T, "MIN_TRAIN_ROWS", 300)
    monkeypatch.setitem(T.PARAMS, "min_data_in_leaf", 50)
    monkeypatch.setenv("BACKTEST_START", "2022-01-01")
    monkeypatch.setenv("BACKTEST_TOP_N", "5")
    get_settings.cache_clear()
    monkeypatch.setattr(F, "MIN_TURNOVER", 0)
    from app.pipeline import forecast as FC
    monkeypatch.setattr(FC, "LARGE_N", 12)
    seed(n_stocks=25, n_days=1300)
    monkeypatch.setattr(T, "walk_forward", _small_wf(T.walk_forward))
    monkeypatch.setattr(T, "HOLDOUT_START", "2022-04-01")
    from app.models import calibrate as C
    monkeypatch.setattr(C, "MIN_ROWS", 50)
    assert run_job("train", JOBS["train"]) == "ok"
    with get_conn() as c:
        run = dict(c.execute("SELECT * FROM backtest_runs ORDER BY id DESC LIMIT 1").fetchone())
        n_fc = c.execute("SELECT COUNT(*) FROM forecasts WHERE horizon=21").fetchone()[0]
        sigs = {r[0] for r in c.execute("SELECT DISTINCT signal FROM forecasts WHERE horizon=21")}
    assert run["status"] == "ok" and n_fc == 25 and sigs <= {"Buy", "Hold", "Sell"}
    import json
    summ = json.loads(run["summary_json"])
    assert "ASX 200 buy-and-hold (STW, incl. dividends)" in summ["strategies"]
    assert summ["strategies"]["Model (with news)"]["trades"] > 0
    assert "survivorship" in summ and summ["production_variant"] in ("news", "no_news")
    assert summ["random_control"]["runs"] == 20
    assert any(k.startswith("Random 5") for k in summ["strategies"])
    assert summ["large_only"]["random_control"]["runs"] == 20 and summ["large_only"]["strategies"]
    q21 = summ["quality"][f"21_{summ['production_variant']}"]
    assert q21["dev"]["to"] < "2022-04-01" <= q21["holdout"]["from"]
    assert q21["holdout"]["cal_rows"] > 0 and "coverage_cal" in q21["holdout"]
    assert summ["model_auc_beat"] == q21["holdout"]["auc_beat"] and summ["fingerprint"]
    assert summ["holdout_strategies"][BENCH]
    meta = T.load_models(FC.models_root())[1]
    assert meta["calibrators"]["21"]["pbeat"] and meta["fingerprint"] == summ["fingerprint"]
    assert run_job("forecast", JOBS["forecast"]) == "ok"
    with get_conn() as c:
        fc = [dict(r) for r in c.execute("SELECT * FROM forecasts WHERE horizon=21")]
    assert all(r["calibrated"] == 1 and r["p_beat_raw"] is not None for r in fc)
    assert all(r["q10"] <= r["q50"] <= r["q90"] for r in fc)
    ranked = sorted((r for r in fc if r["rank"]), key=lambda r: r["rank"])
    assert [r["p_beat_raw"] for r in ranked] == sorted((r["p_beat_raw"] for r in ranked), reverse=True)
    with TestClient(app) as c:
        r = c.get("/backtest")
        assert r.status_code == 200 and "Survivorship" in r.text and "buy-and-hold" in r.text
        assert "Locked holdout" in r.text and "only model design scored" in r.text
        assert "brokerage" in r.text.lower()
        r = c.get("/signals")
        assert r.status_code == 200 and "S01" in r.text and "not financial advice" in r.text
        assert c.get("/api/backtest/latest").json()["status"] == "ok"
        page = c.get("/company/S01").text
        assert "Forecast" in page and "chance" in page.lower()


BENCH = "ASX 200 buy-and-hold (STW, incl. dividends)"


def _small_wf(orig):
    def wf(df, cal_pos, h, variant, retrain, progress=None):
        return orig(df, cal_pos, h, variant, retrain[:6], progress)
    return wf


def test_backtest_verdict():
    from app.main import BENCH_NAME, backtest_verdict
    base = {"strategies": {BENCH_NAME: {"cagr": 0.08}, "Model (with news)": {"cagr": 0.25}},
            "random_control": {"cagr_p10": 0.15, "cagr_p50": 0.22, "cagr_p90": 0.28}}
    v = backtest_verdict(base)
    assert v["level"] == "bad" and "survivorship" in v["text"]          # beats STW but not random picks
    base["strategies"]["Model (with news)"]["cagr"] = 0.05
    assert "did not beat" in backtest_verdict(base)["text"]
    base["strategies"]["Model (with news)"]["cagr"] = 0.35
    assert backtest_verdict(base)["level"] == "warn"
    base["large_only"] = {"n": 100, "strategies": {"Model (with news), top 100 only": {"cagr": 0.09}},
                          "random_control": {"cagr_p10": 0.05, "cagr_p50": 0.08, "cagr_p90": 0.11}}
    v = backtest_verdict(base)
    assert v["level"] == "bad" and "disappears" in v["text"]           # edge only among former small caps
    base["large_only"]["strategies"]["Model (with news), top 100 only"]["cagr"] = 0.20
    assert backtest_verdict(base)["level"] == "warn"
    assert backtest_verdict(None) is None

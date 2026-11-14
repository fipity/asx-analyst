"""Indicators + scoring: known answers and a look-ahead guard."""
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.analysis import indicators, scoring


def frame(closes, volume=None, adj=None):
    n = len(closes)
    days = pd.bdate_range("2020-01-01", periods=n).strftime("%Y-%m-%d")
    return pd.DataFrame({
        "date": days, "high": closes, "low": closes, "close": closes,
        "adj_close": adj if adj is not None else closes,
        "volume": volume if volume is not None else [1000.0] * n,
    })


def test_sma_ema_and_trend():
    closes = [float(i) for i in range(1, 301)]          # 1..300 rising
    ind = indicators.compute(frame(closes))
    last = ind.iloc[-1]
    assert last["sma20"] == pytest.approx(np.mean(closes[-20:]))
    assert last["sma200"] == pytest.approx(np.mean(closes[-200:]))
    assert pd.isna(ind["sma200"].iloc[198]) and not pd.isna(ind["sma200"].iloc[199])
    # EMA of a straight line lags it by (span-1)/2 once warmed up
    assert last["ema20"] == pytest.approx(300 - 9.5, abs=0.01)
    v = indicators.latest(frame(closes))
    assert v["trend"] == "up" and v["date"] == ind.index[-1]
    assert indicators.latest(frame(closes[::-1]))["trend"] == "down"


def test_rsi_known_values():
    up = indicators.compute(frame([float(i) for i in range(1, 60)]))
    assert up["rsi14"].iloc[-1] == pytest.approx(100)
    flat = indicators.compute(frame([10.0] * 60))
    assert flat["rsi14"].iloc[-1] == pytest.approx(50)
    assert pd.isna(flat["rsi14"].iloc[12])
    # alternating +1/-1: average gain == average loss -> ~50
    alt = indicators.compute(frame([10.0 + (i % 2) for i in range(200)]))
    assert alt["rsi14"].iloc[-1] == pytest.approx(50, abs=3)


def test_macd_bollinger_and_constant_series():
    ind = indicators.compute(frame([10.0] * 100))
    last = ind.iloc[-1]
    assert last["macd"] == pytest.approx(0) and last["macd_hist"] == pytest.approx(0)
    assert last["bb_upper"] == pytest.approx(10) and pd.isna(last["bb_pctb"])   # zero-width band
    assert last["vol_20d"] == pytest.approx(0)
    closes = [10.0 + (1 if i % 2 else -1) for i in range(40)]
    b = indicators.compute(frame(closes)).iloc[-1]
    assert b["bb_upper"] == pytest.approx(12) and b["bb_lower"] == pytest.approx(8)   # sd = 1


def test_momentum_and_volatility_use_adjusted_prices():
    n = 300
    adj = [100 * 1.001 ** i for i in range(n)]
    closes = [a * (0.9 if i >= 280 else 1.0) for i, a in enumerate(adj)]   # a dividend-like drop in close only
    ind = indicators.compute(frame(closes, adj=adj)).iloc[-1]
    assert ind["mom_12m"] == pytest.approx(1.001 ** 252 - 1)
    assert ind["mom_12_1"] == pytest.approx(1.001 ** 231 - 1)
    assert ind["vol_60d"] == pytest.approx(0, abs=1e-9)
    # daily log-return sd of 1% -> ~15.9% annualised
    rng = np.random.default_rng(0)
    r = rng.normal(0, 0.01, 400)
    px = list(100 * np.exp(np.cumsum(r)))
    vol = indicators.compute(frame(px)).iloc[-1]["vol_60d"]
    assert vol == pytest.approx(np.std(r[-60:], ddof=1) * math.sqrt(252), rel=1e-6)


def test_volume_z_excludes_today():
    vols = [1000.0] * 70
    vols[30] = 1200.0                                      # some spread in the window
    vols[-1] = 10000.0
    ind = indicators.compute(frame([10.0] * 70, volume=vols)).iloc[-1]
    assert ind["vol_z"] > 5


def test_no_look_ahead():
    rng = np.random.default_rng(1)
    px = list(50 * np.exp(np.cumsum(rng.normal(0, 0.02, 500))))
    vol = list(rng.uniform(1e5, 1e6, 500))
    full = indicators.compute(frame(px, volume=vol))
    cut = indicators.compute(frame(px[:350], volume=vol[:350]))
    pd.testing.assert_frame_equal(full.iloc[:350], cut)


def test_describe_is_plain_english():
    v = indicators.latest(frame([float(i) for i in range(1, 301)]))
    text = " ".join(indicators.describe(v))
    assert "Uptrend" in text and "RSI 100" in text and "52-week high" in text
    assert indicators.describe({}) == []


# --- scoring -----------------------------------------------------------------

def test_percentiles_ties_and_direction():
    p = scoring.percentiles({"a": 1, "b": 2, "c": 2, "d": 3}, higher_is_better=True)
    assert p == {"a": 12.5, "b": 50.0, "c": 50.0, "d": 87.5}
    q = scoring.percentiles({"a": 1, "b": 3}, higher_is_better=False)
    assert q["a"] > q["b"]
    assert scoring.percentiles({"x": 5}, True) == {"x": 50.0}


def test_annual_and_dividend_metrics():
    fins = [{"period_end": f"{y}-06-30", "revenue": 100 * 1.1 ** k, "diluted_eps": 1.0 * 1.2 ** k,
             "net_income": (-5 if k == 0 else 10)} for k, y in enumerate(range(2020, 2025))]
    m = scoring.annual_metrics(fins)
    assert m["rev_cagr"] == pytest.approx(0.1)            # last 4 points, 3 years
    assert m["eps_cagr"] == pytest.approx(0.2)
    assert m["profitable_years"] == pytest.approx(4 / 5)
    assert scoring.annual_metrics(fins[:2])["rev_cagr"] is None
    as_of = date(2026, 11, 12)
    d = lambda days: (as_of - timedelta(days=days)).isoformat()
    divs = [(d(30), 0.6), (d(200), 0.5), (d(400), 0.5), (d(560), 0.5), (d(1500), 0.4)]
    dm = scoring.dividend_metrics(divs, as_of)
    assert dm["div_growth"] == pytest.approx(1.1 / 1.0 - 1)
    assert dm["years_paid"] == 3
    assert scoring.dividend_metrics([], as_of) == {"div_growth": None, "years_paid": 0.0}


def test_score_sector_ranks_with_shrinkage():
    def co(sector, pe, roe, growth, gy):
        return {"sector": sector, "metrics": {"earnings_yield": 1 / pe, "roe": roe, "revenue_growth": growth,
                                               "gross_yield": gy}}
    comps = {
        # Financials: F1 is best on everything
        "F1": co("Financials", 8, 0.20, 0.10, 0.08), "F2": co("Financials", 12, 0.12, 0.05, 0.06),
        "F3": co("Financials", 20, 0.08, 0.01, 0.03),
        # Materials are cheaper and more profitable across the board
        "M1": co("Materials", 5, 0.30, 0.20, 0.10), "M2": co("Materials", 6, 0.25, 0.15, 0.09),
    }
    s = scoring.score(comps)
    assert s["F1"]["sector_rank"] == 1 and s["F3"]["sector_rank"] == 3 and s["F1"]["sector_ranked"] == 3
    assert s["F1"]["composite"] > s["F2"]["composite"] > s["F3"]["composite"]
    # shrinkage: F1 is top of its sector but below both Materials in the universe -> not 100
    pure_sector = (3 - 0.5) / 3 * 100
    assert s["F1"]["valuation"] < pure_sector
    assert s["F1"]["coverage"]["valuation"] == "1/4"
    assert s["F1"]["detail"]["roe"]["value"] == 0.20
    for r in s.values():
        for k in scoring.CATEGORIES + ["composite"]:
            assert r[k] is None or 0 <= r[k] <= 100


def test_composite_needs_three_categories():
    s = scoring.score({"A": {"sector": "X", "metrics": {"roe": 0.1, "gross_yield": 0.05}},
                       "B": {"sector": "X", "metrics": {"roe": 0.2}}})
    assert s["A"]["composite"] is None and s["A"]["quality"] is not None


def test_loss_maker_ranks_last_on_earnings_yield():
    vals = scoring.metric_values({"pe": None, "loss_making": True}, {}, {})
    assert vals["earnings_yield"] < 0
    assert scoring.metric_values({"pe": None}, {}, {})["earnings_yield"] is None
    # non-payer: gross yield counts as 0 rather than missing
    assert scoring.metric_values({}, {}, {"years_paid": 0.0})["gross_yield"] == 0.0

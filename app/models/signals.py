"""Turn model outputs into Buy/Hold/Sell with a confidence label and a plain-English 'why'.

Signal rule (the same rule the backtest's model strategy follows):
- Buy:  among the top-N eligible stocks by P(beat the ASX 200 over ~1 month), with
        P > 0.5 and an expected excess return above the round-trip cost hurdle.
- Sell: among the bottom-N, with P < 0.5 and expected excess below minus the hurdle.
- Hold: everything else, and anything not liquid enough to trade (ineligible).
Cost hurdle = 2 x brokerage / position size + 2 x slippage.

Confidence is capped by the model's own walk-forward track record: if its AUC for beating
the market on the locked holdout (see train.HOLDOUT_START) is below 0.52, every signal is "low".
P is the calibrated probability, so P > 0.5 means "came true more often than not" in testing.
"""
from __future__ import annotations

import math

import numpy as np

AUC_SKILL = 0.52

# feature -> (plain-English name, format). Formats: pct, pp, num, days, rank, count, sent
FEATURE_TEXT: dict[str, tuple[str, str]] = {
    "rsi14": ("RSI (14-day)", "num"), "macd_hist_pct": ("MACD momentum", "spct"),
    "bb_pctb": ("position in its Bollinger band", "num"), "bb_width": ("Bollinger band width", "pct"),
    "vol_z": ("trading volume vs normal", "num"), "dist_52w_high": ("distance from 52-week high", "spct"),
    "dist_52w_low": ("distance above 52-week low", "spct"), "mom_1m": ("1-month return", "spct"),
    "mom_3m": ("3-month return", "spct"), "mom_6m": ("6-month return", "spct"), "mom_12m": ("12-month return", "spct"),
    "mom_12_1": ("12-month momentum (excl. last month)", "spct"), "vol_20d": ("20-day volatility", "pct"),
    "vol_60d": ("60-day volatility", "pct"), "px_sma50": ("price vs 50-day average", "spct"),
    "px_sma200": ("price vs 200-day average", "spct"), "sma50_sma200": ("50-day vs 200-day average", "spct"),
    "log_turnover": ("trading liquidity", "num"), "ret_5d": ("last week's return", "spct"),
    "earnings_yield": ("earnings yield", "pct"), "book_yield": ("book yield", "pct"),
    "fcf_yield": ("free-cash-flow yield", "pct"), "roe": ("return on equity", "pct"),
    "net_margin": ("net margin", "pct"), "debt_equity": ("debt/equity", "num"),
    "rev_growth": ("revenue growth", "spct"), "eps_growth": ("EPS growth", "spct"),
    "div_yield_ttm": ("trailing dividend yield", "pct"), "div_count_ttm": ("dividends in the last year", "count"),
    "days_since_exdiv": ("days since last ex-dividend", "days"), "short_pct": ("short interest", "pp"),
    "short_chg20": ("change in short interest", "pp"), "dir_net_buy_90d": ("net director buying (90 days)", "aud"),
    "dir_buys_90d": ("director buys (90 days)", "count"), "dir_sells_90d": ("director sells (90 days)", "count"),
    "ann_30d": ("announcements in the last 30 days", "count"), "ann_activity": ("announcement activity vs usual", "num"),
    "ps_7d": ("price-sensitive announcements (7 days)", "count"),
    "ps_30d": ("price-sensitive announcements (30 days)", "count"),
    "ps_sent_7d": ("tone of price-sensitive announcements (7 days)", "sent"),
    "ps_sent_30d": ("tone of price-sensitive announcements (30 days)", "sent"),
    "ps_pos_30d": ("positive price-sensitive announcements (30 days)", "count"),
    "ps_neg_30d": ("negative price-sensitive announcements (30 days)", "count"),
    "ev_earnings_30d": ("recent results announcement", "count"), "ev_guidance_30d": ("recent guidance update", "count"),
    "ev_raising_30d": ("recent capital raising", "count"), "ev_takeover_30d": ("recent takeover/M&A news", "count"),
    "ev_halt_30d": ("recent trading halt", "count"), "ev_dividend_30d": ("recent dividend announcement", "count"),
    "days_since_results": ("days since last results", "days"),
    "xjo_mom_21": ("ASX 200 1-month return", "spct"), "xjo_mom_63": ("ASX 200 3-month return", "spct"),
    "xjo_vol_60": ("market volatility", "pct"), "sector_mom_63": ("sector 3-month return", "spct"),
    "sector_rel_63": ("3-month return vs its sector", "spct"), "audusd_chg_63": ("AUD/USD 3-month change", "spct"),
    "cash_rate": ("RBA cash rate", "pp"), "cash_rate_chg_6m": ("change in cash rate (6 months)", "pp"),
    "iron_chg_63": ("iron ore 3-month change", "spct"), "gold_chg_63": ("gold 3-month change", "spct"),
    "oil_chg_63": ("oil 3-month change", "spct"), "mom_12_1_rank": ("momentum rank in the index", "rank"),
    "mom_1m_rank": ("1-month return rank in the index", "rank"), "vol_60d_rank": ("volatility rank in the index", "rank"),
    "sector_id": ("its sector", "sector"),
}
MISSING_TEXT = {"days_since_exdiv": "no dividend on record", "days_since_results": "no results announcement on record",
                "short_pct": "not reported by ASIC", "short_chg20": "not reported by ASIC",
                "ps_sent_7d": "no price-sensitive announcements", "ps_sent_30d": "no price-sensitive announcements"}
NEWS_TERMS = {"ps_7d", "ps_30d", "ps_sent_7d", "ps_sent_30d", "ps_pos_30d", "ps_neg_30d", "ann_30d", "ann_activity",
              "ev_earnings_30d", "ev_guidance_30d", "ev_raising_30d", "ev_takeover_30d", "ev_halt_30d",
              "ev_dividend_30d", "days_since_results"}


def fmt_value(feature: str, v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return MISSING_TEXT.get(feature, "not available")
    kind = FEATURE_TEXT.get(feature, (feature, "num"))[1]
    if kind == "spct":
        return f"{v * 100:+.1f}%"
    if kind == "pct":
        return f"{v * 100:.1f}%"
    if kind == "pp":
        return f"{v:.2f}%" if feature != "short_chg20" and feature != "cash_rate_chg_6m" else f"{v:+.2f} pp"
    if kind == "days":
        return f"{v:.0f} days"
    if kind == "rank":
        return f"{v * 100:.0f}th percentile"
    if kind == "count":
        return f"{v:.0f}"
    if kind == "sent":
        return f"{v:+.2f}"
    if kind == "aud":
        return f"A${v:,.0f}"
    if kind == "sector":
        return "sector"
    return f"{v:.2f}"


def explain(contribs: np.ndarray, feats: list[str], values: dict, k: int = 3) -> dict:
    """contribs: SHAP values (log-odds) per feature for one row, from pred_contrib (bias excluded)."""
    order = np.argsort(contribs)
    def item(i):
        f = feats[i]
        name = FEATURE_TEXT.get(f, (f, "num"))[0]
        return {"feature": f, "label": name, "value": fmt_value(f, values.get(f)), "impact": float(contribs[i]),
                "news": f in NEWS_TERMS}
    up = [item(i) for i in order[::-1][:k] if contribs[i] > 0]
    down = [item(i) for i in order[:k] if contribs[i] < 0]
    return {"for": up, "against": down}


def cost_hurdle(capital: float, top_n: int, brokerage: float, slippage_bps: float) -> float:
    position = capital / top_n
    return 2 * brokerage / position + 2 * slippage_bps / 10_000


def assign(rows: list[dict], top_n: int, hurdle: float, model_auc: float | None) -> None:
    """rows: dicts with code, eligible, p_beat (calibrated), ex50 and optionally score (the raw
    model probability, used for ranking: calibration is monotone but has flat steps, which would
    turn into ties). Adds rank, signal, confidence in place."""
    elig = sorted([r for r in rows if r["eligible"] and r.get("p_beat") is not None],
                  key=lambda r: (-(r["score"] if r.get("score") is not None else r["p_beat"]), r["code"]))
    n = len(elig)
    for i, r in enumerate(elig, start=1):
        r["rank"] = i
    skilled = model_auc is not None and model_auc >= AUC_SKILL
    for r in rows:
        r.setdefault("rank", None)
        p, ex = r.get("p_beat"), r.get("ex50")
        sig = "Hold"
        if r["eligible"] and p is not None and ex is not None:
            if r["rank"] <= top_n and p > 0.5 and ex > hurdle:
                sig = "Buy"
            elif r["rank"] > n - top_n and p < 0.5 and ex < -hurdle:
                sig = "Sell"
        r["signal"] = sig
        if not skilled or p is None:
            r["confidence"] = "low"
        else:
            edge = abs(p - 0.5)
            r["confidence"] = "high" if edge >= 0.10 else "medium" if edge >= 0.05 else "low"

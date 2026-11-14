"""Model jobs.

train (weekly, Sat 02:00; also once on first start): features -> purged walk-forward for
  every horizon and both variants -> forecast-quality evaluation -> portfolio backtests ->
  final models on all known labels -> today's forecasts. ~30-40 min on 2 cores, nice 10.
forecast (end of daily_update): today's features -> saved models -> forecasts + signals.
"""
from __future__ import annotations

import json
import os
import threading
import time

import numpy as np
import pandas as pd

from app.config import get_settings
from app.db import get_conn, upsert, utcnow
from app.jobs import JobContext
from app.models import backtest as B
from app.models import calibrate as C
from app.models import features as F
from app.models import signals as S
from app.models import train as T

_busy = threading.Lock()
LARGE_N = 100


def models_root():
    return get_settings().models_dir / "lgbm"


def _universe(c) -> list[str]:
    u = get_settings().universe
    return [r[0] for r in c.execute(
        "SELECT code FROM index_membership WHERE index_code=? AND as_of=(SELECT MAX(as_of) FROM index_membership "
        "WHERE index_code=?) ORDER BY code", (u, u))]


def _low_priority(fn, *args):
    """Run fn in a fresh thread at nice 10 (Linux nice is per thread; LightGBM's OpenMP threads inherit it)."""
    out: dict = {}

    def work():
        try:
            os.nice(10)
        except OSError:
            pass
        try:
            out["v"] = fn(*args)
        except BaseException as e:  # noqa: BLE001
            out["e"] = e

    t = threading.Thread(target=work, name="model-train", daemon=True)
    t.start()
    t.join()
    if "e" in out:
        raise out["e"]
    return out.get("v")


def step_train(ctx: JobContext) -> None:
    if not _busy.acquire(blocking=False):
        ctx.notes.append("training already running")
        return
    try:
        _low_priority(_train, ctx)
    finally:
        _busy.release()


def _train(ctx: JobContext) -> None:
    s = get_settings()
    t0 = time.time()
    cfg = B.Config(capital=s.paper_capital, top_n=s.backtest_top_n, brokerage=s.brokerage_per_trade,
                   slippage_bps=s.slippage_bps)
    with get_conn() as c:      # own short write transaction (progress() writes from another connection)
        run_id = c.execute("INSERT INTO backtest_runs(started_at, status, config_json) VALUES(?, 'running', ?)",
                           (utcnow(), json.dumps({**cfg.__dict__, "test_start": s.backtest_start,
                                                  "universe": s.universe}))).lastrowid
    try:
        _train_run(ctx, s, cfg, run_id, t0)
    except BaseException as e:
        with get_conn() as c:
            c.execute("UPDATE backtest_runs SET finished_at=?, status='error', message=? WHERE id=?",
                      (utcnow(), str(e)[:500], run_id))
        raise


def _train_run(ctx: JobContext, s, cfg, run_id: int, t0: float) -> None:
    with get_conn() as c:
        codes = _universe(c)
        ctx.progress(f"building features for {len(codes)} companies")
        df = F.build(c, codes)
        cal = F.market_calendar(c)
    if df.empty:
        raise RuntimeError("no feature rows (no price history?)")
    pos = {d: i for i, d in enumerate(cal)}
    last = df["date"].max()
    rebal = T.rebalance_dates(df, s.backtest_start)
    quarterly = rebal[::3]
    summary: dict = {"quality": {}, "strategies": {}, "notes": []}
    preds: dict[tuple[int, str], pd.DataFrame] = {}
    for h in F.HORIZONS:
        schedule = rebal if h == 21 else quarterly
        for variant in T.VARIANTS:
            p = T.walk_forward(df, pos, h, variant, schedule, progress=ctx.progress)
            p = C.walk_forward(p, df, pos, h, schedule)
            preds[(h, variant)] = p
            summary["quality"][f"{h}_{variant}"] = T.evaluate(p, df, h) if not p.empty else {}
    # --- portfolio backtests on the 21-day signals --------------------------------
    ctx.progress("portfolio backtests")
    with get_conn() as c:
        sym = dict(c.execute(f"SELECT code, COALESCE(yahoo_symbol, code || '.AX') FROM companies "
                             f"WHERE code IN ({','.join('?' * len(codes))})", codes).fetchall())
        px = B.load_prices(c, {**sym, "__STW": F.BENCH_TR, "__XJO": F.INDEX}, rebal[0])
        rf = pd.read_sql_query("SELECT date, value FROM macro_series WHERE series='cash_rate' ORDER BY date", c)
    dates = [d for d in cal if d >= rebal[0]]
    rf_s = rf.set_index("date")["value"].reindex(sorted(set(rf["date"]) | set(dates))).ffill().reindex(dates)
    base = df[["code", "date", "eligible", "mom_12_1", "base_pbeat_21", "turnover_20d"]].copy()
    # "Large at the time": top LARGE_N by trading value on each date. Survivorship bias is
    # worst among stocks that were small then (today's big winners), so an edge that only
    # exists outside this set is suspect.
    big = base[base["eligible"]].groupby("date")["turnover_20d"].rank(ascending=False, method="first") <= LARGE_N
    base_large = base.assign(eligible=base["eligible"] & big.reindex(base.index, fill_value=False))
    strategies = {}
    for label, variant in (("Model (with news)", "news"), ("Model (without news)", "no_news")):
        p = preds[(21, variant)]
        if not p.empty:
            strategies[label] = B.top_n_picks(p.merge(base, on=["code", "date"]), "pbeat", rebal, cfg.top_n)
    strategies["Momentum (baseline B)"] = B.top_n_picks(base, "mom_12_1", rebal, cfg.top_n)
    strategies["Historical odds (baseline A)"] = B.top_n_picks(base, "base_pbeat_21", rebal, cfg.top_n)
    exec_start = dates[1] if len(dates) > 1 else dates[0]
    stw = B.buy_and_hold("ASX 200 buy-and-hold (STW, incl. dividends)", px["adj"]["__STW"], px["open"]["__STW"],
                         px["close"]["__STW"], dates, cfg, exec_start)
    bench = stw.equity
    results = [stw] + [B.run(name, picks, px, dates, cfg) for name, picks in strategies.items()]
    xjo = px["close"]["__XJO"].reindex(dates).ffill()
    xjo_eq = xjo / xjo.iloc[0] * cfg.capital
    # Survivorship control: random top-N from the same eligible list, same costs. If the model
    # can't beat this, its "outperformance" comes from picking among today's survivors.
    ctx.progress("random-pick control (20 runs)")
    rand = [B.run(f"random {k}", B.random_picks(base, rebal, cfg.top_n, seed=k), px, dates, cfg) for k in range(20)]
    rand_m = sorted(((B.metrics(r, bench, rf_s, rebal), r) for r in rand), key=lambda x: x[0]["cagr"])
    cagrs = [m["cagr"] for m, _ in rand_m]
    median_run = rand_m[len(rand_m) // 2][1]
    median_run.name = f"Random {cfg.top_n} from the same list (median of 20)"
    results.append(median_run)
    summary["random_control"] = {"runs": len(cagrs), "cagr_p10": float(np.percentile(cagrs, 10)),
                                 "cagr_p50": float(np.percentile(cagrs, 50)),
                                 "cagr_p90": float(np.percentile(cagrs, 90)),
                                 "cagr_min": min(cagrs), "cagr_max": max(cagrs)}
    for r in results:
        summary["strategies"][r.name] = B.metrics(r, bench if r is not stw else None, rf_s, rebal)
    # --- sensitivity: the same strategies restricted to stocks that were large at the time
    ctx.progress("large-at-the-time sensitivity")
    sens = {}
    p_prod = preds[(21, "news")]
    if not p_prod.empty:
        sens[f"Model (with news), top {LARGE_N} only"] = B.run(
            "m", B.top_n_picks(p_prod.merge(base_large, on=["code", "date"]), "pbeat", rebal, cfg.top_n), px, dates, cfg)
    sens[f"Momentum, top {LARGE_N} only"] = B.run("mo", B.top_n_picks(base_large, "mom_12_1", rebal, cfg.top_n), px, dates, cfg)
    rl = sorted(((B.metrics(r, bench, rf_s, rebal), r) for r in
                 (B.run(f"rl{k}", B.random_picks(base_large, rebal, cfg.top_n, seed=100 + k), px, dates, cfg)
                  for k in range(20))), key=lambda x: x[0]["cagr"])
    sens[f"Random {cfg.top_n} from the top {LARGE_N} (median of 20)"] = rl[len(rl) // 2][1]
    lc = [m["cagr"] for m, _ in rl]
    summary["large_only"] = {
        "n": LARGE_N,
        "strategies": {k: B.metrics(r, bench, rf_s, rebal) for k, r in sens.items()},
        "random_control": {"runs": len(lc), "cagr_p10": float(np.percentile(lc, 10)),
                           "cagr_p50": float(np.percentile(lc, 50)), "cagr_p90": float(np.percentile(lc, 90))},
    }
    summary["strategies"]["S&P/ASX 200 price index (no dividends, no costs)"] = B.metrics(
        B.Result("xjo", xjo_eq), None, rf_s, rebal)
    # weekly-sampled curves for the chart
    curves = {r.name: [[d, round(v, 2)] for d, v in r.equity.iloc[::5].items()] for r in results}
    curves["S&P/ASX 200 price index (no dividends, no costs)"] = [[d, round(v, 2)] for d, v in xjo_eq.iloc[::5].items()]
    # --- locked holdout: strategy results from HOLDOUT_START only (same runs, equity sliced)
    hd = [d for d in dates if d >= T.HOLDOUT_START]
    if len(hd) > 20:
        hrebal = [d for d in rebal if d >= hd[0]]
        summary["holdout_strategies"] = {
            r.name: B.metrics(B.Result(r.name, r.equity.loc[hd[0]:]), bench if r is not stw else None, rf_s, hrebal)
            for r in results}
    # --- production variant: chosen on DEVELOPMENT results only (before the holdout); the
    # skill verdict uses the holdout, where the choice can't have been fitted to luck.
    q = summary["quality"]

    def _auc(key, period):
        return ((q.get(key) or {}).get(period) or {}).get("auc_beat")
    dev_news, dev_plain = _auc("21_news", "dev"), _auc("21_no_news", "dev")
    if dev_news is None and dev_plain is None:      # no development period (short history)
        dev_news = (q.get("21_news", {}).get("model") or {}).get("auc_beat")
        dev_plain = (q.get("21_no_news", {}).get("model") or {}).get("auc_beat")
    variant = "news" if (dev_news or 0) >= (dev_plain or 0) else "no_news"
    summary["production_variant"] = variant
    summary["model_auc_beat"] = _auc(f"21_{variant}", "holdout")     # None until the holdout has outcomes
    summary["model_auc_beat_dev"] = _auc(f"21_{variant}", "dev")
    summary["holdout_start"] = T.HOLDOUT_START
    summary["fingerprint"] = T.fingerprint()
    summary["survivorship"] = (f"Backtests use today's {len(codes)} {s.universe.upper()} members for the whole period. "
                               "Companies that were delisted or dropped out are missing, which flatters results.")
    summary["last_feature_date"] = last
    summary["rebalances"] = len(rebal)
    summary["duration_min"] = round((time.time() - t0) / 60, 1)
    # --- final models + today's forecasts
    ctx.progress(f"fitting final models ({variant})")
    models = T.fit_final(df, pos, last, variant)
    calibrators = {str(h): C.fit_final(preds[(h, variant)], df, pos, h, last) for h in F.HORIZONS}
    meta = {"trained_at": utcnow(), "variant": variant, "features": T.VARIANTS[variant], "last_feature_date": last,
            "train_rows": int(df["eligible"].sum()), "auc_beat": summary["model_auc_beat"],
            "auc_beat_dev": summary["model_auc_beat_dev"], "holdout_start": T.HOLDOUT_START,
            "fingerprint": summary["fingerprint"], "calibrators": calibrators, "backtest_run": run_id}
    T.save_models(models_root(), models, meta)
    with get_conn() as c:
        c.execute("UPDATE backtest_runs SET finished_at=?, status='ok', summary_json=?, curves_json=?, message=? WHERE id=?",
                  (utcnow(), json.dumps(summary, default=_json), json.dumps(curves), f"{summary['duration_min']} min",
                   run_id))
    ctx.items += len(df)
    step_forecast(ctx)


def _json(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, (pd.Timestamp,)):
        return o.isoformat()
    return str(o)


def step_forecast(ctx: JobContext) -> int:
    loaded = T.load_models(models_root())
    if loaded is None:
        ctx.notes.append("forecast skipped: no trained models yet (the train job builds them)")
        return 0
    models, meta = loaded
    s = get_settings()
    feats = meta["features"]
    with get_conn() as c:
        codes = _universe(c)
        last = F.market_calendar(c)[-1]
        df = F.build(c, codes, dates=[last])
    if df.empty:
        return 0
    X = df[feats].to_numpy(dtype=np.float32)
    out = df[["code", "date", "eligible"]].copy()
    for key, booster in models.items():
        out[key] = booster.predict(X)
    contrib = models["21_pbeat"].predict(X, pred_contrib=True)[:, :-1] if "21_pbeat" in models else None
    # calibrated probabilities and ranges (raw values kept; ranking uses the raw score)
    cals = meta.get("calibrators") or {}
    for h in F.HORIZONS:
        q = np.sort(out[[f"{h}_q10", f"{h}_q50", f"{h}_q90"]].to_numpy(), axis=1)
        out[f"{h}_q10"], out[f"{h}_q50"], out[f"{h}_q90"] = q[:, 0], q[:, 1], q[:, 2]
        p = pd.DataFrame({"pup": out[f"{h}_pup"], "q10": q[:, 0], "q50": q[:, 1], "q90": q[:, 2]})
        if f"{h}_pbeat" in out:
            p["pbeat"] = out[f"{h}_pbeat"]
        p = C.apply(cals.get(str(h)), p)
        for k in ("pup", "q10", "q90", "pbeat"):
            if k in p:
                out[f"{h}_{k}_cal"] = p[f"{k}_cal"].to_numpy()
    rows = []
    hurdle = S.cost_hurdle(s.paper_capital, s.backtest_top_n, s.brokerage_per_trade, s.slippage_bps)
    recs = out.to_dict("records")
    sig_rows = [{"code": r["code"], "eligible": bool(r["eligible"]),
                 "p_beat": float(r["21_pbeat_cal"]) if "21_pbeat" in r else None,
                 "score": float(r["21_pbeat"]) if "21_pbeat" in r else None,
                 "ex50": float(r["21_ex50"]) if "21_ex50" in r else None, "i": i} for i, r in enumerate(recs)]
    S.assign(sig_rows, s.backtest_top_n, hurdle, meta.get("auc_beat"))
    now = utcnow()
    for sr in sig_rows:
        i = sr["i"]
        values = df.iloc[i][feats].to_dict()
        why = S.explain(contrib[i], feats, values) if contrib is not None else {}
        for h in F.HORIZONS:
            r = recs[i]
            row = {"code": sr["code"], "as_of_date": last, "horizon": h, "variant": meta["variant"],
                   "q10": float(r[f"{h}_q10_cal"]), "q50": float(r[f"{h}_q50"]), "q90": float(r[f"{h}_q90_cal"]),
                   "p_up": float(r[f"{h}_pup_cal"]), "q10_raw": float(r[f"{h}_q10"]), "q90_raw": float(r[f"{h}_q90"]),
                   "p_up_raw": float(r[f"{h}_pup"]), "p_beat_raw": None, "calibrated": int(cals.get(str(h)) is not None),
                   "p_beat": None, "ex50": None, "signal": None, "confidence": None, "rank": None,
                   "eligible": int(sr["eligible"]), "explain_json": None, "model_version": meta.get("version"),
                   "computed_at": now}
            if h == 21:
                row.update(p_beat=sr["p_beat"], p_beat_raw=sr["score"], ex50=sr["ex50"], signal=sr["signal"], confidence=sr["confidence"],
                           rank=sr["rank"], explain_json=json.dumps(why))
            rows.append(row)
    with get_conn() as c:
        upsert(c, "forecasts", rows, ["code", "as_of_date", "horizon"])
    ctx.items += len(sig_rows)
    ctx.notes.append(f"forecasts for {len(sig_rows)} companies as of {last} "
                     f"({sum(r['signal'] == 'Buy' for r in sig_rows)} Buy, {sum(r['signal'] == 'Sell' for r in sig_rows)} Sell)")
    return len(sig_rows)

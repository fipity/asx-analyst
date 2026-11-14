"""LightGBM forecasts: purged walk-forward evaluation, final fit, saved models.

Per horizon h (trading days) we fit, on eligible rows only:
  q10 / q50 / q90   quantile regression of the h-day total return
  pup               classifier: P(return > 0)
and for h = 21 additionally
  pbeat             classifier: P(return beats STW, the ASX 200 ETF incl. dividends)
  ex50              median regression of the excess return vs STW
Each in two variants: with and without the announcement/news features.

Walk-forward: expanding window, retrained at each rebalance date R; a row at date d is
only used for training if its label was known at R, i.e. pos(d) + h <= pos(R) on the
trading calendar (purge gap = horizon). Predictions are made for dates in [R, next R).
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from app.models import features as F

log = logging.getLogger(__name__)

# Deliberately conservative and fixed a priori (2026-11-13), NOT tuned on walk-forward
# results: tuning on the test period would leak and flatter the backtest. Stock returns
# are mostly noise, so: small trees, big leaves, strong L2, few rounds.
PARAMS = {
    "learning_rate": 0.03, "num_leaves": 7, "min_data_in_leaf": 1000, "feature_fraction": 0.7,
    "bagging_fraction": 0.7, "bagging_freq": 1, "lambda_l2": 10.0, "num_threads": 2, "verbose": -1,
    "seed": 7, "deterministic": True, "force_row_wise": True,
}
ROUNDS = 150
MIN_TRAIN_ROWS = 5000      # skip a retrain until there is this much labelled history

# Locked holdout (set 2026-11-13). Every design choice - parameters, features, which variant
# is used, calibration method - must be judged on DEVELOPMENT results (before this date) only.
# Holdout results are for the final verdict (the skill gate). Never move this date, and don't
# change the model because of holdout numbers: that turns the holdout into more training data.
# Caveat: whole-period results (incl. 2024-2026) were seen once, on 2026-11-13, before the split
# existed; the model settings were fixed before that, so they were not chosen with it.
HOLDOUT_START = "2024-01-01"


@dataclass(frozen=True)
class Spec:
    name: str          # q10 | q50 | q90 | pup | pbeat | ex50
    objective: str     # quantile | binary
    alpha: float | None
    target: str        # ret | ex


def specs_for(h: int) -> list[Spec]:
    s = [Spec("q10", "quantile", 0.10, "ret"), Spec("q50", "quantile", 0.50, "ret"),
         Spec("q90", "quantile", 0.90, "ret"), Spec("pup", "binary", None, "ret")]
    if h == 21:
        s += [Spec("pbeat", "binary", None, "ex"), Spec("ex50", "quantile", 0.50, "ex")]
    return s


VARIANTS = {"news": F.FEATURES, "no_news": F.FEATURES_NO_NEWS}


def _label(df: pd.DataFrame, spec: Spec, h: int, clip: tuple[float, float] | None = None) -> np.ndarray:
    y = df[f"fwd_{'ret' if spec.target == 'ret' else 'ex'}_{h}"].to_numpy(dtype=float)
    if spec.objective == "binary":
        return (y > 0).astype(float)
    if clip is not None:
        y = np.clip(y, *clip)
    return y


def fit(df: pd.DataFrame, feats: list[str], spec: Spec, h: int) -> lgb.Booster:
    y_raw = df[f"fwd_{'ret' if spec.target == 'ret' else 'ex'}_{h}"].to_numpy(dtype=float)
    clip = (np.nanpercentile(y_raw, 1), np.nanpercentile(y_raw, 99)) if spec.objective == "quantile" else None
    y = _label(df, spec, h, clip)
    params = dict(PARAMS, objective=spec.objective)
    if spec.alpha is not None:
        params["alpha"] = spec.alpha
    ds = lgb.Dataset(df[feats].to_numpy(dtype=np.float32), label=y, feature_name=feats,
                     categorical_feature=[f for f in F.CATEGORICAL if f in feats], free_raw_data=True)
    return lgb.train(params, ds, num_boost_round=ROUNDS)


def trainable(df: pd.DataFrame, h: int) -> pd.Series:
    return df["eligible"] & df[f"fwd_ret_{h}"].notna() & df[f"fwd_ex_{h}"].notna()


def rebalance_dates(df: pd.DataFrame, test_start: str, every: int = 4) -> list[str]:
    """Every `every`-th sample date from test_start (~monthly with weekly samples)."""
    dates = sorted(d for d in df["date"].unique() if d >= test_start)
    return dates[::every]


def walk_forward(df: pd.DataFrame, cal_pos: dict[str, int], h: int, variant: str, retrain: list[str],
                 progress=None) -> pd.DataFrame:
    feats = VARIANTS[variant]
    specs = specs_for(h)
    pos = df["date"].map(cal_pos).to_numpy()
    ok = trainable(df, h).to_numpy()
    out = []
    for i, r in enumerate(retrain):
        nxt = retrain[i + 1] if i + 1 < len(retrain) else "9999"
        train_mask = ok & (pos + h <= cal_pos[r])
        test = df[(df["date"] >= r) & (df["date"] < nxt)]
        if train_mask.sum() < MIN_TRAIN_ROWS or test.empty:
            continue
        tr = df[train_mask]
        X = test[feats].to_numpy(dtype=np.float32)
        pred = test[["code", "date"]].copy()
        for spec in specs:
            pred[spec.name] = fit(tr, feats, spec, h).predict(X)
        pred["train_rows"] = int(train_mask.sum())
        out.append(pred)
        if progress:
            progress(f"walk-forward h={h} {variant}: {i + 1}/{len(retrain)}")
    if not out:
        return pd.DataFrame()
    res = pd.concat(out, ignore_index=True)
    return _fix_crossing(res)


def _fix_crossing(p: pd.DataFrame) -> pd.DataFrame:
    """Quantile models are fit separately; sort so q10 <= q50 <= q90."""
    if {"q10", "q50", "q90"} <= set(p.columns):
        q = np.sort(p[["q10", "q50", "q90"]].to_numpy(), axis=1)
        p["q10"], p["q50"], p["q90"] = q[:, 0], q[:, 1], q[:, 2]
    return p


def fingerprint(extra: dict | None = None) -> str:
    """Short hash of everything that defines the model; a new value = a new model design."""
    from app.models import calibrate
    spec = {"params": PARAMS, "rounds": ROUNDS, "min_train": MIN_TRAIN_ROWS, "features": VARIANTS,
            "horizons": F.HORIZONS, "holdout": HOLDOUT_START, "eligibility": [F.MIN_PRICE, F.MIN_TURNOVER],
            "calibration": calibrate.VERSION, **(extra or {})}
    return hashlib.sha1(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()[:10]


# --- evaluation ------------------------------------------------------------------

def auc(y: np.ndarray, p: np.ndarray) -> float | None:
    """Rank-based ROC AUC (Mann-Whitney), ties averaged."""
    y = np.asarray(y, dtype=bool)
    n1, n0 = y.sum(), (~y).sum()
    if n1 == 0 or n0 == 0:
        return None
    ranks = pd.Series(p).rank(method="average").to_numpy()
    return float((ranks[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def pinball(y: np.ndarray, q: np.ndarray, alpha: float) -> float:
    d = y - q
    return float(np.mean(np.maximum(alpha * d, (alpha - 1) * d)))


def rank_ic(df: pd.DataFrame, score: str, target: str) -> tuple[float | None, float | None]:
    """Mean per-date Spearman correlation and its t-stat."""
    ics = []
    for _, g in df.groupby("date"):
        if len(g) >= 20 and g[score].nunique() > 1:
            ics.append(g[score].rank().corr(g[target].rank()))
    if len(ics) < 5:
        return None, None
    ics = np.array(ics)
    return float(ics.mean()), float(ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics))))


def evaluate(preds: pd.DataFrame, df: pd.DataFrame, h: int) -> dict:
    """Out-of-sample quality on eligible rows with known outcomes, model vs baseline A."""
    m = preds.merge(df[["code", "date", "eligible", f"fwd_ret_{h}", f"fwd_ex_{h}", f"base_q10_{h}",
                        f"base_q50_{h}", f"base_q90_{h}", f"base_pup_{h}", f"base_pbeat_{h}"]],
                    on=["code", "date"], how="left")
    m = m[m["eligible"] & m[f"fwd_ret_{h}"].notna() & m[f"base_q50_{h}"].notna()]
    if m.empty:
        return {}
    y, ex = m[f"fwd_ret_{h}"].to_numpy(), m[f"fwd_ex_{h}"].to_numpy()
    out: dict = {"rows": int(len(m)), "dates": int(m["date"].nunique()),
                 "from": m["date"].min(), "to": m["date"].max(), "base_rate_up": float((y > 0).mean())}
    loss_m = np.mean([pinball(y, m[q].to_numpy(), a) for q, a in (("q10", .1), ("q50", .5), ("q90", .9))])
    loss_b = np.mean([pinball(y, m[f"base_{q}_{h}"].to_numpy(), a) for q, a in (("q10", .1), ("q50", .5), ("q90", .9))])
    out["model"] = {
        "coverage_80": float(((y >= m["q10"]) & (y <= m["q90"])).mean()),
        "pinball": float(loss_m), "auc_up": auc(y > 0, m["pup"].to_numpy()),
        "brier_up": float(np.mean((m["pup"].to_numpy() - (y > 0)) ** 2)),
    }
    out["baseline_a"] = {
        "coverage_80": float(((y >= m[f"base_q10_{h}"]) & (y <= m[f"base_q90_{h}"])).mean()),
        "pinball": float(loss_b), "auc_up": auc(y > 0, m[f"base_pup_{h}"].to_numpy()),
        "brier_up": float(np.mean((m[f"base_pup_{h}"].to_numpy() - (y > 0)) ** 2)),
    }
    out["pinball_skill_vs_baseline"] = float(1 - loss_m / loss_b) if loss_b else None
    out["model"]["ic_q50"], out["model"]["ic_q50_t"] = rank_ic(m.assign(t=y), "q50", "t")
    if "pbeat" in m:
        out["model"]["auc_beat"] = auc(ex > 0, m["pbeat"].to_numpy())
        out["model"]["brier_beat"] = float(np.mean((m["pbeat"].to_numpy() - (ex > 0)) ** 2))
        out["model"]["ic_beat"], out["model"]["ic_beat_t"] = rank_ic(m.assign(t=ex), "pbeat", "t")
        out["baseline_a"]["auc_beat"] = auc(ex > 0, m[f"base_pbeat_{h}"].to_numpy())
        out["base_rate_beat"] = float((ex > 0).mean())
        out["calibration"] = calibration(m["pbeat"].to_numpy(), ex > 0)
    out["dev"] = period_stats(m[m["date"] < HOLDOUT_START], h)
    out["holdout"] = period_stats(m[m["date"] >= HOLDOUT_START], h)
    return out


def _brier(p, y) -> float:
    return float(np.mean((np.asarray(p, dtype=float) - y) ** 2))


def period_stats(m: pd.DataFrame, h: int) -> dict:
    """Ranking skill on all rows; raw vs calibrated probabilities and ranges on rows that had a
    calibrator (cal_ok), compared on the same rows."""
    if m.empty:
        return {}
    y, ex = m[f"fwd_ret_{h}"].to_numpy(), m[f"fwd_ex_{h}"].to_numpy()
    st: dict = {"rows": int(len(m)), "from": m["date"].min(), "to": m["date"].max(),
                "auc_up": auc(y > 0, m["pup"].to_numpy())}
    if "pbeat" in m:
        st["auc_beat"] = auc(ex > 0, m["pbeat"].to_numpy())
        st["ic_beat"], st["ic_beat_t"] = rank_ic(m.assign(t=ex), "pbeat", "t")
        st["base_rate_beat"] = float((ex > 0).mean())
    if "cal_ok" not in m:
        return st
    c = m[m["cal_ok"].astype(bool)]
    st["cal_rows"] = int(len(c))
    if c.empty:
        return st
    yc, exc = c[f"fwd_ret_{h}"].to_numpy(), c[f"fwd_ex_{h}"].to_numpy()
    st["coverage_raw"] = float(((yc >= c["q10"]) & (yc <= c["q90"])).mean())
    st["coverage_cal"] = float(((yc >= c["q10_cal"]) & (yc <= c["q90_cal"])).mean())
    st["below_cal"] = float((yc < c["q10_cal"]).mean())
    st["above_cal"] = float((yc > c["q90_cal"]).mean())
    st["width_raw"] = float((c["q90"] - c["q10"]).median())
    st["width_cal"] = float((c["q90_cal"] - c["q10_cal"]).median())
    up = (yc > 0).astype(float)
    st["brier_up_raw"], st["brier_up_cal"] = _brier(c["pup"], up), _brier(c["pup_cal"], up)
    st["brier_up_base"] = _brier(np.full(len(up), up.mean()), up)
    if "pbeat_cal" in c:
        b = (exc > 0).astype(float)
        st["brier_beat_raw"], st["brier_beat_cal"] = _brier(c["pbeat"], b), _brier(c["pbeat_cal"], b)
        st["brier_beat_base"] = _brier(np.full(len(b), b.mean()), b)
        st["calibration_raw"] = calibration(c["pbeat"].to_numpy(), exc > 0)
        st["calibration_cal"] = calibration(c["pbeat_cal"].to_numpy(), exc > 0, bins=5)
    return st


def calibration(p: np.ndarray, y: np.ndarray, bins: int = 10) -> list[dict]:
    """Predicted vs realised frequency in probability deciles."""
    edges = np.unique(np.quantile(p, np.linspace(0, 1, bins + 1)))
    idx = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, len(edges) - 2)
    out = []
    for b in range(len(edges) - 1):
        sel = idx == b
        if sel.sum():
            out.append({"lo": float(edges[b]), "hi": float(edges[b + 1]), "n": int(sel.sum()),
                        "predicted": float(p[sel].mean()), "realised": float(np.mean(y[sel]))})
    return out


# --- final models ------------------------------------------------------------------

def fit_final(df: pd.DataFrame, cal_pos: dict[str, int], last_date: str, variant: str,
              horizons=F.HORIZONS) -> dict[str, lgb.Booster]:
    feats = VARIANTS[variant]
    pos = df["date"].map(cal_pos).to_numpy()
    models = {}
    for h in horizons:
        mask = trainable(df, h).to_numpy() & (pos + h <= cal_pos[last_date])
        tr = df[mask]
        for spec in specs_for(h):
            models[f"{h}_{spec.name}"] = fit(tr, feats, spec, h)
    return models


def save_models(root: Path, models: dict[str, lgb.Booster], meta: dict) -> Path:
    """Write to a new version dir, then swap it in as `current` (previous kept as `previous`)."""
    root.mkdir(parents=True, exist_ok=True)
    version = time.strftime("%Y%m%d-%H%M%S")
    new = root / version
    new.mkdir()
    for k, b in models.items():
        b.save_model(str(new / f"{k}.txt"))
    (new / "meta.json").write_text(json.dumps({**meta, "version": version}, indent=1, default=str))
    cur, prev = root / "current", root / "previous"
    if prev.exists():
        shutil.rmtree(prev.resolve() if prev.is_symlink() else prev, ignore_errors=True)
        if prev.is_symlink():
            prev.unlink()
    if cur.exists():
        cur.rename(prev)
    new.rename(cur)
    for d in root.iterdir():          # keep only current + previous
        if d.is_dir() and d.name not in ("current", "previous"):
            shutil.rmtree(d, ignore_errors=True)
    return cur


def load_models(root: Path) -> tuple[dict[str, lgb.Booster], dict] | None:
    cur = root / "current"
    if not (cur / "meta.json").exists():
        return None
    meta = json.loads((cur / "meta.json").read_text())
    models = {p.stem: lgb.Booster(model_file=str(p)) for p in cur.glob("*.txt")}
    return models, meta

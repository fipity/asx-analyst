"""Make the model's numbers honest: calibrated probabilities and 80% ranges that really hold 80%.

Probabilities (P(up), P(beat the ASX 200)): isotonic regression of the outcome on the raw
model probability, fitted on earlier out-of-sample predictions. A calibrated 55% means that
in the past, predictions like this one came true about 55% of the time. When the model has no
skill, the map flattens and every stock shows about the base rate, which is the truthful answer.
The map is monotone, so the order of stocks is unchanged (signals rank on the raw score).

Ranges (10-90%): split-conformal adjustment of the quantile models (conformalised quantile
regression, scaled by each forecast's own width): the lower and upper edges are each moved
so that, on earlier predictions, about 10% of outcomes fell below and 10% above.

Both are refitted walk-forward for the evaluation (at each retrain date, only on predictions
whose outcome was already known then), and once on everything known for today's forecasts.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MIN_ROWS = 2000      # labelled predictions needed before a calibrator is fitted
PROB_BINS = 20       # quantile bins of the raw probability (smooths the isotonic fit)
TAIL = 0.10          # target share of outcomes below the range, and above it
MIN_WIDTH = 1e-4
VERSION = 1          # bump when the method changes (part of the model fingerprint)


def _pav(y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Pool-adjacent-violators: the weighted least-squares non-decreasing fit to y."""
    vals, wts, sizes = [], [], []
    for yi, wi in zip(y, w):
        vals.append(float(yi))
        wts.append(float(wi))
        sizes.append(1)
        while len(vals) > 1 and vals[-2] > vals[-1]:
            tw = wts[-2] + wts[-1]
            v = (vals[-2] * wts[-2] + vals[-1] * wts[-1]) / tw
            n = sizes[-2] + sizes[-1]
            vals[-2:], wts[-2:], sizes[-2:] = [v], [tw], [n]
    return np.repeat(vals, sizes)


def fit_prob(p: np.ndarray, y: np.ndarray, bins: int = PROB_BINS) -> list[list[float]]:
    """Returns knots [xs, ys] for np.interp: xs = mean raw probability per bin, ys = isotonic hit rate."""
    p, y = np.asarray(p, dtype=float), np.asarray(y, dtype=float)
    ok = np.isfinite(p) & np.isfinite(y)
    p, y = p[ok], y[ok]
    edges = np.unique(np.quantile(p, np.linspace(0, 1, bins + 1)))
    if len(edges) < 2:
        return [[float(np.mean(p))], [float(np.mean(y))]]
    idx = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, len(edges) - 2)
    xs, ym, n = [], [], []
    for b in range(len(edges) - 1):
        sel = idx == b
        if sel.any():
            xs.append(p[sel].mean())
            ym.append(y[sel].mean())
            n.append(sel.sum())
    return [[float(x) for x in xs], [float(v) for v in _pav(np.array(ym), np.array(n))]]


def apply_prob(p, knots) -> np.ndarray:
    xs, ys = knots
    return np.interp(np.asarray(p, dtype=float), xs, ys)


def fit_range(q10, q90, y) -> list[float]:
    """Scaled conformal offsets [a_lo, a_hi] (in units of each forecast's q90 - q10 width)."""
    q10, q90, y = (np.asarray(a, dtype=float) for a in (q10, q90, y))
    ok = np.isfinite(q10) & np.isfinite(q90) & np.isfinite(y)
    q10, q90, y = q10[ok], q90[ok], y[ok]
    w = np.maximum(q90 - q10, MIN_WIDTH)
    n = len(y)
    level = min(1.0, np.ceil((n + 1) * (1 - TAIL)) / n)        # finite-sample correction
    a_lo = np.quantile((q10 - y) / w, level, method="higher")
    a_hi = np.quantile((y - q90) / w, level, method="higher")
    return [float(a_lo), float(a_hi)]


def apply_range(q10, q50, q90, offsets) -> tuple[np.ndarray, np.ndarray]:
    q10, q50, q90 = (np.asarray(a, dtype=float) for a in (q10, q50, q90))
    w = np.maximum(q90 - q10, MIN_WIDTH)
    lo = np.minimum(q10 - offsets[0] * w, q50)
    hi = np.maximum(q90 + offsets[1] * w, q50)
    return lo, hi


def fit(m: pd.DataFrame, h: int) -> dict:
    """Calibrators from labelled predictions m (columns pup, q10, q90, [pbeat], fwd_ret_h, fwd_ex_h)."""
    y = m[f"fwd_ret_{h}"].to_numpy(dtype=float)
    out = {"n": int(len(m)), "pup": fit_prob(m["pup"], y > 0), "range": fit_range(m["q10"], m["q90"], y)}
    if "pbeat" in m:
        out["pbeat"] = fit_prob(m["pbeat"], m[f"fwd_ex_{h}"].to_numpy(dtype=float) > 0)
    return out


def apply(cal: dict | None, pred: pd.DataFrame) -> pd.DataFrame:
    """Adds pup_cal, pbeat_cal, q10_cal, q90_cal (raw values copied when cal is None)."""
    out = pred.copy()
    if cal is None:
        for k in ("pup", "pbeat", "q10", "q90"):
            if k in out:
                out[f"{k}_cal"] = out[k]
        return out
    out["pup_cal"] = apply_prob(out["pup"], cal["pup"])
    if "pbeat" in out and "pbeat" in cal:
        out["pbeat_cal"] = apply_prob(out["pbeat"], cal["pbeat"])
    out["q10_cal"], out["q90_cal"] = apply_range(out["q10"], out["q50"], out["q90"], cal["range"])
    return out


def _labelled(preds: pd.DataFrame, df: pd.DataFrame, h: int) -> pd.DataFrame:
    cols = ["code", "date", "eligible", f"fwd_ret_{h}", f"fwd_ex_{h}"]
    return preds.merge(df[cols], on=["code", "date"], how="left")


def walk_forward(preds: pd.DataFrame, df: pd.DataFrame, cal_pos: dict[str, int], h: int,
                 retrain: list[str]) -> pd.DataFrame:
    """Calibrated columns for walk-forward predictions, each block calibrated only on earlier
    predictions whose outcome was known at that block's retrain date. cal_ok marks rows that had
    a calibrator (early blocks without enough history keep raw values and cal_ok=False)."""
    if preds.empty:
        return preds
    m = _labelled(preds, df, h)
    pos = m["date"].map(cal_pos).to_numpy()
    known_label = (m["eligible"].fillna(False).astype(bool) & m[f"fwd_ret_{h}"].notna()
                   & m[f"fwd_ex_{h}"].notna()).to_numpy()
    blocks = []
    starts = sorted(set(retrain))
    for i, r in enumerate(starts):
        nxt = starts[i + 1] if i + 1 < len(starts) else "9999"
        blk = (m["date"] >= r) & (m["date"] < nxt)
        if not blk.any():
            continue
        train = known_label & (pos + h <= cal_pos[r]) & (m["date"] < r).to_numpy()
        cal = fit(m[train], h) if train.sum() >= MIN_ROWS else None
        b = apply(cal, preds[blk.to_numpy()])
        b["cal_ok"] = cal is not None
        blocks.append(b)
    first = starts[0] if starts else "9999"
    before = preds[preds["date"] < first]
    if not before.empty:
        b = apply(None, before)
        b["cal_ok"] = False
        blocks.insert(0, b)
    return pd.concat(blocks, ignore_index=True) if blocks else apply(None, preds).assign(cal_ok=False)


def fit_final(preds: pd.DataFrame, df: pd.DataFrame, cal_pos: dict[str, int], h: int, last: str) -> dict | None:
    """Calibrators for today's forecasts: all walk-forward predictions whose outcome is known."""
    if preds.empty:
        return None
    m = _labelled(preds, df, h)
    pos = m["date"].map(cal_pos).to_numpy()
    ok = (m["eligible"].fillna(False).astype(bool) & m[f"fwd_ret_{h}"].notna() & m[f"fwd_ex_{h}"].notna()).to_numpy()
    ok = ok & (pos + h <= cal_pos[last])
    return fit(m[ok], h) if ok.sum() >= MIN_ROWS else None

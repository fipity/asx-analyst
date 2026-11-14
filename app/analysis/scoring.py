"""Fundamental scores: percentile ranks within the GICS sector.

Four categories (valuation, quality, growth, dividend), each the mean of its
available metric percentiles (0-100, higher = better), plus a composite = mean
of the categories. Ranking within small sectors is noisy, so each sector
percentile is shrunk towards the whole-universe percentile:

    pct = (n_sector * pct_sector + K * pct_universe) / (n_sector + K),  K = 5

where n_sector counts companies in the sector that have that metric.

Point-in-time: `gather()` only reads fundamentals snapshots with as_of_date <= as_of,
annual financials with available_at <= as_of and dividends with ex_date <= as_of.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

SHRINK_K = 5
MIN_CATEGORIES_FOR_COMPOSITE = 3

# metric -> (category, label, format, higher_is_better, help)
METRICS: dict[str, tuple[str, str, str, bool, str]] = {
    "earnings_yield": ("valuation", "Earnings yield", "pct", True, "1 / P/E. Loss-makers rank lowest."),
    "fwd_earnings_yield": ("valuation", "Forward earnings yield", "pct", True, "1 / forward P/E (analyst estimates)"),
    "book_yield": ("valuation", "Book yield", "pct", True, "1 / P/B"),
    "fcf_yield": ("valuation", "Free cash flow yield", "pct", True, "Free cash flow / market cap"),
    "roe": ("quality", "Return on equity", "pct", True, ""),
    "profit_margin": ("quality", "Net margin", "pct", True, ""),
    "debt_to_equity": ("quality", "Debt / equity", "ratio", False, "Lower is better. Not reported for most banks."),
    "profitable_years": ("quality", "Profitable years", "pct", True, "Share of reported financial years with a net profit"),
    "revenue_growth": ("growth", "Revenue growth (latest)", "pct", True, "Year on year, latest report (Yahoo)"),
    "earnings_growth": ("growth", "Earnings growth (latest)", "pct", True, "Year on year, latest report (Yahoo)"),
    "rev_cagr": ("growth", "Revenue growth (multi-year)", "pct", True, "Annual growth rate over up to 3 financial years"),
    "eps_cagr": ("growth", "EPS growth (multi-year)", "pct", True, "Annual diluted-EPS growth over up to 3 years (positive EPS only)"),
    "gross_yield": ("dividend", "Gross yield", "pct", True, "Cash yield plus franking credits at 30% tax"),
    "franking_pct": ("dividend", "Franking", "pct100", True, "Franking % of the latest dividend"),
    "div_growth": ("dividend", "Dividend growth", "pct", True, "Last 12 months of dividends vs the 12 months before"),
    "years_paid": ("dividend", "Years with a dividend", "int", True, "Out of the last 5 years"),
}
CATEGORIES = ["valuation", "quality", "growth", "dividend"]
CATEGORY_LABELS = {"valuation": "Valuation", "quality": "Quality", "growth": "Growth", "dividend": "Dividend",
                   "composite": "Overall"}


def _cagr(first: float | None, last: float | None, years: float) -> float | None:
    if first is None or last is None or first <= 0 or last <= 0 or years <= 0:
        return None
    return (last / first) ** (1 / years) - 1


def annual_metrics(fins: list[dict]) -> dict:
    """fins: annual rows (period_end ascending) already filtered to available_at <= as_of."""
    fins = [f for f in fins if f.get("revenue") is not None]
    out = {"rev_cagr": None, "eps_cagr": None, "profitable_years": None}
    if len(fins) >= 3:
        recent = fins[-4:]
        span = len(recent) - 1
        out["rev_cagr"] = _cagr(recent[0]["revenue"], recent[-1]["revenue"], span)
        out["eps_cagr"] = _cagr(recent[0].get("diluted_eps"), recent[-1].get("diluted_eps"), span)
        ni = [f["net_income"] for f in fins if f.get("net_income") is not None]
        if len(ni) >= 3:
            out["profitable_years"] = sum(1 for x in ni if x > 0) / len(ni)
    return out


def dividend_metrics(divs: list[tuple[str, float]], as_of: date) -> dict:
    """divs: (ex_date, amount) with ex_date <= as_of."""
    def window(start_days: int, end_days: int) -> float:
        lo, hi = (as_of - timedelta(days=start_days)).isoformat(), (as_of - timedelta(days=end_days)).isoformat()
        return sum(a or 0 for d, a in divs if lo < d <= hi)

    ttm, prev = window(365, 0), window(730, 365)
    years_paid = sum(1 for k in range(5) if window(365 * (k + 1), 365 * k) > 0)
    return {"div_growth": (ttm / prev - 1) if prev > 0 else None, "years_paid": float(years_paid)}


def metric_values(f: dict, annual: dict, divm: dict) -> dict:
    """Raw metric values from merged fundamentals (market.latest_fundamentals), annual and dividend metrics."""
    pe, fpe, pb = f.get("pe"), f.get("forward_pe"), f.get("pb")
    if pe is not None and pe > 0:
        ey = 1 / pe
    elif f.get("loss_making"):
        ey = -0.01        # below every profitable company; all loss-makers tie
    else:
        ey = None
    return {
        "earnings_yield": ey,
        "fwd_earnings_yield": (1 / fpe) if fpe and fpe > 0 else None,
        "book_yield": (1 / pb) if pb and pb > 0 else None,
        "fcf_yield": f.get("fcf_yield"),
        "roe": f.get("roe"),
        "profit_margin": f.get("profit_margin"),
        "debt_to_equity": f.get("debt_to_equity"),
        "profitable_years": annual.get("profitable_years"),
        "revenue_growth": f.get("revenue_growth"),
        "earnings_growth": f.get("earnings_growth"),
        "rev_cagr": annual.get("rev_cagr"),
        "eps_cagr": annual.get("eps_cagr"),
        "gross_yield": f.get("gross_yield") if f.get("gross_yield") is not None else (0.0 if divm.get("years_paid") == 0 else None),
        "franking_pct": f.get("franking_pct"),
        "div_growth": divm.get("div_growth"),
        "years_paid": divm.get("years_paid"),
    }


def percentiles(values: dict[str, float], higher_is_better: bool) -> dict[str, float]:
    """Mid-rank percentiles (0-100, ties share the average rank). One value -> 50."""
    items = sorted(values.items(), key=lambda kv: kv[1] if higher_is_better else -kv[1])
    n = len(items)
    out: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        avg_rank = (i + j) / 2 + 1                      # 1-based, worst = 1
        for k in range(i, j + 1):
            out[items[k][0]] = (avg_rank - 0.5) / n * 100
        i = j + 1
    return out


def score(companies: dict[str, dict]) -> dict[str, dict]:
    """companies: code -> {"sector": str, "metrics": {metric: value|None}}.
    Returns code -> {valuation, quality, growth, dividend, composite, sector_n, detail, coverage}."""
    pct: dict[str, dict[str, float]] = {code: {} for code in companies}
    for m, (_cat, _label, _fmt, hib, _help) in METRICS.items():
        vals = {c: d["metrics"].get(m) for c, d in companies.items() if d["metrics"].get(m) is not None}
        if not vals:
            continue
        uni = percentiles(vals, hib)
        by_sector: dict[str, dict[str, float]] = {}
        for c, v in vals.items():
            by_sector.setdefault(companies[c]["sector"] or "Unknown", {})[c] = v
        for sec_vals in by_sector.values():
            sec = percentiles(sec_vals, hib)
            n = len(sec_vals)
            for c in sec_vals:
                pct[c][m] = (n * sec[c] + SHRINK_K * uni[c]) / (n + SHRINK_K)
    sector_n: dict[str, int] = {}
    for d in companies.values():
        sector_n[d["sector"] or "Unknown"] = sector_n.get(d["sector"] or "Unknown", 0) + 1
    out = {}
    for c, d in companies.items():
        res: dict = {"sector_n": sector_n[d["sector"] or "Unknown"], "detail": {}, "coverage": {}}
        for cat in CATEGORIES:
            ms = [m for m, info in METRICS.items() if info[0] == cat]
            got = [pct[c][m] for m in ms if m in pct[c]]
            res[cat] = sum(got) / len(got) if got else None
            res["coverage"][cat] = f"{len(got)}/{len(ms)}"
        cats = [res[k] for k in CATEGORIES if res[k] is not None]
        res["composite"] = sum(cats) / len(cats) if len(cats) >= MIN_CATEGORIES_FOR_COMPOSITE else None
        for m in METRICS:
            res["detail"][m] = {"value": d["metrics"].get(m), "pct": pct[c].get(m)}
        out[c] = res
    # composite rank within sector (1 = best)
    by_sector: dict[str, list[tuple[float, str]]] = {}
    for c, r in out.items():
        if r["composite"] is not None:
            by_sector.setdefault(companies[c]["sector"] or "Unknown", []).append((r["composite"], c))
    for lst in by_sector.values():
        lst.sort(reverse=True)
        for i, (_s, c) in enumerate(lst, start=1):
            out[c]["sector_rank"] = i
            out[c]["sector_ranked"] = len(lst)
    return out


def gather(c: sqlite3.Connection, codes: list[str], as_of: date) -> dict[str, dict]:
    """Point-in-time inputs for score()."""
    from app.analysis.market import latest_fundamentals

    fund = latest_fundamentals(c, codes, as_of=as_of.isoformat())
    ph = ",".join("?" * len(codes))
    sectors = dict(c.execute(f"SELECT code, sector FROM companies WHERE code IN ({ph})", codes).fetchall())
    fins: dict[str, list[dict]] = {}
    for r in c.execute(f"SELECT * FROM financials_annual WHERE code IN ({ph}) AND available_at <= ? "
                       f"ORDER BY code, period_end", [*codes, as_of.isoformat()]):
        fins.setdefault(r["code"], []).append(dict(r))
    divs: dict[str, list] = {}
    since = (as_of - timedelta(days=365 * 5 + 5)).isoformat()
    for r in c.execute(f"SELECT code, ex_date, amount FROM dividends WHERE code IN ({ph}) AND ex_date > ? "
                       f"AND ex_date <= ? ORDER BY ex_date", [*codes, since, as_of.isoformat()]):
        divs.setdefault(r[0], []).append((r[1], r[2]))
    out = {}
    for code in codes:
        out[code] = {
            "sector": sectors.get(code),
            "metrics": metric_values(fund.get(code, {}), annual_metrics(fins.get(code, [])),
                                     dividend_metrics(divs.get(code, []), as_of)),
        }
    return out


def score_rows(scores: dict[str, dict], as_of: date, computed_at: str, sectors: dict[str, str | None]) -> list[dict]:
    """Rows for the `scores` table."""
    return [{
        "code": code, "as_of_date": as_of.isoformat(), "sector": sectors.get(code),
        "sector_n": s["sector_n"], "valuation": s["valuation"], "quality": s["quality"], "growth": s["growth"],
        "dividend": s["dividend"], "composite": s["composite"], "sector_rank": s.get("sector_rank"),
        "detail_json": json.dumps({"detail": s["detail"], "coverage": s["coverage"],
                                   "sector_ranked": s.get("sector_ranked")}),
        "computed_at": computed_at,
    } for code, s in scores.items()]

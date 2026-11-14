"""FastAPI app: pages, JSON API, lifespan (DB init + scheduler)."""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pandas as pd
from markupsafe import Markup
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Undefined

from app import auth
from app.analysis import dashboard, indicators, market, markets, scoring
from app.models import signals as model_signals
from app.news import ai as news_ai
from app.news import queries as newsq
from app.portfolio import holdings, paper, record as sigrecord, watchlist
from app.news import sentiment as news_sentiment
from app.news.tagging import EVENT_LABELS, MACRO_LABELS, MACRO_SECTORS
from app.sources.rss import FEEDS
from app.config import MARKET_TZ, get_settings
from app.db import get_conn, init_db
from app import alerts
from app.jobs import JOB_FAILURE_HOOKS, start_job_thread
from app.logging_setup import setup_logging
from app.pipeline.ingest import JOBS
from app.scheduler import JOB_INFO, scheduled_jobs, start_scheduler, stop_scheduler
from app.status import data_state, health, status_snapshot, storage

log = logging.getLogger("app")
BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    setup_logging(s.log_level)
    init_db()
    s.get_secret_key()
    if alerts.on_job_failure not in JOB_FAILURE_HOOKS:
        JOB_FAILURE_HOOKS.append(alerts.on_job_failure)
    if s.scheduler_enabled:
        start_scheduler()
        threading.Thread(target=_warm_caches, name="warm-caches", daemon=True).start()
    log.info("ASX Analyst started: universe=%s, password=%s", s.universe, "on" if s.app_password else "off")
    yield
    stop_scheduler()


def _warm_caches() -> None:
    """Build the screener/freshness caches (and pull the DB into the page cache) so the first page view is fast."""
    try:
        status_snapshot()
        with get_conn() as c:
            market.screener_rows(c)
    except Exception:  # noqa: BLE001 - best effort only
        log.exception("cache warm-up failed")


app = FastAPI(title="ASX Analyst", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
app.add_middleware(auth.PasswordMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=2048)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def static_url(path: str) -> str:
    """/static/<path>?v=<mtime>: browsers fetch the new file after a rebuild instead of a cached old one."""
    try:
        v = int((BASE_DIR / "static" / path).stat().st_mtime)
    except OSError:
        return f"/static/{path}"
    return f"/static/{path}?v={v}"


templates.env.globals["static_url"] = static_url


# --- template helpers -------------------------------------------------------

def _parse_ts(v):
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None  # a plain date
    return dt.astimezone(MARKET_TZ)


_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$")


def fmt_date(v) -> str:
    """Display dates the Australian way: 2026-11-13 -> 13/11/2026, 2026-06 -> 06/2026.
    Timestamps and anything else pass through unchanged; None -> ''. Inputs, CSV and the API keep ISO."""
    if v is None or isinstance(v, Undefined) or v == "":
        return ""
    if isinstance(v, (date, datetime)):
        v = v.isoformat()[:10]
    m = _ISO_DATE.match(str(v))
    if not m:
        return str(v)
    y, mo, d = m.groups()
    return f"{d}/{mo}/{y}" if d else f"{mo}/{y}"


def fmt_time(v) -> str:
    dt = _parse_ts(v)
    if dt is None:
        return fmt_date(v) if v else "—"
    return dt.strftime("%d/%m/%Y %H:%M")


def fmt_age(v) -> str:
    dt = _parse_ts(v)
    if dt is None:
        return ""
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{secs / 60:.0f} min ago"
    if secs < 172800:
        return f"{secs / 3600:.0f} h ago"
    return f"{secs / 86400:.0f} days ago"


def fmt_num(v, digits: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{digits}f}"
    except (TypeError, ValueError):
        return str(v)


def fmt_money(v) -> str:
    if v is None or isinstance(v, Undefined):
        return "—"
    v = float(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v / div:,.1f}{suf}"
    return f"{v:,.0f}"


def fmt_cell(v, fmt: str) -> str:
    """Format a value according to a market.COLUMNS format code."""
    if v is None or v == "" or isinstance(v, Undefined):
        return "—"
    if fmt == "text":
        return str(v)
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if fmt == "pct":
        return f"{x * 100:.1f}%"
    if fmt == "spct":
        return f"{x * 100:+.1f}%"
    if fmt == "pct100":
        return f"{x:.1f}%" if x < 100 else f"{x:.0f}%"
    if fmt == "pp":
        return f"{x:+.2f}"
    if fmt == "money":
        return fmt_money(x)
    if fmt == "price":
        return f"{x:,.3f}" if x < 1 else f"{x:,.2f}"
    if fmt == "ratio":
        return f"{x:,.1f}"
    if fmt == "ratio0" or fmt == "score":
        return f"{x:.0f}"
    if fmt == "pct0":
        return f"{x * 100:.0f}%"
    return str(v)


_SMALL_WORDS = {"OF", "THE", "AND", "FOR", "CO", "NEW", "ONE", "OIL", "GAS", "BIG", "TOP", "AIR", "SEA", "BAY",
                "ART", "LAB", "MED", "BIO", "PAY", "NET", "WAY", "ICE", "RED", "SUN", "LTD", "INC", "PLC", "NL",
                "PTY", "TEN", "TWO", "SIX", "ALL", "ONE", "ORE", "LAW", "CAR", "BUY", "HUB", "KEY", "PRO", "AI"}


def company_name(v) -> str:
    """'BHP GROUP LIMITED' -> 'BHP Group Limited': title case, but short acronyms stay upper."""
    if not v or isinstance(v, Undefined):
        return ""
    s = str(v)
    if s != s.upper():
        return s                       # already mixed case: leave it alone
    words = []
    for w in s.split(" "):
        core = re.sub(r"[^A-Z0-9&]", "", w)
        if core and (len(core) <= 3 and core not in _SMALL_WORDS or any(ch.isdigit() for ch in core)):
            words.append(w)
        else:
            words.append(w.capitalize() if "-" not in w else "-".join(x.capitalize() for x in w.split("-")))
    return " ".join(words)


def score_class(v) -> str:
    """Colour band for a 0-100 score."""
    if v is None or isinstance(v, Undefined):
        return ""
    return "s-hi" if v >= 67 else "s-lo" if v < 33 else "s-mid"


def sent_class(score) -> str:
    if score is None or isinstance(score, Undefined):
        return "sent-none"
    return "sent-pos" if score >= 0.25 else "sent-neg" if score <= -0.25 else "sent-neu"


def sign_class(v) -> str:
    if v is None or isinstance(v, Undefined):
        return ""
    return "up" if v > 0 else "down" if v < 0 else ""


templates.env.filters["time"] = fmt_time
templates.env.filters["dmy"] = fmt_date
templates.env.globals["data_state"] = lambda: data_state()

# ISO dates inside any plain text a template prints (job summaries, notes built in Python, ...) are shown
# as DD/MM/YYYY too. Skipped: Markup (|tojson, |safe - use |safe for values that must stay ISO, e.g. date
# inputs) and dates glued to URL/identifier characters (/2026-11-12/, ?d=2026-11-12, 2026-11-12T...).
_ISO_IN_TEXT = re.compile(r"(?<![\w/=.-])(\d{4})-(\d{2})-(\d{2})(?![\w/-])")


def _finalize(v):
    if isinstance(v, str) and not isinstance(v, Markup) and "-" in v:
        return _ISO_IN_TEXT.sub(lambda m: f"{m[3]}/{m[2]}/{m[1]}", v)
    if isinstance(v, (date, datetime)) and not isinstance(v, datetime):
        return fmt_date(v)
    return v


templates.env.finalize = _finalize
templates.env.filters["cell"] = fmt_cell
templates.env.filters["money"] = fmt_money
templates.env.filters["sign"] = sign_class
templates.env.filters["coname"] = company_name
templates.env.filters["scorecls"] = score_class
templates.env.filters["sentcls"] = sent_class
templates.env.filters["age"] = fmt_age
templates.env.filters["num"] = fmt_num
templates.env.filters["smoney"] = lambda v: "—" if v is None or isinstance(v, Undefined) else f"{float(v):+,.0f}"


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    s = get_settings()
    ctx.setdefault("password_on", bool(s.app_password))
    ctx.setdefault("universe_label", s.universe.upper().replace("ASX", "ASX "))
    return templates.TemplateResponse(request, name, ctx)


# --- health & auth ----------------------------------------------------------

@app.get("/healthz")
def healthz():
    with get_conn() as c:
        c.execute("SELECT 1").fetchone()
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    if not get_settings().app_password:
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", next=next, error=None)


@app.post("/login")
def login(request: Request, password: str = Form(...), next: str = Form("/")):
    if not auth.password_ok(password):
        return render(request, "login.html", next=next, error="Wrong password")
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(auth.COOKIE, auth.make_cookie(), max_age=auth.MAX_AGE, httponly=True, samesite="lax")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


# --- pages ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    snap = status_snapshot()
    with get_conn() as c:
        rows = market.screener_rows(c)
        mkt = dashboard.overview(c)
        fc, as_of = _forecast_rows(c)
        latest_news, _ = newsq.news_list(c, days=3, limit=8)
        universe = [r["code"] for r in rows]
        ps_anns = [dict(r) for r in c.execute(
            f"SELECT a.code, a.headline, a.published_at, a.url, a.pdf_url FROM announcements a "
            f"WHERE a.price_sensitive=1 AND a.code IN ({','.join('?' * len(universe))}) "
            f"ORDER BY a.published_at DESC LIMIT 10", universe)] if universe else []
        wl = watchlist.rows(c)[:8]
        pfs = paper.portfolios(c)
        pf_cards = []
        for pf in pfs[:3]:
            sm = paper.summary(c, pf)
            pf_cards.append({**pf, **{k: sm.get(k) for k in ("value", "ret", "bench_ret", "start", "days")}})
    movers = [r for r in rows if r.get("chg_1d") is not None and not r.get("stale")]
    movers.sort(key=lambda r: r["chg_1d"])
    names = {r["code"]: r["name"] for r in rows}
    buys = sorted([r for r in fc if r["signal"] == "Buy"], key=lambda r: r["rank"] or 999)[:5]
    sells = sorted([r for r in fc if r["signal"] == "Sell"], key=lambda r: -(r["rank"] or 0))[:5]
    breadth = {"up": sum(1 for r in movers if r["chg_1d"] > 0), "down": sum(1 for r in movers if r["chg_1d"] < 0),
               "n": len(movers)}
    return render(request, "home.html", snap=snap, health=health(snap), mkt=mkt, breadth=breadth,
                  gainers=movers[::-1][:5], losers=movers[:5], latest_news=latest_news, ps_anns=ps_anns,
                  buys=buys, sells=sells, fc_as_of=as_of, skilled=_skilled(), names=names, wl=wl, pfs=pf_cards)


@app.get("/market/{slug}", response_class=HTMLResponse)
def market_page(request: Request, slug: str):
    """Detail page behind each dashboard market card."""
    if slug not in markets.MARKETS:
        raise HTTPException(404, f"Unknown market {slug}")
    with get_conn() as c:
        d = markets.detail(c, slug, market.screener_rows(c))
    return render(request, "market.html", m=d, others=[(k, v["label"]) for k, v in markets.MARKETS.items()])


@app.get("/api/market/{slug}/series")
def api_market_series(slug: str):
    if slug not in markets.MARKETS:
        raise HTTPException(404, f"Unknown market {slug}")
    with get_conn() as c:
        return markets.chart_points(c, slug)


# --- screener ---------------------------------------------------------------

SCREENER_PARAMS = ("q", "sector", "trend", "view", "sort", "dir", *market.NUM_FILTERS)


def _screener_ctx(request: Request) -> dict:
    params = {k: request.query_params.get(k, "") for k in SCREENER_PARAMS}
    sort = params["sort"] if params["sort"] in market.COLUMNS else "market_cap"
    desc = params["dir"] != "asc" if params["dir"] else sort not in market.ASC_FIRST
    with get_conn() as c:
        all_rows = market.screener_rows(c)
    rows = market.sort_rows(market.filter_rows(all_rows, params), sort, desc)
    sectors = sorted({r["sector"] for r in all_rows})
    return {"rows": rows, "total": len(all_rows), "params": params, "sort": sort, "desc": desc,
            "columns": market.COLUMNS, "show": market.VIEWS.get(params["view"], market.VIEWS["overview"])[1],
            "views": market.VIEWS, "view": params["view"] if params["view"] in market.VIEWS else "overview",
            "trends": market.TRENDS, "asc_first": market.ASC_FIRST, "filters": market.NUM_FILTERS,
            "sectors": sectors, "stale": sum(1 for r in rows if r.get("stale"))}


@app.get("/screener", response_class=HTMLResponse)
def screener(request: Request):
    ctx = _screener_ctx(request)
    if request.headers.get("HX-Request") and request.headers.get("HX-Target") == "screener-results":
        return render(request, "_screener_table.html", **ctx)
    return render(request, "screener.html", **ctx)


@app.get("/api/screener")
def api_screener(request: Request):
    ctx = _screener_ctx(request)
    return {"count": len(ctx["rows"]), "total": ctx["total"], "rows": ctx["rows"]}


@app.get("/go")
def go(q: str = ""):
    """Top-bar search: an exact ASX code goes to the company page, anything else to the screener."""
    q = q.strip()
    code = q.upper().removesuffix(".AX")
    if code:
        with get_conn() as c:
            if c.execute("SELECT 1 FROM companies WHERE code=?", (code,)).fetchone():
                return RedirectResponse(f"/company/{code}", status_code=303)
    return RedirectResponse(f"/screener?q={quote(q)}", status_code=303)


# --- news -------------------------------------------------------------------

NEWS_PARAMS = ("q", "code", "source", "event", "macro", "sentiment", "scope", "days")
NEWS_PAGE = 40


def _news_ctx(request: Request) -> dict:
    p = {k: request.query_params.get(k, "") for k in NEWS_PARAMS}
    try:
        days = int(p["days"]) if p["days"] else 7
    except ValueError:
        days = 7
    try:
        offset = max(0, int(request.query_params.get("offset", 0)))
    except ValueError:
        offset = 0
    with get_conn() as c:
        items, more = newsq.news_list(
            c, code=p["code"] or None, source=p["source"] or None, event=p["event"] or None,
            macro=p["macro"] or None, sentiment=p["sentiment"] or None, scope=p["scope"] or None,
            q=p["q"] or None, days=days or None, limit=NEWS_PAGE, offset=offset)
        sources = newsq.sources_summary(c)
    qs = "&".join(f"{k}={quote(v)}" for k, v in p.items() if v)
    return {"items": items, "more": more, "params": p, "days": days, "offset": offset,
            "next_url": f"/news?{qs + '&' if qs else ''}offset={offset + NEWS_PAGE}",
            "feeds": FEEDS, "events": EVENT_LABELS, "macros": MACRO_LABELS, "macro_sectors": MACRO_SECTORS,
            "sentiments": newsq.SENTIMENT_FILTERS, "sources": sources,
            "model_ok": news_sentiment.model_info() is not None}


@app.get("/news", response_class=HTMLResponse)
def news_page(request: Request):
    ctx = _news_ctx(request)
    target = request.headers.get("HX-Target")
    if request.headers.get("HX-Request") and target == "news-results":
        return render(request, "_news_results.html", **ctx)
    if request.headers.get("HX-Request") and target == "news-more":
        return render(request, "_news_items.html", **ctx, more_url=ctx["next_url"] if ctx["more"] else None)
    return render(request, "news.html", **ctx)


@app.get("/api/news")
def api_news(request: Request):
    ctx = _news_ctx(request)
    return {"count": len(ctx["items"]), "more": ctx["more"], "items": ctx["items"]}


# --- forecasts & backtests -------------------------------------------------------

def _latest_run(c, ok_only: bool = True) -> dict | None:
    row = c.execute("SELECT * FROM backtest_runs " + ("WHERE status='ok' " if ok_only else "")
                    + "ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    r = dict(row)
    for k in ("config_json", "summary_json", "curves_json"):
        r[k.removesuffix("_json")] = json.loads(r[k]) if r.get(k) else None
    return r


def _model_meta() -> dict | None:
    from app.pipeline.forecast import models_root
    p = models_root() / "current" / "meta.json"
    return json.loads(p.read_text()) if p.exists() else None


def _forecast_rows(c, horizon: int = 21) -> tuple[list[dict], str | None]:
    last = c.execute("SELECT MAX(as_of_date) FROM forecasts").fetchone()[0]
    if not last:
        return [], None
    rows = [dict(r) for r in c.execute(
        """SELECT f.*, c.name, c.sector, f63.q50 AS q50_63, f63.p_up AS p_up_63, f5.p_up AS p_up_5
           FROM forecasts f JOIN companies c ON c.code=f.code
           LEFT JOIN forecasts f63 ON f63.code=f.code AND f63.as_of_date=f.as_of_date AND f63.horizon=63
           LEFT JOIN forecasts f5 ON f5.code=f.code AND f5.as_of_date=f.as_of_date AND f5.horizon=5
           WHERE f.as_of_date=? AND f.horizon=?""", (last, horizon))]
    return rows, last


SIGNAL_ORDER = {"Buy": 0, "Hold": 1, "Sell": 2}


@app.get("/signals", response_class=HTMLResponse)
def signals_page(request: Request, signal: str = "", sort: str = "rank"):
    with get_conn() as c:
        rows, as_of = _forecast_rows(c)
        run = _latest_run(c)
        live = sigrecord.summarise(sigrecord.outcomes(c))
    meta = _model_meta()
    counts = {k: sum(1 for r in rows if r["signal"] == k) for k in SIGNAL_ORDER}
    if signal in SIGNAL_ORDER:
        rows = [r for r in rows if r["signal"] == signal]
    keyfn = {
        "rank": lambda r: (r["rank"] is None, r["rank"] or 0),
        "p_beat": lambda r: -(r["p_beat"] or 0),
        "ex50": lambda r: -(r["ex50"] or 0),
        "code": lambda r: r["code"],
        "signal": lambda r: (SIGNAL_ORDER.get(r["signal"], 9), r["rank"] or 999),
    }.get(sort, lambda r: (r["rank"] is None, r["rank"] or 0))
    rows.sort(key=keyfn)
    s = get_settings()
    return render(request, "signals.html", skilled=bool(meta and (meta.get("auc_beat") or 0) >= model_signals.AUC_SKILL),
                  rows=rows, as_of=as_of, meta=meta, run=run, counts=counts,
                  signal=signal, sort=sort, top_n=s.backtest_top_n, auc_skill=model_signals.AUC_SKILL,
                  bt=_bt_stats(run), live=live,
                  hurdle=model_signals.cost_hurdle(s.paper_capital, s.backtest_top_n, s.brokerage_per_trade,
                                                   s.slippage_bps))


@app.get("/api/signals")
def api_signals():
    with get_conn() as c:
        rows, as_of = _forecast_rows(c)
    for r in rows:
        r["explain"] = json.loads(r.pop("explain_json") or "{}")
    return {"as_of": as_of, "model": _model_meta(), "rows": rows}


BENCH_NAME = "ASX 200 buy-and-hold (STW, incl. dividends)"


def backtest_verdict(summary: dict | None) -> dict | None:
    """Plain verdict for the banner: compare the model strategies with buy-and-hold AND with
    random picks from the same list (the survivorship control)."""
    if not summary:
        return None
    st = summary.get("strategies", {})
    bench = (st.get(BENCH_NAME) or {}).get("cagr")
    models = {k: v["cagr"] for k, v in st.items() if k.startswith("Model") and v and v.get("cagr") is not None}
    if bench is None or not models:
        return None
    best_name, best = max(models.items(), key=lambda kv: kv[1])
    rc = summary.get("random_control") or {}
    if best <= bench:
        level = "bad"
        text = (f"The model strategies did not beat simply holding the ASX 200 ({best * 100:.1f}% a year at best vs "
                f"{bench * 100:.1f}%, after costs).")
    elif rc and best <= rc.get("cagr_p90", float("inf")):
        level = "bad"
        text = (f"The model strategies beat buy-and-hold ({best * 100:.1f}% vs {bench * 100:.1f}% a year), but so did picking "
                f"stocks at random from the same list: random picks returned a median {rc['cagr_p50'] * 100:.1f}% a year "
                f"(80% of 20 random runs between {rc['cagr_p10'] * 100:.1f}% and {rc['cagr_p90'] * 100:.1f}%). The edge comes from "
                f"survivorship bias (choosing among today's index members), not from the model.")
    else:
        lo = summary.get("large_only") or {}
        lo_model = next((v.get("cagr") for k, v in (lo.get("strategies") or {}).items() if k.startswith("Model")), None)
        lo_rc = lo.get("random_control") or {}
        if lo_model is not None and lo_rc and lo_model <= lo_rc.get("cagr_p90", float("inf")):
            level = "bad"
            text = (f"{best_name} beat buy-and-hold ({best * 100:.1f}% vs {bench * 100:.1f}% a year) and random picks, but the edge "
                    f"disappears among the {lo.get('n')} stocks that were largest at the time ({lo_model * 100:.1f}% vs a random "
                    f"median of {lo_rc['cagr_p50'] * 100:.1f}%, 90th percentile {lo_rc['cagr_p90'] * 100:.1f}%). It most likely comes "
                    f"from survivorship bias: today's index includes small companies that later soared, and momentum among them "
                    f"looks predictive only in hindsight.")
        else:
            level = "warn"
            text = (f"{best_name} beat buy-and-hold ({best * 100:.1f}% vs {bench * 100:.1f}% a year), 90% of random picks from "
                    f"the same list (median {rc.get('cagr_p50', 0) * 100:.1f}%), and held up among stocks that were large at the time. "
                    f"Survivorship bias still flatters every number here.")
    return {"level": level, "text": text, "best": best, "bench": bench}


@app.get("/backtest", response_class=HTMLResponse)
def backtest_page(request: Request):
    with get_conn() as c:
        run = _latest_run(c)
        latest_any = _latest_run(c, ok_only=False)
        history = [dict(r) for r in c.execute(
            "SELECT id, started_at, finished_at, status, message FROM backtest_runs ORDER BY id DESC LIMIT 10")]
        # how many distinct model designs have been scored on the current holdout
        designs = {r[0] for r in c.execute(
            "SELECT json_extract(summary_json, '$.fingerprint') FROM backtest_runs WHERE status='ok' "
            "AND json_extract(summary_json, '$.holdout_start')=?",
            ((run or {}).get("summary", {}).get("holdout_start") if run else None,)) if r[0]}
    return render(request, "backtest.html", run=run, latest_any=latest_any, history=history, meta=_model_meta(),
                  designs=len(designs),
                  auc_skill=model_signals.AUC_SKILL, verdict=backtest_verdict(run["summary"] if run else None))


@app.get("/api/backtest/latest")
def api_backtest_latest():
    with get_conn() as c:
        run = _latest_run(c, ok_only=False)
    if run is None:
        raise HTTPException(404, "no backtest yet")
    return {k: v for k, v in run.items() if not k.endswith("_json")}


# --- watchlist ---------------------------------------------------------------

def _back(request: Request, default: str) -> str:
    ref = request.headers.get("referer") or ""
    path = ref.split("://", 1)[-1].split("/", 1)
    target = "/" + path[1] if len(path) > 1 else default
    return target if target.startswith("/") and not target.startswith("//") else default


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request, error: str = ""):
    with get_conn() as c:
        rows = watchlist.rows(c)
        codes = [r["code"] for r in rows]
        news, _ = newsq.news_list(c, scope="company", days=14, limit=300) if codes else ([], False)
        anns = [dict(r) for r in c.execute(
            f"SELECT ids_id, code, headline, published_at, price_sensitive, url, pdf_url FROM announcements WHERE code IN "
            f"({','.join('?' * len(codes))}) ORDER BY published_at DESC LIMIT 30", codes)] if codes else []
        ai = news_ai.for_ids(c, [a["ids_id"] for a in anns])
        for a in anns:
            a["ai"] = ai.get(a["ids_id"])
    news = [n for n in news if any(l["code"] in codes for l in n.get("codes", []))][:15]
    return render(request, "watchlist.html", rows=rows, anns=anns, news=news, error=error, skilled=_skilled())


@app.post("/watchlist/add")
def watchlist_add(request: Request, code: str = Form(...), note: str = Form("")):
    try:
        with get_conn() as c:
            watchlist.add(c, code, note)
    except ValueError as e:
        return RedirectResponse(f"/watchlist?error={quote(str(e))}", status_code=303)
    _fetch_new_codes()
    return RedirectResponse(_back(request, "/watchlist"), status_code=303)


@app.post("/watchlist/{code}/toggle")
def watchlist_toggle(request: Request, code: str):
    code = code.upper()
    with get_conn() as c:
        on = watchlist.contains(c, code)
        if on:
            watchlist.remove(c, code)
        else:
            try:
                watchlist.add(c, code)
            except ValueError as e:
                raise HTTPException(404, str(e))
            _fetch_new_codes()
    if request.headers.get("HX-Request"):
        return render(request, "_watch_button.html", code=code, watched=not on)
    return RedirectResponse(_back(request, "/watchlist"), status_code=303)


@app.get("/api/watchlist")
def api_watchlist():
    with get_conn() as c:
        return {"rows": watchlist.rows(c)}


# --- paper trading -------------------------------------------------------------

def _skilled() -> bool:
    m = _model_meta()
    return bool(m and (m.get("auc_beat") or 0) >= model_signals.AUC_SKILL)


def _curve_points(s: pd.Series) -> list:
    return [[d, round(float(v), 2)] for d, v in s.items()]


@app.get("/paper", response_class=HTMLResponse)
def paper_page(request: Request, id: int | None = None, code: str = "", error: str = "", ok: str = ""):
    with get_conn() as c:
        pfs = paper.portfolios(c)
        cards = []
        for pf in pfs:
            sm = paper.summary(c, pf)
            cards.append({**pf, **{k: v for k, v in sm.items() if k not in ("curve", "bench", "positions")}})
        current = next((p for p in pfs if p["id"] == id), pfs[0] if pfs else None)
        detail = paper.summary(c, current) if current else None
        if detail:
            detail["curve_pts"], detail["bench_pts"] = _curve_points(detail["curve"]), _curve_points(detail["bench"])
        pending = paper.orders(c, current["id"], "pending") if current else []
        history = [o for o in paper.orders(c, current["id"]) if o["status"] != "pending"] if current else []
        names = {r[0]: r[1] for r in c.execute("SELECT code, name FROM companies")}
    s = get_settings()
    return render(request, "paper.html", pfs=cards, current=current, detail=detail, pending=pending,
                  history=history[:100], names=names, code=code.upper(), error=error, ok=ok,
                  brokerage=s.brokerage_per_trade, slippage=s.slippage_bps, capital=s.paper_capital,
                  min_trade=paper.MIN_TRADE, rebalance_days=paper.REBALANCE_DAYS, skilled=_skilled())


@app.get("/api/paper/{pid}")
def api_paper(pid: int):
    with get_conn() as c:
        pf = paper.get_portfolio(c, pid)
        if pf is None:
            raise HTTPException(404, "unknown portfolio")
        sm = paper.summary(c, pf)
        pf["orders"] = paper.orders(c, pid)
    sm["curve"], sm["bench"] = _curve_points(sm["curve"]), _curve_points(sm["bench"])
    return {**pf, **sm}


@app.post("/paper/new")
def paper_new(name: str = Form(...), capital: float = Form(...)):
    try:
        with get_conn() as c:
            pid = paper.create_portfolio(c, name, "manual", capital)
    except ValueError as e:
        return RedirectResponse(f"/paper?error={quote(str(e))}", status_code=303)
    return RedirectResponse(f"/paper?id={pid}", status_code=303)


@app.post("/paper/{pid}/order")
def paper_order(pid: int, code: str = Form(...), side: str = Form(...), size_type: str = Form("quantity"),
                size: float = Form(0)):
    try:
        with get_conn() as c:
            pf = paper.get_portfolio(c, pid)
            if pf is None or pf["kind"] != "manual":
                raise ValueError("orders can only be placed in your own (manual) portfolios")
            qty = amount = None
            if side == "sell" and size_type == "all":
                pass
            elif size_type == "amount":
                amount = size
            else:
                qty = int(size)
            oid = paper.place_order(c, pid, code, side, quantity=qty, amount=amount)
            o = c.execute("SELECT exec_after FROM paper_orders WHERE id=?", (oid,)).fetchone()
    except ValueError as e:
        return RedirectResponse(f"/paper?id={pid}&error={quote(str(e))}", status_code=303)
    _fetch_new_codes()
    msg = f"Order placed. It fills at the open of the first trading day from {o[0]}, once that day's prices arrive."
    return RedirectResponse(f"/paper?id={pid}&ok={quote(msg)}", status_code=303)


@app.post("/paper/{pid}/cancel/{oid}")
def paper_cancel(pid: int, oid: int):
    with get_conn() as c:
        if not c.execute("SELECT 1 FROM paper_orders WHERE id=? AND portfolio_id=?", (oid, pid)).fetchone():
            raise HTTPException(404, "unknown order")
        paper.cancel_order(c, oid)
    return RedirectResponse(f"/paper?id={pid}", status_code=303)


@app.post("/paper/{pid}/close")
def paper_close(pid: int):
    with get_conn() as c:
        c.execute("UPDATE paper_portfolios SET active=0 WHERE id=? AND kind='manual'", (pid,))
        c.execute("UPDATE paper_orders SET status='cancelled', message='portfolio closed' "
                  "WHERE portfolio_id=? AND status='pending'", (pid,))
    return RedirectResponse("/paper", status_code=303)


# --- holdings -------------------------------------------------------------------

@app.get("/holdings", response_class=HTMLResponse)
def holdings_page(request: Request, error: str = "", ok: str = "", code: str = ""):
    with get_conn() as c:
        try:
            sm = holdings.summary(c)
        except ValueError as e:      # inconsistent trades (shouldn't happen: validated on entry)
            sm, error = None, str(e)
        tr = holdings.trades(c)
    s = get_settings()
    return render(request, "holdings.html", sm=sm, trades=tr[::-1], error=error, ok=ok, code=code.upper(), sides=holdings.SIDES,
                  brokerage=s.brokerage_per_trade, today=date.today().isoformat(), skilled=_skilled(),
                  max_stock=holdings.MAX_STOCK_WEIGHT, max_sector=holdings.MAX_SECTOR_WEIGHT)


def _fetch_new_codes() -> None:
    start_job_thread("new_codes", JOBS["new_codes"])


@app.post("/holdings/trade")
def holdings_trade(code: str = Form(...), side: str = Form(...), trade_date: str = Form(...),
                   quantity: float = Form(...), price: float = Form(0.0), fees: float = Form(0.0), note: str = Form(""),
                   other_code: str = Form(""), ratio: float = Form(0.0), cost_pct: float = Form(0.0)):
    try:
        with get_conn() as c:
            holdings.add_trade(c, code, side, trade_date, quantity, price, fees, note,
                               other_code=other_code, ratio=ratio, cost_pct=cost_pct)
    except ValueError as e:
        return RedirectResponse(f"/holdings?error={quote(str(e))}", status_code=303)
    _fetch_new_codes()
    label = holdings.SIDES.get(side, side)
    return RedirectResponse(f"/holdings?ok={quote(f'{label} of {code.upper()} saved.')}", status_code=303)


@app.post("/holdings/trade/{tid}/delete")
def holdings_delete(tid: int):
    try:
        with get_conn() as c:
            holdings.delete_trade(c, tid)
    except ValueError as e:
        return RedirectResponse(f"/holdings?error={quote('Can’t delete that buy: ' + str(e))}", status_code=303)
    return RedirectResponse("/holdings?ok=Trade+deleted.", status_code=303)


@app.get("/api/holdings")
def api_holdings():
    with get_conn() as c:
        return holdings.summary(c)


# --- alerts --------------------------------------------------------------------------

@app.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request, ok: str = "", error: str = ""):
    s = get_settings()
    with get_conn() as c:
        rows = alerts.recent(c, 100)
        follow = alerts.followed_codes(c)
    return render(request, "alerts.html", rows=rows, channels=alerts.channels(), s=s, quiet=alerts.in_quiet_hours(),
                  follow=follow, ok=ok, error=error)


@app.post("/alerts/test")
def alerts_test():
    if not alerts.channels():
        return RedirectResponse("/alerts?error=" + quote("No alert channel is set up. Add NTFY_URL + NTFY_TOPIC or "
                                                         "WEBHOOK_URL to .env, then docker compose up -d."), status_code=303)
    now = datetime.now(timezone.utc)
    status = alerts.notify(f"test:{now.isoformat()}", "test", "ASX Analyst test alert",
                           "If you can read this, alerts work.", url="/alerts", force=True)
    if status == "sent":
        return RedirectResponse("/alerts?ok=" + quote("Test alert sent."), status_code=303)
    with get_conn() as c:
        err = c.execute("SELECT error FROM alerts WHERE kind='test' ORDER BY id DESC LIMIT 1").fetchone()[0]
    return RedirectResponse("/alerts?error=" + quote(f"Test alert failed: {err}"), status_code=303)


# --- CSV export --------------------------------------------------------------------

def _csv(name: str, rows: list[dict], columns: list[str] | None = None) -> Response:
    columns = columns or (list(rows[0].keys()) if rows else [])
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})
    stamp = date.today().isoformat()
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="asx-{name}-{stamp}.csv"'})


@app.get("/export/screener.csv")
def export_screener(request: Request):
    ctx = _screener_ctx(request)
    cols = [k for k in market.COLUMNS] + ["price_date", "stale"]
    return _csv("screener", ctx["rows"], cols)


@app.get("/export/signals.csv")
def export_signals():
    """Every daily signal since forecasts began, with the outcome over the following 21 trading days."""
    with get_conn() as c:
        fc = [dict(r) for r in c.execute(
            "SELECT f.as_of_date, f.code, co.name, f.signal, f.confidence, f.rank, f.p_beat, f.ex50, f.q10, f.q50, f.q90, "
            "f.p_up, f.eligible, f.model_version FROM forecasts f LEFT JOIN companies co ON co.code=f.code "
            "WHERE f.horizon=21 ORDER BY f.as_of_date DESC, f.rank IS NULL, f.rank, f.code")]
        outs = {(o["code"], o["as_of_date"]): o for o in sigrecord.outcomes(c, episodes=False)}
    for r in fc:
        o = outs.get((r["code"], r["as_of_date"]), {})
        r.update(outcome_end=o.get("end_date"), outcome_days=o.get("days"), stock_return=o.get("ret"),
                 asx200_return=o.get("bench_ret"), excess_return=o.get("excess"), finished=o.get("matured"))
    return _csv("signals-history", fc)


@app.get("/export/paper/{pid}.csv")
def export_paper(pid: int):
    with get_conn() as c:
        pf = paper.get_portfolio(c, pid)
        if pf is None:
            raise HTTPException(404, "unknown portfolio")
        rows = paper.orders(c, pid, limit=100000)[::-1]
    for r in rows:
        r["portfolio"] = pf["name"]
    return _csv(f"paper-{pid}", rows, ["portfolio", "id", "placed_at", "code", "side", "quantity", "amount", "exec_after",
                                       "status", "filled_date", "filled_qty", "price", "value", "fee", "reason",
                                       "signal_date", "message"])


@app.get("/export/holdings.csv")
def export_holdings():
    with get_conn() as c:
        sm = holdings.summary(c)
    rows = [{**{k: p[k] for k in ("code", "name", "sector", "units", "avg_cost", "cost", "price", "value", "pnl",
                                 "pnl_pct", "weight", "dividends", "franking", "signal")},
             "parcel_bought": l["buy_date"], "parcel_units": l["units"], "parcel_cost": l["cost"], "parcel_gain": l["gain"],
             "cgt_discount_from": l["discount_date"], "cgt_discount_now": l["eligible"]}
            for p in sm["positions"] for l in p["lots"]]
    return _csv("holdings", rows)


@app.get("/export/holdings-trades.csv")
def export_holdings_trades():
    with get_conn() as c:
        return _csv("holdings-trades", holdings.trades(c),
                    ["id", "trade_date", "code", "side", "quantity", "price", "fees", "note", "created_at"])


@app.get("/export/holdings-realised.csv")
def export_holdings_realised():
    with get_conn() as c:
        sm = holdings.summary(c)
    return _csv("realised-gains", sm["realised_rows"][::-1],
                ["sell_date", "code", "buy_date", "units", "cost", "proceeds", "gain", "discount"])


@app.get("/export/dividends.csv")
def export_dividends():
    with get_conn() as c:
        sm = holdings.summary(c)
    return _csv("dividends", sm["dividend_rows"][::-1],
                ["code", "ex_date", "pay_date", "units", "amount", "currency", "per_share_aud", "cash", "franking_pct",
                 "franking_credit", "franking_estimated", "received"])


# --- company ----------------------------------------------------------------

ANN_PAGE = 25


def _announcements(c, code: str, ps_only: bool, offset: int) -> list[dict]:
    return newsq.announcements_with_signals(c, code, ps_only, ANN_PAGE + 1, offset)


@app.get("/company/{code}", response_class=HTMLResponse)
def company_page(request: Request, code: str):
    code = code.upper()
    with get_conn() as c:
        comp = market.get_company(c, code)
        if comp is None:
            raise HTTPException(404, f"Unknown ASX code {code}")
        rows = market.screener_rows(c)
        me = next((r for r in rows if r["code"] == code), None)
        if me is None:  # not in the universe: compute what we can directly
            bars = c.execute("SELECT * FROM prices WHERE symbol=? ORDER BY date DESC LIMIT 300",
                             (comp["symbol"],)).fetchall()[::-1]
            me = {**dict.fromkeys(market.COLUMNS), "code": code, "sector": comp.get("sector"),
                  **market.price_metrics(bars),
                  **market.latest_fundamentals(c, [code]).get(code, {}),
                  **market.latest_shorts(c, [code]).get(code, {})}
        fund = market.latest_fundamentals(c, [code]).get(code, {})
        peer_rows, sector_med = market.peers(rows, code, comp.get("sector"))
        returns = market.returns_table(c, comp["symbol"], comp.get("sector"))
        divs = [dict(r) for r in c.execute(
            "SELECT * FROM dividends WHERE code=? ORDER BY ex_date DESC LIMIT 12", (code,))]
        year_ago = (date.today() - timedelta(days=365)).isoformat()
        ttm = [d for d in divs if d["ex_date"] > year_ago]
        fins = [dict(r) for r in c.execute(
            "SELECT * FROM financials_annual WHERE code=? AND revenue IS NOT NULL ORDER BY period_end DESC LIMIT 5",
            (code,))][::-1]
        today = date.today().isoformat()
        upcoming = [dict(r) for r in c.execute(
            "SELECT * FROM calendar_events WHERE code=? AND event_date>=? ORDER BY event_date", (code, today))]
        recent_events = [dict(r) for r in c.execute(
            "SELECT * FROM calendar_events WHERE code=? AND event_date<? ORDER BY event_date DESC LIMIT 4",
            (code, today))]
        tech = market.latest_technicals(c, [code]).get(code)
        if tech is None:  # not tracked: compute on the fly
            tech = indicators.latest(pd.read_sql_query(
                "SELECT date, high, low, close, adj_close, volume FROM "
                "(SELECT * FROM prices WHERE symbol=? ORDER BY date DESC LIMIT 400) ORDER BY date",
                c, params=(comp["symbol"],)))
        score = market.latest_scores(c, [code]).get(code)
        last_fc = c.execute("SELECT MAX(as_of_date) FROM forecasts WHERE code=?", (code,)).fetchone()[0]
        forecasts = {r["horizon"]: dict(r) for r in c.execute(
            "SELECT * FROM forecasts WHERE code=? AND as_of_date=?", (code, last_fc))} if last_fc else {}
        why = json.loads(forecasts[21]["explain_json"] or "{}") if 21 in forecasts else {}
        n_ranked = c.execute("SELECT COUNT(*) FROM forecasts WHERE as_of_date=? AND horizon=21 AND rank IS NOT NULL",
                             (last_fc,)).fetchone()[0] if last_fc else 0
        news_items, news_more = newsq.news_list(c, code=code, days=None, limit=10)
        news_summary = newsq.company_summary(c, code)
        anns = _announcements(c, code, False, 0)
        trades = [dict(r) for r in c.execute(
            "SELECT * FROM director_trades WHERE code=? ORDER BY published_at DESC LIMIT 15", (code,))]
        watched = watchlist.contains(c, code)
        sig_history = sigrecord.outcomes(c, code=code)[::-1][:12]
        run = _latest_run(c)
        net_90 = c.execute(
            "SELECT SUM(CASE trade_type WHEN 'buy' THEN value WHEN 'sell' THEN -value END), "
            "SUM(trade_type='buy'), SUM(trade_type='sell') FROM director_trades "
            "WHERE code=? AND trade_type IN ('buy','sell') AND published_at>=?",
            (code, (date.today() - timedelta(days=90)).isoformat())).fetchone()
    return render(
        request, "company.html", comp=comp, me=me, fund=fund, peers=peer_rows, sector_med=sector_med,
        returns=returns, periods=list(market.PERIODS), divs=divs, ttm_divs=ttm, fins=fins,
        upcoming=upcoming, recent_events=recent_events, anns=anns[:ANN_PAGE],
        anns_more=len(anns) > ANN_PAGE, ann_page=ANN_PAGE, trades=trades,
        net_90={"value": net_90[0], "buys": net_90[1] or 0, "sells": net_90[2] or 0},
        columns=market.COLUMNS, universe_codes={r["code"] for r in rows},
        tech=tech, tech_facts=indicators.describe(tech or {}), score=score, score_groups=_score_groups(score),
        news_items=news_items, news_more=news_more, news_summary=news_summary,
        model_ok=news_sentiment.model_info() is not None,
        forecasts=forecasts, why=why, n_ranked=n_ranked, model_meta=_model_meta(),
        skilled=bool(_model_meta() and (_model_meta().get("auc_beat") or 0) >= model_signals.AUC_SKILL),
        auc_skill=model_signals.AUC_SKILL, watched=watched, sig_history=sig_history, bt=_bt_stats(run))


def _bt_stats(run: dict | None) -> dict | None:
    """Backtest hit rate + drawdown of the production model strategy, shown next to signals."""
    if not run or not run.get("summary"):
        return None
    sm = run["summary"]
    label = "Model (with news)" if sm.get("production_variant") == "news" else "Model (without news)"
    st = (sm.get("strategies") or {}).get(label) or {}
    bench = (sm.get("strategies") or {}).get(BENCH_NAME) or {}
    lo = next((v for k, v in ((sm.get("large_only") or {}).get("strategies") or {}).items() if k.startswith("Model")), {})
    return {"label": label, "hit_rate": st.get("hit_rate_vs_bench"), "max_dd": st.get("max_drawdown"),
            "cagr": st.get("cagr"), "bench_cagr": bench.get("cagr"), "bench_dd": bench.get("max_drawdown"),
            "large_cagr": lo.get("cagr"), "large_hit": lo.get("hit_rate_vs_bench"), "start": st.get("start")}


def _score_groups(score: dict | None) -> list[dict]:
    """Scores grouped by category with per-metric value + sector percentile for the template."""
    if not score:
        return []
    detail = score.get("detail", {})
    groups = []
    for cat in scoring.CATEGORIES:
        metrics = [{"key": m, "label": info[1], "fmt": info[2], "higher": info[3], "help": info[4],
                    **detail.get(m, {"value": None, "pct": None})}
                   for m, info in scoring.METRICS.items() if info[0] == cat]
        groups.append({"key": cat, "label": scoring.CATEGORY_LABELS[cat], "score": score.get(cat),
                       "coverage": score.get("coverage", {}).get(cat), "metrics": metrics})
    return groups


@app.get("/company/{code}/announcements", response_class=HTMLResponse)
def company_announcements(request: Request, code: str, ps: int = 0, offset: int = 0):
    code = code.upper()
    with get_conn() as c:
        anns = _announcements(c, code, bool(ps), max(offset, 0))
    return render(request, "_announcements.html", code=code, anns=anns[:ANN_PAGE],
                  anns_more=len(anns) > ANN_PAGE, ps=ps, next_offset=offset + ANN_PAGE, append=offset > 0)


def _interval(interval: str) -> str:
    interval = (interval or "D").upper()
    if interval not in indicators.INTERVALS:
        raise HTTPException(400, "interval must be D, W or M")
    return interval


@app.get("/api/company/{code}/indicators")
def api_company_indicators(code: str, interval: str = "D"):
    """Indicator series for the chart overlays (full history, computed on request).
    interval W/M: computed on weekly/monthly bars (so sma50 = 50-week average)."""
    code = code.upper()
    interval = _interval(interval)
    with get_conn() as c:
        comp = market.get_company(c, code)
        if comp is None:
            raise HTTPException(404, f"Unknown ASX code {code}")
        df = pd.read_sql_query("SELECT date, high, low, close, adj_close, volume FROM prices WHERE symbol=? ORDER BY date",
                               c, params=(comp["symbol"],))
    if df.empty:
        return {}
    return indicators.chart_series(indicators.resample(df, interval))


@app.get("/api/company/{code}/chart")
def api_company_chart(code: str, interval: str = "D"):
    code = code.upper()
    interval = _interval(interval)
    with get_conn() as c:
        comp = market.get_company(c, code)
        if comp is None:
            raise HTTPException(404, f"Unknown ASX code {code}")
        return market.aggregate_payload(market.chart_payload(c, code, comp["symbol"]), interval)


SCHEDULE_ALIASES = {"daily_catchup": "daily_update", "train_first": "train"}


def _job_table(snap: dict) -> list[dict]:
    """One row per job: what it does, when, last run, last success, next run."""
    last = {j["job"]: j for j in snap["jobs"]}
    nxt: dict[str, str] = {}
    for sj in scheduled_jobs():
        job = SCHEDULE_ALIASES.get(sj["id"], sj["id"])
        if sj["next_run"] and (job not in nxt or sj["next_run"] < nxt[job]):
            nxt[job] = sj["next_run"]
    rows = []
    for job in list(JOB_INFO) + [j for j in last if j not in JOB_INFO]:
        r = last.get(job, {})
        dur = None
        if r.get("started_at") and r.get("finished_at"):
            try:
                dur = (datetime.fromisoformat(r["finished_at"]) - datetime.fromisoformat(r["started_at"])).total_seconds()
            except ValueError:
                pass
        when, what = JOB_INFO.get(job, ("", ""))
        rows.append({"job": job, "when": when, "what": what, "last": r, "duration": dur, "next_run": nxt.get(job),
                     "running": job in snap["running"], "runnable": job in JOBS})
    return rows


def fmt_duration(sec) -> str:
    if sec is None:
        return ""
    return f"{sec:.0f} s" if sec < 90 else f"{sec / 60:.0f} min" if sec < 5400 else f"{sec / 3600:.1f} h"


templates.env.filters["duration"] = fmt_duration
templates.env.filters["bytes"] = lambda b: "—" if b is None else (
    f"{b / 1e9:.2f} GB" if b >= 1e9 else f"{b / 1e6:.0f} MB" if b >= 1e6 else f"{b / 1e3:.0f} KB")


@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request, started: str | None = None, job: str = ""):
    snap = status_snapshot()
    with get_conn() as c:
        ai_usage = news_ai.usage(c)
    return render(request, "status.html", snap=snap, health=health(snap), jobs=_job_table(snap),
                  storage=storage(), started=started, started_job=job, ai_on=news_ai.enabled(), ai_usage=ai_usage,
                  s=get_settings())


@app.post("/jobs/{job}/run")
def run_job_form(job: str):
    if job not in JOBS:
        raise HTTPException(404, f"unknown job {job}")
    started = start_job_thread(job, JOBS[job])
    return RedirectResponse(f"/status?started={'1' if started else '0'}&job={job}", status_code=303)


@app.post("/refresh")
def refresh_form(request: Request):
    started = start_job_thread("daily_update", JOBS["daily_update"])
    # Header button without JS: go back to the page it was pressed on; Status page form: show the result there.
    back = request.headers.get("referer", "")
    if back and "/status" not in back and back.startswith(str(request.base_url)):
        return RedirectResponse(back, status_code=303)
    return RedirectResponse(f"/status?started={'1' if started else '0'}", status_code=303)


def _data_state_json() -> dict:
    st = data_state()
    st["last_update_text"] = fmt_time(st["last_update"]) if st["last_update"] else None
    st["last_update_age"] = fmt_age(st["last_update"]) if st["last_update"] else None
    st["price_date_text"] = fmt_date(st["price_date"]) if st["price_date"] else None
    st["started_text"] = fmt_time(st["started_at"])[-5:] if st["started_at"] else None     # HH:MM
    return st


@app.get("/api/refresh")
def api_refresh_state():
    """Header data bar: last update + progress of a running daily update (polled by app.js)."""
    return _data_state_json()


@app.post("/api/refresh")
def api_refresh_start():
    started = start_job_thread("daily_update", JOBS["daily_update"])
    return JSONResponse({**_data_state_json(), "started": started}, status_code=202 if started else 409)


# --- JSON API ---------------------------------------------------------------

@app.get("/api/status")
def api_status():
    snap = status_snapshot()
    snap["schedule"] = scheduled_jobs()
    snap["health"] = health(snap)
    snap["storage"] = storage()
    return snap


@app.post("/api/refresh")
def api_refresh():
    started = start_job_thread("daily_update", JOBS["daily_update"])
    return JSONResponse({"started": started, "job": "daily_update"}, status_code=202 if started else 409)


@app.post("/api/jobs/{job}/run")
def api_run_job(job: str):
    if job not in JOBS:
        raise HTTPException(404, f"unknown job {job}")
    started = start_job_thread(job, JOBS[job])
    return JSONResponse({"started": started, "job": job}, status_code=202 if started else 409)

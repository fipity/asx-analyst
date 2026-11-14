"""Data pipeline steps and composite jobs.

Every step is resumable: re-running picks up from what is already stored
(prices fetch from the last stored date, ASIC skips processed files,
announcement backfill tracks (code, year) in backfill_progress). Per-item
failures are recorded and skipped, never fatal.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from app import alerts
from app.config import MARKET_TZ, get_settings
from app.db import get_conn, kv_get, kv_set, upsert, utcnow
from app.jobs import JobContext, source_ok
from app.pipeline import analysis as analysis_steps
from app.pipeline import forecast as forecast_steps
from app.pipeline import news as news_steps
from app.news import ai as news_ai
from app.pipeline import maintenance
from app.portfolio import paper
from app.sources import appendix3y, asic, asx, rba, universe, yahoo
from app.sources.base import HttpClient, NotFound, SourceError

log = logging.getLogger(__name__)


def market_today() -> date:
    return datetime.now(MARKET_TZ).date()


# --- universe ---------------------------------------------------------------

def latest_members(conn, index_code: str) -> list[str]:
    row = conn.execute("SELECT MAX(as_of) FROM index_membership WHERE index_code=?", (index_code,)).fetchone()
    if not row or not row[0]:
        return []
    return [r[0] for r in conn.execute(
        "SELECT code FROM index_membership WHERE index_code=? AND as_of=? ORDER BY weight DESC",
        (index_code, row[0]))]


def tracked_codes() -> list[str]:
    """Universe members plus anything held or watched (tables added in later phases)."""
    s = get_settings()
    with get_conn() as c:
        codes = latest_members(c, s.universe)
        extra: list[str] = []
        for sql in ("SELECT code FROM watchlist", "SELECT code FROM holding_trades",
                    "SELECT other_code FROM holding_trades WHERE other_code IS NOT NULL",
                    "SELECT code FROM paper_orders"):
            extra += sorted({r[0] for r in c.execute(sql)})
    seen, out = set(), []
    for code in codes + extra:
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


def step_universe(ctx: JobContext) -> None:
    s = get_settings()
    http = HttpClient("universe")
    now = utcnow()
    try:
        directory = universe.fetch_directory(http)
        source_ok("asx-directory")
    except SourceError as e:
        ctx.error("asx-directory", "directory", e)
        directory = []
    with get_conn() as c:
        if directory:
            upsert(c, "companies", [{
                "code": d.code, "name": d.name, "industry_group": d.industry_group,
                "sector": d.sector, "listing_date": d.listing_date, "market_cap": d.market_cap,
                "status": "listed", "yahoo_symbol": yahoo.yahoo_symbol(d.code),
                "first_seen": now, "updated_at": now, "fetched_at": now,
            } for d in directory], pk=["code"],
                update=["name", "industry_group", "sector", "listing_date", "market_cap",
                        "status", "yahoo_symbol", "updated_at", "fetched_at"],
                keep_existing=["sector", "industry_group", "listing_date", "market_cap"])
            # Anything we know about that is no longer in the directory has delisted.
            listed = {d.code for d in directory}
            known = [r[0] for r in c.execute("SELECT code FROM companies WHERE status='listed'")]
            gone = [k for k in known if k not in listed]
            if len(gone) < 0.2 * max(1, len(known)):   # guard against a truncated file
                c.executemany("UPDATE companies SET status='delisted', updated_at=? WHERE code=?",
                              [(now, g) for g in gone])
                if gone:
                    ctx.notes.append(f"{len(gone)} delisted")
        ctx.items += len(directory)

    for idx in dict.fromkeys([s.universe, "asx200"]):
        try:
            snap = universe.fetch_index(http, idx)
        except (SourceError, ValueError) as e:
            ctx.error("index-" + idx, idx, e)
            continue
        source_ok(snap.source.split("+")[0].replace("top50-of-", "").replace("top100-of-", ""))
        with get_conn() as c:
            upsert(c, "index_membership", [{
                "index_code": snap.index_code, "code": m.code, "as_of": snap.as_of,
                "weight": m.weight, "source": snap.source, "approximate": int(snap.approximate),
                "fetched_at": now,
            } for m in snap.members], pk=["index_code", "as_of", "code"])
            # Make sure every member has a company row; fill sector from the ETF file.
            upsert(c, "companies", [{
                "code": m.code, "name": m.name, "sector": m.sector, "status": "listed",
                "yahoo_symbol": yahoo.yahoo_symbol(m.code), "first_seen": now,
                "updated_at": now, "fetched_at": now,
            } for m in snap.members], pk=["code"],
                update=["sector"], keep_existing=["sector"])
        ctx.notes.append(f"{idx}: {len(snap.members)} members as of {snap.as_of} ({snap.source})")
    with get_conn() as c:
        kv_set(c, "universe_refreshed_at", now)
    http.close()


def universe_is_stale(max_age_days: int = 7) -> bool:
    with get_conn() as c:
        ts = kv_get(c, "universe_refreshed_at")
        if not ts or not latest_members(c, get_settings().universe):
            return True
    return datetime.fromisoformat(ts) < datetime.now(timezone.utc) - timedelta(days=max_age_days)


# --- prices -----------------------------------------------------------------

def _last_price_date(symbol: str) -> date | None:
    with get_conn() as c:
        row = c.execute("SELECT MAX(date) FROM prices WHERE symbol=?", (symbol,)).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def _history_readjusted(symbol: str, fetched: list[dict], before: str, tol: float = 1e-4) -> bool:
    """True if Yahoo's adjustment of the overlapping, already stored days (before `before`, whose
    bar may have been stored mid-session) changed."""
    by_date = {p["date"]: p for p in fetched if p["date"] < before}
    if not by_date:
        return False
    with get_conn() as c:
        stored = c.execute(f"SELECT date, close, adj_close FROM prices WHERE symbol=? AND date IN "
                           f"({','.join('?' * len(by_date))})", [symbol, *by_date]).fetchall()
    for r in stored:
        p = by_date[r["date"]]
        for col in ("close", "adj_close"):
            a, b = r[col], p.get(col)
            if a and b and abs(b / a - 1) > tol:
                return True
    return False


def update_symbol_prices(ctx: JobContext, symbol: str, code: str | None = None) -> None:
    last = _last_price_date(symbol)
    start = last - timedelta(days=10) if last else None
    if last and last >= market_today():
        return
    try:
        prices, divs, splits = yahoo.fetch_history(symbol, start)
    except NotFound as e:
        if code and last is None:
            with get_conn() as c:
                c.execute("UPDATE companies SET status='no_data', updated_at=? WHERE code=? AND status='listed'",
                          (utcnow(), code))
        ctx.error("yahoo", symbol, f"{e} (delisted or not on Yahoo?)")
        return
    except SourceError as e:
        ctx.error("yahoo", symbol, e)
        return
    if last and prices and _history_readjusted(symbol, prices, last.isoformat()):
        # A new dividend or split since the last fetch: Yahoo re-scales the whole history
        # (adj_close for dividends, all prices for splits). Re-download it so total returns
        # across the ex-date stay correct.
        try:
            prices, divs, splits = yahoo.fetch_history(symbol, None)
            ctx.notes.append(f"{symbol}: history re-adjusted")
        except SourceError as e:
            ctx.error("yahoo", symbol, f"full refetch after adjustment change: {e}")
            return
    now = utcnow()
    with get_conn() as c:
        upsert(c, "prices", [{"symbol": symbol, **p, "fetched_at": now} for p in prices],
               pk=["symbol", "date"])
        if code:
            # Yahoo's amounts for .AX listings are in AUD, whatever currency the dividend was declared in
            upsert(c, "dividends", [{"code": code, **d, "currency": "AUD", "source": "yahoo", "fetched_at": now}
                                    for d in divs],
                   pk=["code", "ex_date"], update=["amount", "currency", "fetched_at"])
            upsert(c, "splits", [{"code": code, **sp, "fetched_at": now} for sp in splits],
                   pk=["code", "date"])
    source_ok("yahoo")
    ctx.items += 1


def step_market_prices(ctx: JobContext) -> None:
    for sym in yahoo.MARKET_SERIES:
        update_symbol_prices(ctx, sym)


def step_stock_prices(ctx: JobContext, codes: list[str]) -> None:
    for i, code in enumerate(codes, 1):
        update_symbol_prices(ctx, yahoo.yahoo_symbol(code), code)
        if i % 10 == 0:
            ctx.progress(f"prices {i}/{len(codes)}")


# --- fundamentals, dividends, calendar --------------------------------------

def step_fundamentals(ctx: JobContext, codes: list[str], financials: bool = False) -> None:
    http = HttpClient("asx")
    today = market_today().isoformat()
    for i, code in enumerate(codes, 1):
        sym = yahoo.yahoo_symbol(code)
        now = utcnow()
        with get_conn() as c:
            have = {r[0] for r in c.execute(
                "SELECT source FROM fundamentals WHERE code=? AND as_of_date=?", (code, today))}
            have_fin = c.execute("SELECT COUNT(*) FROM financials_annual WHERE code=?", (code,)).fetchone()[0]
        if "yahoo" not in have:
            try:
                info = yahoo.fetch_info(sym)
                m = yahoo.map_info(info)
                with get_conn() as c:
                    upsert(c, "fundamentals", [{
                        "code": code, "as_of_date": today, "source": "yahoo", **m,
                        "raw_json": None, "fetched_at": now}], pk=["code", "as_of_date", "source"])
                    if m.get("market_cap"):
                        c.execute("UPDATE companies SET market_cap=? WHERE code=?", (m["market_cap"], code))
                source_ok("yahoo-fundamentals")
            except SourceError as e:
                ctx.error("yahoo-fundamentals", code, e)
            try:
                events = yahoo.fetch_calendar(sym)
                with get_conn() as c:
                    upsert(c, "calendar_events", [{"code": code, **e, "detail": None, "source": "yahoo",
                                                   "fetched_at": now} for e in events],
                           pk=["code", "event_type", "event_date"])
            except SourceError as e:
                ctx.error("yahoo-calendar", code, e)
        if financials or not have_fin:
            try:
                rows = yahoo.fetch_financials(sym)
                with get_conn() as c:
                    upsert(c, "financials_annual", [{"code": code, "source": "yahoo", **r, "fetched_at": now}
                                                    for r in rows], pk=["code", "period_end", "source"])
            except SourceError as e:
                ctx.error("yahoo-financials", code, e)
        if "asx" not in have:
            try:
                ks = asx.fetch_key_statistics(http, code)
                dv = ks.pop("dividend")
                ks.pop("raw", None)
                with get_conn() as c:
                    upsert(c, "fundamentals", [{"code": code, "as_of_date": today, "source": "asx",
                                                **ks, "fetched_at": now}],
                           pk=["code", "as_of_date", "source"])
                    if dv and dv.get("ex_date"):
                        # Merge franking/pay/record into the Yahoo dividend row. Keep
                        # Yahoo's AUD amount + currency if present (ASX may quote in USD).
                        c.execute(
                            "INSERT INTO dividends(code, ex_date, amount, currency, franking_pct, pay_date,"
                            " record_date, source, fetched_at) VALUES(?,?,?,?,?,?,?,?,?)"
                            " ON CONFLICT(code, ex_date) DO UPDATE SET franking_pct=excluded.franking_pct,"
                            " pay_date=excluded.pay_date, record_date=excluded.record_date,"
                            " currency=COALESCE(dividends.currency, excluded.currency),"
                            " fetched_at=excluded.fetched_at",
                            (code, dv["ex_date"], dv.get("amount"), dv.get("currency"), dv.get("franking_pct"),
                             dv.get("pay_date"), dv.get("record_date"), "asx", now))
                        upsert(c, "calendar_events", [
                            {"code": code, "event_type": "ex_dividend", "event_date": dv["ex_date"],
                             "detail": f"{dv.get('amount')} {dv.get('currency') or ''} "
                                       f"{dv.get('franking_pct') or 0:.0f}% franked", "source": "asx",
                             "fetched_at": now}]
                            + ([{"code": code, "event_type": "dividend_pay", "event_date": dv["pay_date"],
                                 "detail": None, "source": "asx", "fetched_at": now}] if dv.get("pay_date") else []),
                            pk=["code", "event_type", "event_date"])
                source_ok("asx-keystats")
            except NotFound:
                pass
            except SourceError as e:
                ctx.error("asx-keystats", code, e)
        ctx.items += 1
        if i % 10 == 0:
            ctx.progress(f"fundamentals {i}/{len(codes)}")
    http.close()


# --- announcements & director trades ----------------------------------------

def _store_announcements(anns: list[asx.Announcement], source: str) -> int:
    now = utcnow()
    with get_conn() as c:
        before = c.total_changes
        c.executemany(
            "INSERT INTO announcements(ids_id, code, published_at, headline, price_sensitive, pages, url,"
            " source, fetched_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(ids_id) DO NOTHING",
            [(a.ids_id, a.code, a.published_at, a.headline, int(a.price_sensitive), a.pages, a.url,
              source, now) for a in anns])
        return c.total_changes - before


def step_announcements_recent(ctx: JobContext) -> int:
    http = HttpClient("asx")
    new = 0
    for name, fn in (("today", asx.fetch_today), ("prev", asx.fetch_prev_day)):
        try:
            anns = fn(http)
            new += _store_announcements(anns, f"asx-{name}")
            source_ok("asx-announcements")
        except SourceError as e:
            ctx.error("asx-announcements", name, e)
    ctx.items += new
    ctx.notes.append(f"{new} new announcements")
    http.close()
    return new


def step_announcements_backfill(ctx: JobContext, codes: list[str], years: int) -> None:
    http = HttpClient("asx")
    this_year = market_today().year
    todo = [(code, y) for code in codes for y in range(this_year - years + 1, this_year + 1)]
    with get_conn() as c:
        done = {r[0] for r in c.execute("SELECT key FROM backfill_progress WHERE task='announcements'")}
    todo = [(c_, y) for c_, y in todo if f"{c_}:{y}" not in done]
    for i, (code, year) in enumerate(todo, 1):
        try:
            anns = asx.fetch_company_year(http, code, year)
            n = _store_announcements(anns, "asx-year")
            ctx.items += n
            with get_conn() as c:
                upsert(c, "backfill_progress", [{"task": "announcements", "key": f"{code}:{year}",
                                                 "done_at": utcnow(), "status": f"{len(anns)} rows"}],
                       pk=["task", "key"])
            source_ok("asx-announcements")
        except NotFound:
            pass
        except SourceError as e:
            ctx.error("asx-announcements", f"{code} {year}", e)
        if i % 20 == 0:
            ctx.progress(f"announcement backfill {i}/{len(todo)}")
    http.close()


def step_director_trades(ctx: JobContext, codes: list[str], since_days: int, limit: int = 5000) -> None:
    if not codes:
        return
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    qmarks = ",".join("?" * len(codes))
    with get_conn() as c:
        rows = c.execute(
            f"SELECT a.ids_id, a.code, a.published_at, a.headline FROM announcements a"
            f" LEFT JOIN director_trades d ON d.ids_id=a.ids_id"
            f" WHERE d.ids_id IS NULL AND a.published_at>=? AND a.code IN ({qmarks})"
            f" AND lower(a.headline) LIKE '%director%interest%'"
            f" ORDER BY a.published_at DESC LIMIT ?", (since, *codes, limit)).fetchall()
    rows = [r for r in rows if appendix3y.is_3y_headline(r["headline"])]
    http = HttpClient("asx")
    for i, r in enumerate(rows, 1):
        try:
            pdf_url, data = asx.fetch_pdf(http, r["ids_id"])
        except SourceError as e:
            ctx.error("asx-pdf", r["ids_id"], e)
            continue
        try:
            t = appendix3y.parse_appendix_3y(appendix3y.pdf_to_text(data))
            rec = t.__dict__ | {"on_market": int(t.on_market), "parse_ok": int(t.parse_ok)}
        except Exception as e:  # malformed PDF: keep a stub so we don't refetch forever
            log.info("3Y parse failed %s: %s", r["ids_id"], e)
            rec = {"director": None, "change_date": None, "security_class": None, "acquired": None,
                   "disposed": None, "value": None, "nature": None, "trade_type": "unparsed",
                   "on_market": 0, "parse_ok": 0}
        with get_conn() as c:
            upsert(c, "director_trades", [{"ids_id": r["ids_id"], "code": r["code"],
                                           "published_at": r["published_at"], **rec,
                                           "fetched_at": utcnow()}], pk=["ids_id"])
            c.execute("UPDATE announcements SET pdf_url=? WHERE ids_id=?", (pdf_url, r["ids_id"]))
        ctx.items += 1
        if i % 20 == 0:
            ctx.progress(f"director notices {i}/{len(rows)}")
    http.close()


# --- ASIC short positions ---------------------------------------------------

def step_shorts(ctx: JobContext, years: int, max_files: int | None = None) -> None:
    http = HttpClient("asic")
    try:
        index = asic.fetch_index(http)
        source_ok("asic")
    except SourceError as e:
        ctx.error("asic", "index", e)
        return
    cutoff = (market_today() - timedelta(days=365 * years)).isoformat()
    with get_conn() as c:
        done = {r[0]: r[1] for r in c.execute("SELECT date, version FROM asic_files WHERE status='ok'")}
    todo = [(d, v) for d, v in index if d >= cutoff and done.get(d) != v]
    if max_files:
        todo = todo[:max_files]
    for i, (d, v) in enumerate(todo, 1):
        try:
            rows = asic.fetch_daily(http, d, v)
        except SourceError as e:
            ctx.error("asic", d, e)
            with get_conn() as c:
                upsert(c, "asic_files", [{"date": d, "version": v, "status": "error", "rows": 0,
                                          "fetched_at": utcnow()}], pk=["date"])
            continue
        avail = asic.add_bdays(date.fromisoformat(d), asic.PUBLICATION_LAG_BDAYS).isoformat()
        now = utcnow()
        with get_conn() as c:
            upsert(c, "short_positions", [{
                "code": r.code, "date": d, "short_qty": r.short_qty, "issued_qty": r.issued_qty,
                "short_pct": r.short_pct, "available_at": avail, "fetched_at": now} for r in rows],
                pk=["code", "date"])
            upsert(c, "asic_files", [{"date": d, "version": v, "status": "ok", "rows": len(rows),
                                      "fetched_at": now}], pk=["date"])
        ctx.items += 1
        if i % 25 == 0:
            ctx.progress(f"ASIC files {i}/{len(todo)}")
    source_ok("asic")
    http.close()


# --- macro ------------------------------------------------------------------

def step_macro(ctx: JobContext) -> None:
    http = HttpClient("rba")
    now = utcnow()
    for series, fn in (("cash_rate", rba.fetch_cash_rate), ("audusd_rba", rba.fetch_audusd)):
        try:
            rows = fn(http)
            with get_conn() as c:
                upsert(c, "macro_series", [{"series": series, "date": d, "value": v, "source": "rba",
                                            "fetched_at": now} for d, v in rows], pk=["series", "date"])
            ctx.items += 1
            source_ok("rba")
        except SourceError as e:
            ctx.error("rba", series, e)
    http.close()


# --- composite jobs -----------------------------------------------------------

def _parallel(ctx: JobContext, *branches) -> None:
    """Run independent branches (different hosts) concurrently."""
    with ThreadPoolExecutor(max_workers=len(branches)) as ex:
        futures = [ex.submit(b) for b in branches]
        for f in futures:
            try:
                f.result()
            except Exception as e:
                ctx.error("job", "branch", e)


def job_daily_update(ctx: JobContext) -> None:
    """Incremental update; used by the 16:10 schedule and the Refresh button."""
    if universe_is_stale():
        step_universe(ctx)
    codes = tracked_codes()
    if not codes:
        raise SourceError("universe is empty; constituent sources failed")

    def yahoo_branch():
        step_market_prices(ctx)
        step_stock_prices(ctx, codes)

    def asx_branch():
        step_announcements_recent(ctx)
        step_director_trades(ctx, codes, since_days=14)

    def other_branch():
        step_macro(ctx)
        step_shorts(ctx, years=1, max_files=30)

    _parallel(ctx, yahoo_branch, asx_branch, other_branch)
    step_fundamentals(ctx, codes, financials=market_today().weekday() == 4)
    news_steps.step_tag_announcements(ctx)
    news_steps.step_sentiment(ctx, limit=3000)
    step_analysis(ctx)
    forecast_steps.step_forecast(ctx)
    paper.step_paper(ctx)
    news_ai.step_ai_summaries(ctx)
    alerts.step_alerts(ctx)
    with get_conn() as c:
        kv_set(c, "last_daily_update", utcnow())


def job_initial_backfill(ctx: JobContext) -> None:
    s = get_settings()
    step_universe(ctx)
    codes = tracked_codes()
    if not codes:
        raise SourceError("universe is empty; constituent sources failed")
    ctx.progress(f"backfilling {len(codes)} companies")

    def yahoo_branch():
        step_market_prices(ctx)
        step_stock_prices(ctx, codes)
        step_fundamentals(ctx, codes, financials=True)

    def asx_branch():
        step_announcements_recent(ctx)
        step_announcements_backfill(ctx, codes, s.announcement_backfill_years)
        step_director_trades(ctx, codes, since_days=s.director_trades_backfill_days)

    def other_branch():
        step_macro(ctx)
        step_shorts(ctx, years=s.asic_backfill_years)

    _parallel(ctx, yahoo_branch, asx_branch, other_branch)
    step_analysis(ctx)
    with get_conn() as c:
        kv_set(c, "initial_backfill_done", utcnow())
        kv_set(c, "last_daily_update", utcnow())


def step_analysis(ctx: JobContext) -> None:
    """Technical snapshot for tracked codes; scores ranked within the configured universe."""
    ctx.progress("computing indicators and scores")
    analysis_steps.step_technicals(ctx, tracked_codes())
    with get_conn() as c:
        members = latest_members(c, get_settings().universe)
    analysis_steps.step_scores(ctx, members, market_today())


def job_analysis(ctx: JobContext) -> None:
    step_analysis(ctx)


def job_announcements(ctx: JobContext) -> None:
    """30-minute poll: ASX announcements, director notices, RSS news, tags, sentiment for new items."""
    codes = tracked_codes()
    step_announcements_recent(ctx)
    step_director_trades(ctx, codes, since_days=3)
    news_steps.step_rss(ctx, codes)
    news_steps.step_tag_announcements(ctx)
    news_steps.step_sentiment(ctx, limit=3000)
    news_ai.step_ai_summaries(ctx)
    alerts.step_alerts(ctx)


def job_news_backfill(ctx: JobContext) -> None:
    """One-off (resumable): tag every stored announcement and score all headlines with FinBERT.
    ~15 min for ~52k distinct headlines on 2 cores; runs at nice 10."""
    news_steps.step_tag_announcements(ctx)
    news_steps.step_sentiment(ctx, limit=None, background=True)
    with get_conn() as c:
        if news_steps.backlog(c)["ann_unscored"] == 0:
            kv_set(c, "news_backfill_done", utcnow())


def job_new_codes(ctx: JobContext) -> None:
    """Price history for followed codes (watchlist, holdings, paper) that have none yet, so a newly
    added stock outside the index has prices straight away instead of after the next daily update."""
    with get_conn() as c:
        have = {r[0] for r in c.execute("SELECT DISTINCT symbol FROM prices")}
    for code in tracked_codes():
        sym = yahoo.yahoo_symbol(code)
        if sym not in have:
            update_symbol_prices(ctx, sym, code)


def job_shorts(ctx: JobContext) -> None:
    step_shorts(ctx, years=1, max_files=30)


def job_universe(ctx: JobContext) -> None:
    step_universe(ctx)


JOBS = {
    "daily_update": job_daily_update,
    "initial_backfill": job_initial_backfill,
    "announcements": job_announcements,
    "shorts": job_shorts,
    "universe": job_universe,
    "analysis": job_analysis,
    "news_backfill": job_news_backfill,
    "train": forecast_steps.step_train,
    "forecast": forecast_steps.step_forecast,
    "paper": paper.step_paper,
    "maintenance": maintenance.job_maintenance,
    "new_codes": job_new_codes,
}

# CLAUDE.md — ASX Analyst

Self-hosted FastAPI + SQLite app, runs via Docker Compose on a headless Linux home server (port 8000). Spec: the user's original brief (summarised in PLAN.md). Progress: PROGRESS.md (read it first; update it after every meaningful step).

## Rules from the user
- Don't touch other containers, networks, volumes or compose files on this server.
- Build in phases; after each phase: update PROGRESS.md, commit, **stop and wait** for the user.
- Commit small working increments. Never leave the repo broken; note half-done work in PROGRESS.md.
- Verify with curl + tests inside the container (no browser on the server).
- Honesty: no look-ahead, include brokerage, state survivorship bias, compare with ASX 200 buy-and-hold.
- Never place real trades or connect to a broker. Respect robots.txt; store headline/summary only for news.
- On "continue": read PROGRESS.md + CLAUDE.md, `git status`, run the tests, carry on from "Next step".

## Layout
```
app/
  main.py          FastAPI app, routes, template filters, lifespan (init_db + scheduler)
  config.py        Settings (pydantic-settings, .env), MARKET_TZ
  db.py            sqlite3 helpers, MIGRATIONS list (PRAGMA user_version), upsert()
  jobs.py          run_job/start_job_thread/run_exclusive; job_runs, errors, source_status
  scheduler.py     APScheduler cron jobs (Sydney time)
  status.py        Status page queries (freshness per dataset)
  auth.py          optional APP_PASSWORD middleware (signed cookie)
  pipeline/ingest.py  steps (step_*) + composite jobs (JOBS dict)
  analysis/market.py  read-side queries: screener rows, filters, views, peers, returns, chart payload
  analysis/indicators.py  technical indicators (pure, point-in-time safe)
  analysis/scoring.py     sector-percentile fundamental scores (pure + point-in-time gather())
  pipeline/analysis.py    step_technicals / step_scores (job `analysis`, also end of daily_update)
  pipeline/news.py        step_rss / step_tag_announcements / step_sentiment (jobs `announcements`, `news_backfill`)
  news/            text.py (dedupe), matching.py (company links), tagging.py (events, macro), sentiment.py (FinBERT ONNX), queries.py
  models/          features.py (point-in-time panel + targets + eligibility), train.py (LightGBM, purged walk-forward,
                   evaluation incl. dev/holdout split, save/load), calibrate.py (isotonic probs, conformal ranges), backtest.py (top-N engine, metrics), signals.py (Buy/Hold/Sell, explanations)
  pipeline/forecast.py    jobs `train` (weekly Sat 02:00: walk-forward + backtests + final fit) and `forecast` (end of daily_update)
  pipeline/maintenance.py job `maintenance` (Sun 03:30): gzipped online DB backup to data/backups/asx-auto-*, prune errors/job_runs
  portfolio/       paper.py (paper trading: orders -> next-open fills, replayed ledger, equity vs STW, `paper` job +
                   model forward-test portfolio), watchlist.py, record.py (live signal outcomes vs STW over 21 days)
  analysis/dashboard.py   market cards + sparklines, sector table
  analysis/markets.py     /market/{slug} detail pages (MARKETS defs, returns, ranges, A$ price, correlated stocks, RBA decisions)
  portfolio/holdings.py   real holdings from hand-entered trades: FIFO parcels, CGT discount dates, dividends + franking,
                          concentration, Sell-signal CGT flags, STW mirror
  market_calendar.py  ASX holidays (rules verified 2014-2026 vs ^AXJO), is_trading_day, next/prev, trading_days_after
  news/ai.py       optional Claude summaries of price-sensitive announcements (anthropic SDK, structured output,
                   only summaries stored; scope followed|universe; daily cap; errors recorded, not retried)
  alerts.py        ntfy/webhook, quiet hours, dedupe (`alerts` table); triggers: signal change, PS announcement, job failure
tools/install-finbert.sh  one-off model build into data/models/finbert (throwaway torch container)
  sources/         adapters: base.py (HttpClient: rate limit, robots, cache, retries),
                   universe.py, yahoo.py, asx.py, appendix3y.py, asic.py, rba.py, sectors.py
  templates/       Jinja; `_*.html` = HTMX partials
  static/          app.css, app.js (whole-row company links), company.js (chart), market.js, vendor/ (lightweight-charts 5.3.0, htmx 2.0.11; no CDN)
tests/             pytest; fixtures/ are recorded real responses
data/              (bind mount, gitignored) asx.db, cache/, models/, secret.key
```

## Conventions
- Adapters = pure `parse_*` (tested with fixtures) + thin `fetch_*`. Raise `SourceError` / `NotFound`; callers record via `ctx.error()` and continue.
- Timestamps: `*_at` = UTC ISO strings; `date` = market-local YYYY-MM-DD. Display is DD/MM/YYYY (user request): templates convert automatically (`dmy`/`time` filters + `env.finalize`); a value that must stay ISO in HTML (date inputs) needs `|safe`. CSV/API stay ISO.
- Point-in-time: announcements use `published_at`; shorts `available_at` (= report date + 4 business days); annual financials `available_at` (= period end + 60 days); fundamentals snapshots by `as_of_date`.
- Prices are keyed by Yahoo symbol (`BHP.AX`, `^AXJO`, `AUDUSD=X`).
- Schema changes: append a new string to `MIGRATIONS` (never edit old ones).
- One uvicorn worker (scheduler is in-process). Jobs run in threads; SQLite in WAL mode.
- New jobs: add to `JOBS` in ingest.py (or later modules) and schedule in scheduler.py.

## Commands
```bash
docker compose up -d --build                                  # build + run
docker compose logs -f                                         # logs
docker compose run --rm --no-deps -v "$PWD:/app" asx-analyst pytest   # tests on working tree
curl -s localhost:8000/api/status | python3 -m json.tool       # job + freshness status
curl -X POST localhost:8000/api/jobs/<job>/run                 # daily_update|initial_backfill|announcements|shorts|universe|analysis|news_backfill|train|forecast|paper|maintenance
```

## Gotchas
- Fundamentals: always read through `market.latest_fundamentals()` (merges Yahoo + ASX, fixes units: D/E ratio, yields as fractions, P/E<=0 -> None + `loss_making`).
- `upsert()` takes its column list from the first row: pass rows with identical keys.
- Sentiment: identical texts share one `text_sentiment` row (key = sha1 of normalised text). Only one FinBERT instance at a time (`_sentiment_busy`); the backfill renices a dedicated thread, never a scheduler pool thread.
- RSS: never store full article text (summary ≤300 chars). News point-in-time = `available_at`.
- Don't start a second app instance (e.g. a test container) on the live `data/` dir: its `init_db()` marks the live container's running jobs as "interrupted" in job_runs (the jobs keep running and fix their row when they finish). Use `SCHEDULER_ENABLED=false` and a copy of the DB, or check against the live container.
- Models: forecasts store CALIBRATED q10/q90/p_up/p_beat (raw in *_raw); rank on p_beat_raw. Locked holdout from
  `train.HOLDOUT_START` (2024-01-01): judge any model change on development results only, never move the date.
- Models: never tune PARAMS on walk-forward results (data snooping). Any new feature must be point-in-time: extend
  tests/test_models.py::test_features_are_point_in_time (build with `end=` cutoff must equal the full build).
- Holding a write transaction while calling ctx.progress() deadlocks (progress writes from another connection).
- Paper trading: never store cash/positions; always `paper.replay()` from filled orders + dividends. Prices are
  split-adjusted to today, so fills convert to today's units via splits after the fill date.
- Price history: `update_symbol_prices` re-downloads a symbol's full history when Yahoo's adjustment of already-stored
  days changes (new dividend/split); otherwise total returns across ex-dates would be wrong.
- Scheduler catch-up: on start, a missed 16:10 daily_update runs after 2 min, a stale (>8 d) training after 5 min.
  So `docker compose up -d --build` after a missed slot triggers a daily update — expected.
- `dividends.currency` is the currency of `amount` (migration 9). Yahoo .AX amounts are AUD even for USD-declaring
  companies; only ASX-only rows (not yet on Yahoo) can be USD.
- Holdings sides: buy, sell, drp, return (capital return), demerger (other_code, ratio, cost_pct). Units per code over
  time come from `Book.unit_events` (used for dividends) - extend it for any new action type.
- tracked_codes() = universe + watchlist + holding_trades (code, other_code) + paper_orders; `new_codes` job fetches
  history for followed codes with no prices (started when you add one).
- AI tests mock `ai._client`, `asx.fetch_pdf`, `PdfReader`, `pdf_to_text` - never call the real API in tests.
- Alerts: `notify()` writes through its own connection; callers holding a write transaction must commit first
  (check_* functions do). Never let alert errors break a data job (step_alerts catches).
- Status freshness is memoised per (db path, job_runs state); dashboard/screener caches are warmed in a thread at start.
- UI check: Playwright image `mcr.microsoft.com/playwright:v1.64.0-noble` with `--network host` against :8000 (no browser on the host). For pages needing
  demo data, run a second container on a *copy* of the DB (`-p 127.0.0.1:8001:8000 -e SCHEDULER_ENABLED=false`,
  `-v copy:/app/data -v $PWD/app:/app/app:ro`), never on the live data dir.

## Known environment quirks
- Ad-blocking LAN DNS (Pi-hole/AdGuard style) can return 0.0.0.0 for fc.yahoo.com → compose sets `dns: 1.1.1.1, 8.8.8.8` for this container. Without it, Yahoo `.info`/calendar fail (prices still work).
- Plain curl to Yahoo gets HTTP 429; yfinance (curl_cffi impersonation) works.
- ASX Markit announcements JSON returns only the latest 5 → use the HTML pages (todayAnns.do, prevBusDayAnns.do, announcements.do?timeframe=Y&year=).
- Google News RSS is disallowed by robots.txt → not used.
- Root disk (Docker): ~67 GB free after a cleanup on 2026-11-13 (was ~18 GB). Still keep the image lean (no torch: FinBERT via ONNX). The build cache was pruned, so the next build re-downloads packages (a few minutes).

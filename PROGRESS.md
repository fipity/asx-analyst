# PROGRESS

## Current phase
Phase 5: Forecasting + walk-forward backtests. **Complete (2026-11-13). Waiting for user go-ahead for phase 6.**

## Done
- PLAN.md approved 2026-11-12 with recommendations D1–D5 (container DNS 1.1.1.1/8.8.8.8, bind 0.0.0.0:8000, Bing off, Anthropic Haiku optional, ASX100/300 approximations).
- Docker: Dockerfile (python:3.12-slim, non-root uid 1000), compose (restart unless-stopped, healthcheck, 2 CPU / 2 GB, log rotation 3×10 MB, TZ Sydney, ./data bind mount).
- SQLite schema (migration 1): companies, index_membership, prices, dividends, splits, fundamentals, financials_annual, calendar_events, announcements, director_trades, short_positions, asic_files, macro_series, job_runs, source_status, errors, backfill_progress, kv.
- Adapters (all verified live): universe (SPDR STW/SFY, iShares IOZ, ASX directory; ASX 50/100/200/300 all work), yahoo (prices, dividends, splits, info, calendar, annual financials), asx (announcements HTML today/prev/yearly, PDF link, key stats incl. franking), appendix3y (parser verified against ~30 real notices), asic (index JSON + daily CSV incl. old UTF-16 files), rba (A2 cash rate, F11.1 AUD/USD).
- Pipeline: resumable steps; jobs daily_update, initial_backfill (3 parallel host branches), announcements, shorts, universe. Scheduler: 16:10 Mon–Fri update, announcements every 30 min in market hours / hourly otherwise, ASIC 09:05, universe Sundays.
- Web: Home (overview), Status page (freshness per dataset, jobs, schedule, sources, errors), Refresh button, /api/status, /api/refresh, /api/jobs/{job}/run, optional password, disclaimer + delay note on every page.
- README.md (setup/update/backup), CLAUDE.md.
- 39 tests passing in container.
- Live: container healthy, reachable on port 8000. First backfill pass: 219 Yahoo symbols (1.18M price rows) with zero errors; fundamentals + franking confirmed (DNS fix works).

- Initial backfill complete: 1.18M stock price rows, 93k market-series rows, 157k announcements (10 yrs), 1.55M ASIC short rows (2,527 daily files), 1,000 director notices (318 buy / 72 sell / 574 other / 36 unparsed), 6.8k dividends, 941 annual financials, 478 calendar events, RBA cash rate + AUD/USD.
- Refresh button verified: POST /api/refresh -> daily_update ok (204 items, 0 errors, ~6 s incremental). Container ~135-195 MB RAM.
- Fixed: ASIC publishes 0-byte files on some holidays (2017-10-02) -> parsed as empty.

## Phase 2 (done 2026-11-12)
- `app/analysis/market.py`: screener rows (price metrics, merged Yahoo+ASX fundamentals, grossed-up yield at 30% tax, ASIC short % + 20-report change), filters/sorting, peers + sector medians, returns table (stock total/price vs sector index vs ASX 200), chart payload (events snapped to the next trading day). Memoised until the next job run finishes.
- Pages: `/screener` (HTMX live filters, sortable columns, clean URLs, filters fold away on phones), `/company/{code}` (key stats, lightweight-charts price chart with range buttons, line/candles, volume, toggles for dividends / price-sensitive announcements / director buys+sells, short % pane, hover/tap legend listing the day's events with links; returns, dividends, calendar, annual financials, peers, director trades, announcements with "price-sensitive only" + load more), top-bar search (`/go`), home page biggest rises/falls. JSON: `/api/screener`, `/api/company/{code}/chart`. GZip on.
- Vendored JS (no CDN): lightweight-charts 5.3.0 (Apache-2.0), htmx 2.0.11 (0BSD) in `app/static/vendor/`.
- Data fixes: Yahoo `dividendYield` is always percent (sub-1% yields were stored 100x too big) and -99999.99 P/E sentinels → migration 2 corrected stored rows (backup at `data/backups/asx-pre-phase2.db`). Dividend yield + franking now prefer ASX. Negative P/E shown as "loss" and excluded by "max P/E".
- 46 tests pass (new `tests/test_market.py` with synthetic data). Verified with headless Chromium (Playwright image): no JS errors, chart renders light/dark, no horizontal scroll at 390 px. Pages 15–50 ms warm; chart JSON ~144 KB gzipped for BHP (since 1988).

## Phase 3 (done 2026-11-13)
- `app/analysis/indicators.py` (pure pandas): SMA/EMA 20/50/200, Wilder RSI 14, MACD 12/26/9, Bollinger 20/2 (%B, width), volume z-score vs prior 60 days, distance from 52w high/low, momentum 1/3/6/12m + 12-1m, volatility 20/60d annualised; trend label (price vs 50d vs 200d); plain-English `describe()`. Level indicators use `close`, return-based ones `adj_close`. Look-ahead test: values at t are identical when later rows are appended.
- `app/analysis/scoring.py`: 16 metrics in 4 categories (valuation, quality, growth, dividend) → mid-rank percentiles within GICS sector, shrunk towards the universe percentile with K=5; category = mean of available metrics; composite needs ≥3 categories; sector rank. `gather(c, codes, as_of)` is point-in-time (fundamentals snapshot ≤ as_of, financials `available_at` ≤ as_of, dividends ex_date ≤ as_of).
- Migration 3: `technicals` (daily snapshot per code) and `scores` (per as_of_date, detail_json). History accrues from 2026-11-13.
- Pipeline: `step_analysis` runs at the end of daily_update and initial_backfill; new job `analysis` (`POST /api/jobs/analysis/run`, ~5 s).
- Screener: views (Overview, Scores, Valuation, Quality & growth, Dividends, Technical, Shorts), score/trend/RSI/volatility/12-1m columns, filters for min score, trend, RSI range, max volatility.
- Company page: score bars + "what's behind the scores" table (value + sector percentile per metric), technical picture (facts + key values), chart toggles for 20/50/200-day averages, Bollinger bands, RSI and MACD panes (chart grows per pane). `/api/company/{code}/indicators` (dates once + aligned arrays; BHP 38 yrs ≈ 250 KB gzipped).
- Status page: freshness rows for technicals and scores.
- 59 tests pass. Browser-checked (Playwright): no JS errors.

## Phase 4 (done 2026-11-13)
- RSS adapter `app/sources/rss.py`: ABC Business, Guardian AU Business, SMH Business, Motley Fool AU, RBA media releases (robots checked; HttpClient re-checks). Keeps headline, link, time, ≤300-char plain summary only (Fool's full `content:encoded` is ignored; test enforces it).
- `app/news/`: `text.py` (normalise, SimHash, dedupe: same title, ≥60% headline word overlap, or SimHash ≤3 bits; same-source only on identical titles), `matching.py` (explicit "ASX: XXX"/"XXX shares", full names, curated aliases, brand codes, ambiguous single words need capitalisation + market context; summary-only mentions ×0.8), `tagging.py` (12 event types incl. substantial holder; broker only for news; 12 macro themes → sectors), `sentiment.py` (FinBERT via onnxruntime + tokenizers), `queries.py`.
- FinBERT: `tools/install-finbert.sh` builds `data/models/finbert` once in a throwaway python container (torch 2.5.1 CPU, transformers 4.46.3) from the official safetensors at a pinned revision. Compared on 493 real headlines vs fp32 PyTorch: fp32 ONNX 100% agreement, int8 94%, per-channel int8 13% (broken) → **fp32 ONNX installed (438 MB)**, not int8 as planned. 58 headlines/s on 2 cores, ~0.75 GB RAM while loaded; freed after each run (malloc_trim). Licence CC-BY-NC-SA-4.0 (personal use).
- Migration 4: `news_items` (available_at = max(published, first_seen)), `news_links`, `text_sentiment` (keyed by text hash: 157k announcements = 52k distinct headlines), `item_tags`, `announcements.sent_key/tagged`.
- Jobs: the 30-min `announcements` poll now also fetches RSS, links, tags and scores new items (≤3000/run); `daily_update` tags + scores new announcements; `news_backfill` (one-off, resumable, nice 10, auto-scheduled 2 min after start until done) scored all 157k announcement headlines. Only one model in memory at a time.
- UI: `/news` (filters: search, company, scope, source, event, theme, sentiment, period; duplicate stories shown once with "also: …"), `/api/news`, company page "News & sentiment" (7/30-day news count + sentiment, 90-day price-sensitive sentiment, announcement activity vs usual, event counts, linked stories), event chips + sentiment on announcements, latest news on home, Status page model/backlog section + freshness rows.
- 105 tests pass (incl. a real-model test when the model is present).

## Phase 5 (done 2026-11-13)
- `app/models/features.py`: point-in-time panel (weekly samples from 2017-03, 92.7k rows, 17 s): 19 technical, 11 fundamental (financials_annual by available_at, USD converted via AUDUSD; NZD/CAD/EUR reporters NaN), 5 positioning (shorts by available_at; director trades NaN before 90 days of collection), 15 announcement/news (Sydney time, ≥16:00 → next day; counts, events, FinBERT tone), 11 market/macro, 3 cross-sectional ranks, sector. Targets: 5/21/63-day total return and excess vs STW (ASX 200 ETF incl. dividends). Baseline A = each stock's past 3-yr distribution. Eligibility: price ≥ 20c, ≥ A$1M/day turnover, traded in last 5 days.
- `app/models/train.py`: LightGBM native API (no sklearn), fixed conservative params chosen a priori (not tuned on results). Quantiles q10/q50/q90 + P(up) for each horizon, + P(beat STW) + median excess for 21d; with/without news variants. Purged expanding walk-forward (train row only if pos(d)+h ≤ pos(R)); 21d retrained monthly (87 rebalances from 2020-01), 5d/63d quarterly. Evaluation: AUC, Brier, rank IC (+t), 80% range coverage, pinball vs baseline A, calibration deciles.
- `app/models/backtest.py`: top-10 monthly, next-day open, 10 bps slippage, A$5 brokerage, whole shares, adjusted prices, cash earns 0. Benchmarks STW buy-and-hold and ^AXJO; baselines momentum and historical odds; **survivorship controls**: 20 random top-10 portfolios from the same list, and all strategies re-run on the top-100-by-turnover *at the time*.
- `app/models/signals.py`: Buy/Hold/Sell = backtest rule (top/bottom-N by P(beat), P vs 0.5, cost hurdle 0.4%); confidence capped to "low" when walk-forward AUC < 0.52; SHAP explanations (pred_contrib) in plain English.
- Migration 6: `forecasts`, `backtest_runs`. Jobs `train` (Sat 02:00 + first start, ~20 min, nice 10, peak ~500 MB) and `forecast` (end of daily_update). Pages `/signals`, `/backtest`, company Forecast section; `/api/signals`, `/api/backtest/latest`.
- **First results (2026-11-13):** out-of-sample AUC(beat) 0.50, rank IC ~0.01 (t ≈ 1), 80% ranges cover 74–78%, calibration flat (predicted 44–58% → realised ~50% everywhere). Headline CAGR 2020–2026: model 26.4% / 22.3% (no news), momentum 30.3%, random-10 median 11.4%, STW 8.3%. **Among stocks large at the time: model 2.7%, momentum 7.8%, random median 5.6–5.9%, STW 8.3% → the headline edge is survivorship bias.** The Backtest page says so in a red banner; all signals are "low confidence" and shown greyed out.
- 119 tests pass (look-ahead guard, after-close timing, purge, engine known answers, metrics, signal rules, verdict logic, end-to-end train job + pages).

## Phase 6 (done 2026-11-13)
- **Dashboard** (`/`): market cards (ASX 200, All Ords, AUD/USD, iron ore, gold, Brent; 1d/1m/YTD + 3-month SVG sparklines), RBA cash rate, breadth, sector table (1d/1m/YTD/1y), strongest Buy/Sell (greyed when no skill), movers, market news, price-sensitive announcements, watchlist + paper snapshots, health dot. Caches warmed at start (first view ~50 ms instead of 6 s).
- **Watchlist** (migration 7 `watchlist`): `/watchlist` (price, 1d/1m/1y, signal, P(beat), 7-day news count + tone, next event, latest price-sensitive announcement, combined announcements + news feed), HTMX ☆ Watch toggle on company pages, `/api/watchlist`.
- **Paper trading** (`app/portfolio/paper.py`, tables `paper_portfolios`, `paper_orders`): market orders by shares or A$ amount; fill at the open of the first trading day ≥ exec_after (before 10:00 on a weekday = that day, else next weekday); slippage + brokerage, whole shares, buys cut to cash, A$500 minimum for amount buys; sells before buys; dividends on ex-date (USD converted via AUDUSD); split-aware. State is always replayed from fills. Equity curve vs STW bought the same day with the same costs. `/paper`, `/api/paper/{id}`. Job `paper` (end of daily_update).
- **Model forward test**: "Model signals (top 10, forward test)" portfolio auto-created, follows the backtest rule (top-N by P(beat), rebalance every 20 trading days, no top-ups). First 10 buys placed on forecasts of 2026-11-12, filling at the 2026-11-16 open.
- **Live signal record** (`app/portfolio/record.py`): each new signal (episode start) vs STW over the next 21 trading days; hit rate and average excess per Buy/Sell on Signals page + company page (with backtest hit rate and drawdown of the rule next to them); company "Signal history" table.
- **Scheduler**: weekly `maintenance` (Sun 03:30: gzipped online backup to `data/backups/asx-auto-*.db.gz`, keep `BACKUP_KEEP`=4, prune errors >90 d and job_runs >180 d, PRAGMA optimize); catch-up on start (missed 16:10 update → after 2 min; training older than 8 days → after 5 min); `JOB_INFO` descriptions.
- **Status**: health summary (ok/warn/bad with reasons), freshness with expected max age + stale flag (memoised: 0.8 s → 60 ms), merged job table (what, when, last run, duration, last success, next run, Run button), storage per part of ./data, free disk, container memory (cgroup), backups.
- **Fix:** incremental price updates kept the old dividend/split adjustment of stored days, so total returns across a new ex-date missed the dividend. Now a symbol's full history is re-downloaded when Yahoo's adjustment of already-stored days changes.
- 132 tests pass (engine fills/costs/dividends/splits, amount buys, signal-portfolio rebalancing, signal record, catch-up rules, backup rotation, re-adjust refetch, pages). Browser-checked with Playwright on a DB copy (desktop, phone, dark): no JS errors, no horizontal scroll.

## Phase 7 (done 2026-11-13)
- **Holdings** (`app/portfolio/holdings.py`, migration 8 `holding_trades`): hand-entered buys/sells validated on entry (no overselling at any date, no future dates; deleting a buy that later sells need is refused). FIFO parcels in today's share units (split-aware); realised gains per parcel with discount flag; CGT discount from buy date + 1 year + 1 day; dividends on shares held at the close before each ex-date, franking credits at 30% (latest ASX franking used and marked "est." when the dividend's own is unknown); value/P&L, concentration by stock (>20%) and sector (>40%) warnings; Sell-signal banner on held stocks saying whether selling now misses the discount and on how much gain; "same money in the ASX 200" mirror (each buy buys STW, each sell sells the same fraction). `/holdings`, `/api/holdings`.
- **Alerts** (`app/alerts.py`, table `alerts`): ntfy (JSON publish, optional token, click link via APP_URL) and generic webhook; quiet hours queue (flushed every 15 min, dropped after 2 days); dedupe keys; triggers: signal change on watchlist/held stock (end of daily_update; CGT note on held Sells), price-sensitive announcement for them (every announcements poll; starts from first run, no history replay), job failure via `JOB_FAILURE_HOOKS` (≤1 per job per 6 h). `/alerts` page with config summary, followed stocks, test button, history. Settings ALERT_SIGNALS/ANNOUNCEMENTS/JOBS, APP_URL.
- **CSV export**: screener (current filters), signals history with outcomes, paper orders, holdings positions+parcels, holdings trades, realised gains, dividends.
- **Fix (migration 9):** Yahoo's dividend amounts for .AX listings are already in AUD (BHP's US$0.60 FY25 final = A$0.9193), but the ASX "declared in USD" had been copied onto them and paper trading converted them again (+~50%). `dividends.currency` now means the currency of `amount`; Yahoo rows are AUD. Company page note corrected.
- 143 tests pass. Browser-checked holdings/alerts/screener on a DB copy with demo trades (desktop + phone dark): no JS errors, no horizontal scroll.

## Extras (2026-11-13, after phase 7)
- **AI summaries** (`app/news/ai.py`, migration 10 `ai_summaries`): anthropic 2.8.0, `messages.parse` with a Pydantic schema (summary, ≤4 key points, events from EVENT_LABELS, tone). PDF via the existing ASX fetch (cached), first 15 pages / 60k chars; only the summary is stored. Scope followed (default) or universe; LLM_DAILY_LIMIT=40/24 h; auth/rate-limit/connection errors stop the run and retry later, other failures (incl. refusals) are recorded and not retried. Runs in the announcements poll and daily update, before alerts (alerts include the summary). Shown on company announcements and watchlist; Status shows 30-day usage + estimated cost. Off until LLM_API_KEY is set; default model claude-haiku-5-5 (plan decision D4).
- **Corporate actions** in holdings: DRP, capital return (cost-base reduction; excess = capital gain with discount by parcel age), demerger (ratio + cost %; child parcels keep acquisition dates; STW mirror split too). Dividends now follow units through all actions (`Book.unit_events`).
- **Fix:** `tracked_codes()` looked for tables that never existed (`holdings`, `paper_positions`), so held/paper stocks outside the index never got prices. Now uses holding_trades (+ demerged codes) and paper_orders; new `new_codes` job fetches history immediately when one is added.
- **ASX holidays** (`app/market_calendar.py`): rule set matches every closed weekday 2014–2026 in ^AXJO history (incl. Anzac Day never moved, 2022-09-22 one-off); 3 Yahoo data gaps identified (2014-02-17, 2017-09-04, 2019-10-03). Scheduler skips daily_update on holidays, catch-up uses trading days, announcement polling treats holidays as off-market, paper fills roll past holidays, freshness for market data counts trading days, Status/dashboard show closures.
- Housekeeping: deleted data/backups/asx-pre-phase2.db (378 MB). Git history rewritten locally so the full Motley Fool article text (tests/fixtures/rss_fool_au.xml in the Phase 4 commit) no longer appears anywhere; final tree identical. **The force-push to GitHub was blocked by the permission system: the user must run it** (see Next step). Bundle of the old history: scratchpad `asx-analyst-before-rewrite.bundle` (temporary).
- 151 tests pass.

## Forecast reliability (2026-11-13)
- `app/models/calibrate.py`: isotonic calibration of P(up)/P(beat) (20 quantile bins + PAV) and scaled split-conformal adjustment of the 10–90% ranges (each tail to 10%). Fitted walk-forward (each block only on earlier predictions with known outcomes, ≥2000 rows) for evaluation, and on all known outcomes for today's forecasts (saved in meta.json `calibrators`).
- Forecasts table (migration 11): q10/q90/p_up/p_beat now hold calibrated values; raw in q10_raw/q90_raw/p_up_raw/p_beat_raw, `calibrated` flag. Ranking (and so paper/backtest picks) uses the raw score; Buy/Sell thresholds and confidence use the calibrated P.
- **Locked holdout** `train.HOLDOUT_START = 2024-01-01`: variant chosen on development AUC only; skill gate (meta/summary `auc_beat`) = holdout AUC. Backtest page: development vs holdout table (AUC, IC, Brier raw→calibrated vs base rate, range coverage raw→calibrated), holdout calibration table, holdout-only strategy results, model fingerprint + count of designs scored on the holdout. Caveat recorded: whole-period results were seen once before the split.
- 155 tests pass.

## Full-screen price chart (2026-11-13)
- Company page: "⛶ Full screen" button (or press F) puts the controls, chart and legend in full screen (Fullscreen API on `#chart-wrap`; fixed-overlay fallback where element fullscreen isn't supported, e.g. iPhone Safari). Esc or the button exits. The visible bars and the 110 px lower panes are kept after the resize. On narrow screens the overlay/indicator checkboxes go behind an "Options" button while in full screen.
- Browser-checked desktop (light) + iPhone 13 (dark): no JS errors, no horizontal scroll. Tests pass.
- Fix after the user reported it didn't work: static files had no cache-busting, so browsers kept the old company.js (button shown, no handler). Templates now use `static_url()` (adds `?v=<mtime>`). If the browser refuses native full screen, the overlay is used instead of failing silently.
- Fix: hovering days with events made the full-screen chart flicker (legend grew → chart shrank under the cursor → different day → legend shrank). Legend now has a fixed height in full screen (scrolls if long). Verified by sweeping the mouse across the chart: chart height constant.

## Daily / weekly / monthly bars (2026-11-14)
- Company chart: Daily / Weekly / Monthly switch. `/api/company/{code}/chart?interval=D|W|M` and `/indicators?interval=…` (400 otherwise). Bars built server-side from daily prices (`market.aggregate_payload`, `indicators.resample`): ISO week / calendar month, dated by the period's first trading day; last bar = period to date. Events move to their period's bar; short % = last reading in the period (periods without a price bar dropped). Indicators are computed on the aggregated bars, so labels switch to "50-week avg" etc.; legend shows "Week of …" / "Nov 2026".
- Switching keeps the selected range (5Y stays 5 years). Old-interval series are removed before new data is set (they stretched the time scale otherwise).
- 157 tests pass; browser-checked desktop (incl. full screen) + iPhone 13 dark, no JS errors.

## DD/MM/YYYY dates (2026-11-14)
- All displayed dates are DD/MM/YYYY (user request): `dmy` filter (also YYYY-MM -> MM/YYYY), `time` filter = DD/MM/YYYY HH:MM, and a Jinja `finalize` hook converting ISO dates inside any printed text (job summaries, notes) except Markup and URL-like contexts. Chart axis/crosshair (`localization.dateFormat`) and legend too. Kept ISO: `<input type=date>` (uses `|safe`), CSV exports, API.
- Verified: every page's visible text scanned in Playwright has no YYYY-MM-DD left. 158 tests pass.

## Clickable rows + market detail pages (2026-11-14)
- `static/app.js` (every page): a table row with exactly one `/company/X` link opens it when clicked anywhere (event delegation, HTMX-safe; buttons/inputs/other links keep their own action; Ctrl/Cmd/middle click = new tab; text selection doesn't navigate). Rows with an external link (news, announcements) or several companies are left alone.
- Dashboard market cards link to `/market/{slug}` (asx200, allords, audusd, iron-ore, gold, brent, cash-rate; `app/analysis/markets.py`, `market.html`, `market.js`, `/api/market/{slug}/series`). Page: chart with ranges, returns 1 day–10 years (3y+ annualised), 52-week range, record high/low, plain-English "what it is and why it matters", A$ price for commodities, ASX stocks most/least correlated (2 years of weekly returns, |corr| ≥ 0.2 only, with sensitivity), RBA decision history for the cash rate (step line, filled per business day so the time axis is even; changes measured to today).
- 160 tests pass. Browser-checked: card → page, row clicks on screener/signals/dashboard/watchlist/market tables, Ctrl-click new tab, news rows not clickable, all 7 pages desktop + phone dark, no JS errors or horizontal scroll.

## Header refresh button + data bar (2026-11-14)
- Every page: "↻ Refresh data" button in the header (starts `daily_update` via POST /api/refresh, no page change; no-JS fallback POST /refresh redirects back) and a data bar under it: "Data updated DD/MM/YYYY HH:MM (age) · prices to close DD/MM/YYYY", warning link if the last run failed. While running: current step ("prices 40/200") + progress line (per-step %, or indeterminate), polled every 3 s; when done: "Refresh finished … Reload to see the new data". Idle re-check every 60 s (skips hidden tabs). `status.data_state()`, GET /api/refresh.
- 161 tests pass. Browser-checked on the live app with two real refreshes (~3–4 min each, both ok), desktop + phone dark.

## In progress
- Nothing.

## Next step
Waiting for the user. Pending on their side: optionally set LLM_API_KEY and an alert channel. (The rewritten history is on GitHub; normal pushes work again.)

## Open decisions / known issues
- ASX 100 = top 100 of ASX 200 by weight; ASX 300 = ASX 200 + next largest by market cap (labelled approximations).
- Delisted companies have no Yahoo data → backtests limited to current constituents (must be stated in UI, phase 5). Index membership snapshots are stored from now on.
- ~~Yahoo dividend amounts for USD-reporting companies are in USD~~ — wrong: they're AUD (fixed in migration 9, phase 7).
- ASX key-statistics yields can include special dividends (e.g. HLI ~25%). Yahoo yields differ by >1 pp for ~13 stocks; ASX is used.
- Scores use today's fundamentals snapshot; historical scores before 2026-11-13 don't exist. Phase 5 must rebuild point-in-time fundamentals from `financials_annual` (+ prices) for backtests.
- Materials (gold miners) dominate the top composite scores; that's within-sector ranking + strong reported numbers, not a bug.
- FinBERT on ASX headlines is noisy (e.g. "Net loss widened…" → positive; "BHP lifts dividend after record profit" → negative). Shown as a noisy signal; phase 5 will measure whether it adds anything.
- RSS history starts 2026-11-13; historical news features must come from announcements.
- Plotly never vendored: lightweight-charts draws the backtest curves (saves ~3.5 MB). Deviation from PLAN.md.
- Automatic backups live on the same disk as the DB (data/backups); README tells the user to copy them off-box. The old manual `data/backups/asx-pre-phase2.db` (378 MB) is left alone.
- Holdings franking credits assume a 30% company tax rate (base-rate entities pay 25%). Alerts need a channel in .env; until then they're logged as "skipped".
- Never change the model (params, features, variant rule, calibration) because of holdout numbers; judge changes on development results. A new design changes `T.fingerprint()`; the Backtest page counts designs scored on the holdout.
- Forecast models have no demonstrated skill (see Phase 5 results). Don't present signals as actionable. (only needed for forecast fans/backtests in phase 5).
- Director trades: "buy"/"sell" only for discretionary on-market trades; plan/DRP/rights = "other"; unreadable = "unparsed" (still linked).

# ASX Analyst — Plan (awaiting approval)

Educational tool, not financial advice. Personal, non-commercial, LAN-only use.

## 1. Target environment

- A small always-on Linux box with Docker Compose; CPU only (no GPU), ~8 GB RAM.
- One container on port 8000, LAN only. Other services on the host are never touched.

**Network gotcha:** an ad-blocking LAN DNS (Pi-hole/AdGuard style) may resolve `fc.yahoo.com` to `0.0.0.0`. That breaks Yahoo's cookie/crumb flow: price history still works, but fundamentals and the earnings calendar fail. Setting `dns: [1.1.1.1, 8.8.8.8]` on **this app's container only** fixes it. See decision D1.

## 2. Data sources (each tested live from this server today)

All sit behind adapters (`app/sources/<name>.py`, common interface, per-host rate limiter, HTTP cache, `fetched_at` on every row).

| Need | Primary source | Status | Fallback / notes |
|---|---|---|---|
| Universe ASX 200 | SPDR **STW** daily holdings xlsx (ssga.com) | ✅ 200 rows, as of 11-Nov-2026 | iShares **IOZ** holdings CSV ✅ |
| Universe ASX 50 | SPDR **SFY** holdings xlsx | ✅ | top 50 of STW by weight |
| Universe ASX 100 | No free official list found | ⚠️ | top 100 of the ASX 200 by index weight (labelled as an approximation in UI) |
| Universe ASX 300 | Vanguard VAS holdings (page loads; data endpoint TBD in phase 1) | ⚠️ | top 300 by market cap from the ASX company directory file (approximation, labelled) |
| Company list, sector, mkt cap, listing date | ASX `ASXListedCompanies.csv` + Markit directory file (1,833 rows) | ✅ | |
| Daily OHLCV, dividends, splits (10+ yrs) | Yahoo Finance via **yfinance 1.8** (`.AX`) | ✅ BHP back to 1988, 80 dividends, 4 splits | plain curl gets 429; yfinance's browser impersonation works. Rate limit ~1 req/s, nightly incremental only |
| Fundamentals (P/E, EPS, revenue, debt, margins, ROE, yield) | yfinance `.info` + annual statements | ✅ (needs D1) | ASX Markit `key-statistics` ✅ |
| **Franking %**, ex/pay/record dates, dividend amount | ASX Markit `key-statistics` | ✅ (e.g. BHP 100% franked, ex 2026-09-03) | Yahoo has no franking data |
| Earnings dates | yfinance `calendar` / `get_earnings_dates` | ✅ (needs D1) | often sparse for ASX; shown as "unknown" if missing |
| Price-sensitive announcements | ASX Markit `companies/{code}/announcements` (JSON, `isPriceSensitive`) | ✅ but only the latest 5 | ASX `announcements.do?...timeframe=Y&year=YYYY` HTML: full yearly history back to ~2010 with the price-sensitive flag ✅ (backfill + polling) |
| Director trades | Appendix 3Y = "Change of Director's Interest Notice" announcements; PDF via Markit file endpoint ✅ | ✅ | parsed with pypdf (best effort: acquired/disposed qty + value; unparsed ones are still listed with a link) |
| Short positions | ASIC daily aggregated CSV: `download.asic.gov.au/short-selling/RRYYYYMMDD-VVV-SSDailyAggShortPos.csv`, indexed by `short-selling-data.json` (4,150 days back to 2010) | ✅ | T+3 publication lag, respected in backtests |
| ASX 200 & sector indices | yfinance `^AXJO`, `^AXMJ`, `^AXFJ`, … | ✅ | |
| AUD/USD | yfinance `AUDUSD=X` | ✅ | RBA F11.1 CSV ✅ |
| RBA cash rate | RBA F1.1 CSV | ✅ | |
| Iron ore / gold / oil | yfinance `TIO=F` (SGX 62% Fe), `GC=F`, `BZ=F`, `CL=F` | ✅ | RBA I2 commodity index |

**News (headline, source, link, time, short summary only; no full text):**

| Source | robots.txt | Use |
|---|---|---|
| ASX announcements (above) | allowed | main per-company feed; price-sensitive flag |
| ABC News Business RSS | allowed | market news |
| RBA media releases RSS | allowed | macro: rates |
| The Guardian business RSS | allowed | macro/company |
| SMH business RSS | allowed | headlines + teaser only (the RSS feed itself is free) |
| Motley Fool AU RSS | allowed | per-company mentions |
| ~~Google News RSS search~~ | **DISALLOWED** (`Disallow: /` for all agents; only `/topics/`, `/home` etc. allowed) | **Excluded**, per your robots.txt rule |
| Bing News RSS search | robots allows it, but Microsoft's terms bar automated use of search results | adapter written, **off by default** (D3) |
| AFR | paywalled | excluded |

Terms of use: Yahoo, ASX and the news sites all permit personal, non-commercial viewing. This app stays private on your LAN, keeps no full article text, and polls politely with an identifying User-Agent, rate limits and caching.

## 3. Architecture

```
docker compose (1 service: asx-analyst, port 8000)
└─ python:3.12-slim, non-root uid 1000, TZ=Australia/Sydney
   ├─ FastAPI + Jinja2 templates (server-rendered, mobile-first CSS, HTMX for partial refreshes)
   │   ├─ lightweight-charts (price + indicators), Plotly basic (forecast fans, backtest curves)
   │   └─ JS vendored locally, no CDN
   ├─ APScheduler (in-process, SQLite job store)
   │   ├─ 16:10 daily: prices, fundamentals, dividends → features → signals → alerts
   │   ├─ news: every 30 min 10:00–16:00, hourly otherwise
   │   ├─ ASIC shorts daily 09:00; universe refresh weekly
   │   └─ retrain + backtests Sat 02:00 (outside market hours)
   ├─ app/sources/*   adapters (yfinance, asx, asic, rba, rss…), each with tests + recorded fixtures
   ├─ app/news/*      dedupe (normalised title + simhash), matching (ticker/name/alias dict), FinBERT, event rules
   ├─ app/analysis/*  indicators, fundamental peer ranks, feature builder (point-in-time)
   ├─ app/models/*    baselines, LightGBM, walk-forward backtester, signal combiner
   └─ SQLite (WAL) in ./data/asx.db; models in ./data/models; HTTP cache in ./data/cache
```

- **Point-in-time discipline:** every row stores `published_at` (source time) and `fetched_at`. The feature builder takes an `as_of` timestamp and only reads rows with `available_at <= as_of`. Lags applied: ASIC shorts T+3; financial statements 60 days after period end; announcements at their ASX timestamp; RSS items at max(published, first seen).
- **Optional password:** `APP_PASSWORD` in .env (empty = off) → signed session cookie login.
- **Alerts:** ntfy + generic webhook, quiet hours, test button, deduped. Triggers: signal change on held/watchlist stock, price-sensitive announcement, job failure.
- **Logging:** stdout JSON-ish lines; compose `json-file` driver, max-size 10m × 3.
- **Never** connects to a broker or places trades.

## 4. Models

1. **Technical features:** SMA/EMA 20/50/200, RSI 14, MACD, Bollinger, volume z-score, distance from 52-week high/low, 1/3/6/12-month momentum, 20/60-day volatility.
2. **Fundamental scores:** valuation (P/E, P/B, FCF yield), quality (ROE, margins, debt/equity), growth (revenue/EPS growth), dividend (grossed-up yield incl. franking), as percentile ranks within GICS sector.
3. **News features:** FinBERT sentiment rolled over 1/7/30 days, volume spike z-score, event flags (earnings, guidance, dividend, raising, takeover, management, legal/regulatory, broker rating), plus macro tags mapped to sectors (e.g. iron ore/China → Materials, rates → Financials/REITs).
4. **Positioning/calendar:** short % and 20-day change, net director buying over 90 days, days to ex-dividend, days to earnings.
5. **Forecast (5 / 21 / 63 trading days):**
   - Baseline A: historical empirical return distribution per stock (drift + vol).
   - Baseline B: momentum-only rule.
   - Main: LightGBM **quantile regression** (p10/p50/p90) + LightGBM classifier P(return > 0), with and without news features.
   - Output is always a range plus a probability, never a price target.
6. **Signal:** Buy/Hold/Sell from P(up, 1-month) and the expected excess return vs the ASX 200 net of costs, with confidence bands. The explanation lists the top SHAP contributions in plain English, with links to the articles behind the news terms.

**FinBERT:** ProsusAI/finbert exported to **ONNX int8** (~110 MB) and run with onnxruntime. No PyTorch in the runtime image (saves ~1.5 GB of image and ~1 GB of RAM). It is downloaded once into `./data/models` on first start. Optional LLM summaries/event extraction via `LLM_API_KEY` (off by default).

## 5. Backtesting and honesty

- Walk-forward: expanding window, retrained monthly, with a purge gap equal to the forecast horizon. Trades happen at the next day's open after the signal, with brokerage `BROKERAGE_PER_TRADE=5` (AUD) plus a configurable slippage in bps. Paper capital is configurable (default $50k) so that the $5 fee has a realistic weight.
- Benchmark: buy-and-hold `^AXJO` price index, plus the STW ETF with dividends as a total-return proxy. A red banner shows when a strategy fails to beat it.
- Shown for every strategy: CAGR, Sharpe, max drawdown, hit rate, turnover, and a with-news vs without-news comparison.
- **Known limitations, stated in the UI:**
  - **Survivorship bias:** no free historical index membership source exists, and Yahoo has no data for delisted tickers (tested: AWC.AX returns nothing). Backtests therefore use *current constituents*, which flatters results. Membership snapshots are stored daily from now on to reduce this over time.
  - RSS news only exists from the day collection starts. Historical news features come from ASX announcements (back to ~2010), so the news-feature backtest is announcement-driven.
  - Fundamentals history is limited to about 4 years of annual statements from Yahoo; earlier periods run without fundamental features.
  - Free price data is delayed by 20+ minutes.

## 6. Resource use (estimates)

| | Estimate | Limit set |
|---|---|---|
| Image | ~800 MB (slim + pandas/lightgbm/onnxruntime, no torch) | — |
| RAM | ~300 MB idle, ~1.2 GB peak during retrain/FinBERT batch | `mem_limit: 2g` |
| CPU | near 0 idle; 2 cores for ~10–30 min during weekly retrain | `cpus: 2.0`; training runs at `nice` 10 |
| Disk (./data) | ~1–2 GB after backfill (ASX 200 × 10+ yrs prices, 16 yrs ASIC shorts, announcements), growing ~100 MB/yr | on /home |
| Network | initial backfill ~1–2 h (rate-limited); daily ~10 min | |

The initial backfill runs as a background job with progress shown on the Status page. The app is usable while it runs.

## 7. Phases (commit + PROGRESS.md after each task; I stop after each phase)

1. Docker, adapters, DB schema, backfill, Refresh button, minimal Status page
2. Screener + company pages
3. Indicators + fundamental scoring
4. News ingestion, matching, FinBERT, events
5. Forecasting + walk-forward backtests
6. Dashboard, watchlist, paper portfolio, full scheduler, Status page
7. Holdings (P&L, dividends, concentration, CGT 12-month flag), alerts, CSV export

Tests (pytest, run inside the container): adapters against recorded fixtures plus outage/delisted cases, news matching/dedupe, and the backtest engine (synthetic data with known answers, plus a look-ahead guard test).

## 8. Open decisions (please answer with your approval)

- **D1 — DNS:** set `dns: [1.1.1.1, 8.8.8.8]` on this one container (recommended), or would you rather whitelist `fc.yahoo.com` in the LAN DNS?
- **D2 — Bind address:** publish on `0.0.0.0:8000` (recommended; survives a DHCP IP change), or bind strictly to the server's LAN IP?
- **D3 — Bing News RSS:** keep it off by default (recommended) or enable it?
- **D4 — Optional LLM:** default provider Anthropic (Claude Haiku) when `LLM_API_KEY` is set; it stays off unless you add a key. OK?
- **D5 — ASX 100/300:** accept the labelled approximations above if no free official list turns up in phase 1?

"""SQLite storage. Plain sqlite3 + numbered migrations (PRAGMA user_version).

Conventions:
- *_at columns: ISO-8601 UTC timestamps ("2026-11-12T05:10:00+00:00").
- date columns: YYYY-MM-DD in market (Sydney) time.
- Every fetched row has fetched_at. Rows usable by backtests also have an
  availability time (published_at / available_at) so features can be built
  point-in-time.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from app.config import get_settings

_init_lock = threading.Lock()


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


MIGRATIONS: list[str] = [
    # 1: phase 1 schema
    """
    CREATE TABLE companies(
        code TEXT PRIMARY KEY,
        name TEXT,
        industry_group TEXT,
        sector TEXT,
        listing_date TEXT,
        market_cap REAL,
        status TEXT NOT NULL DEFAULT 'listed',   -- listed | delisted | no_data
        yahoo_symbol TEXT,
        first_seen TEXT,
        updated_at TEXT,
        fetched_at TEXT
    );
    CREATE TABLE index_membership(
        index_code TEXT NOT NULL,
        code TEXT NOT NULL,
        as_of TEXT NOT NULL,
        weight REAL,
        source TEXT,
        approximate INTEGER NOT NULL DEFAULT 0,
        fetched_at TEXT,
        PRIMARY KEY(index_code, as_of, code)
    );
    CREATE TABLE prices(
        symbol TEXT NOT NULL,
        date TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL, adj_close REAL, volume REAL,
        fetched_at TEXT,
        PRIMARY KEY(symbol, date)
    ) WITHOUT ROWID;
    CREATE TABLE dividends(
        code TEXT NOT NULL,
        ex_date TEXT NOT NULL,
        amount REAL,
        currency TEXT,
        franking_pct REAL,
        pay_date TEXT,
        record_date TEXT,
        source TEXT,
        fetched_at TEXT,
        PRIMARY KEY(code, ex_date)
    );
    CREATE TABLE splits(
        code TEXT NOT NULL, date TEXT NOT NULL, ratio REAL, fetched_at TEXT,
        PRIMARY KEY(code, date)
    );
    CREATE TABLE fundamentals(
        code TEXT NOT NULL,
        as_of_date TEXT NOT NULL,
        source TEXT NOT NULL,
        market_cap REAL, pe REAL, forward_pe REAL, eps REAL, pb REAL,
        revenue REAL, net_income REAL, total_debt REAL, total_cash REAL,
        debt_to_equity REAL, profit_margin REAL, operating_margin REAL, roe REAL,
        revenue_growth REAL, earnings_growth REAL, dividend_yield REAL,
        payout_ratio REAL, franking_pct REAL, fcf_yield REAL, beta REAL,
        shares_outstanding REAL, week52_high REAL, week52_low REAL,
        currency TEXT, sector TEXT, industry TEXT,
        raw_json TEXT,
        fetched_at TEXT,
        PRIMARY KEY(code, as_of_date, source)
    );
    CREATE TABLE financials_annual(
        code TEXT NOT NULL,
        period_end TEXT NOT NULL,
        source TEXT NOT NULL,
        revenue REAL, net_income REAL, ebit REAL, total_debt REAL,
        total_equity REAL, operating_cash_flow REAL, free_cash_flow REAL,
        diluted_eps REAL, shares REAL,
        available_at TEXT,          -- conservative: period_end + 60 days
        fetched_at TEXT,
        PRIMARY KEY(code, period_end, source)
    );
    CREATE TABLE calendar_events(
        code TEXT NOT NULL,
        event_type TEXT NOT NULL,   -- earnings | ex_dividend | dividend_pay
        event_date TEXT NOT NULL,
        detail TEXT,
        source TEXT,
        fetched_at TEXT,
        PRIMARY KEY(code, event_type, event_date)
    );
    CREATE TABLE announcements(
        ids_id TEXT PRIMARY KEY,
        code TEXT NOT NULL,
        published_at TEXT NOT NULL,
        headline TEXT NOT NULL,
        price_sensitive INTEGER NOT NULL DEFAULT 0,
        pages INTEGER,
        url TEXT,
        pdf_url TEXT,
        source TEXT,
        fetched_at TEXT
    );
    CREATE INDEX ix_ann_code_time ON announcements(code, published_at);
    CREATE INDEX ix_ann_time ON announcements(published_at);
    CREATE TABLE director_trades(
        ids_id TEXT PRIMARY KEY,
        code TEXT NOT NULL,
        published_at TEXT,
        director TEXT,
        change_date TEXT,
        security_class TEXT,
        acquired REAL,
        disposed REAL,
        value REAL,
        nature TEXT,
        trade_type TEXT,            -- buy | sell | other | unparsed
        on_market INTEGER,
        parse_ok INTEGER NOT NULL DEFAULT 0,
        fetched_at TEXT
    );
    CREATE INDEX ix_dt_code ON director_trades(code, published_at);
    CREATE TABLE short_positions(
        code TEXT NOT NULL,
        date TEXT NOT NULL,
        short_qty REAL,
        issued_qty REAL,
        short_pct REAL,
        available_at TEXT,          -- when ASIC published it (report date + lag)
        fetched_at TEXT,
        PRIMARY KEY(code, date)
    ) WITHOUT ROWID;
    CREATE TABLE asic_files(
        date TEXT PRIMARY KEY, version TEXT, status TEXT, rows INTEGER, fetched_at TEXT
    );
    CREATE TABLE macro_series(
        series TEXT NOT NULL, date TEXT NOT NULL, value REAL, source TEXT, fetched_at TEXT,
        PRIMARY KEY(series, date)
    );
    CREATE TABLE job_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,       -- running | ok | partial | error | interrupted
        items INTEGER DEFAULT 0,
        errors INTEGER DEFAULT 0,
        message TEXT
    );
    CREATE INDEX ix_job_runs ON job_runs(job, started_at);
    CREATE TABLE source_status(
        source TEXT PRIMARY KEY,
        last_success TEXT, last_attempt TEXT,
        last_error TEXT, last_error_at TEXT,
        ok_count INTEGER DEFAULT 0, error_count INTEGER DEFAULT 0
    );
    CREATE TABLE errors(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL, job TEXT, source TEXT, item TEXT, message TEXT
    );
    CREATE TABLE backfill_progress(
        task TEXT NOT NULL, key TEXT NOT NULL, done_at TEXT, status TEXT,
        PRIMARY KEY(task, key)
    );
    CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT);
    """,
    # 2: Yahoo yields under 1% were stored in percent (0.85 instead of 0.0085). Where an ASX
    # yield exists for the same snapshot, keep whichever reading is closer to it; without
    # one, a stored yield of 10-100% is taken to be an undivided percent. Also drop
    # Yahoo's -99999.99 "not meaningful" sentinels.
    """
    UPDATE fundamentals SET dividend_yield = dividend_yield / 100
    WHERE source='yahoo' AND dividend_yield > 0 AND dividend_yield <= 1 AND (
        EXISTS (SELECT 1 FROM fundamentals a WHERE a.code=fundamentals.code AND a.source='asx'
                AND a.as_of_date=fundamentals.as_of_date AND a.dividend_yield IS NOT NULL
                AND ABS(fundamentals.dividend_yield / 100 - a.dividend_yield)
                    < ABS(fundamentals.dividend_yield - a.dividend_yield))
        OR (dividend_yield >= 0.1 AND NOT EXISTS (
                SELECT 1 FROM fundamentals a WHERE a.code=fundamentals.code AND a.source='asx'
                AND a.as_of_date=fundamentals.as_of_date AND a.dividend_yield IS NOT NULL)));
    UPDATE fundamentals SET pe=NULL WHERE ABS(pe) >= 99999;
    UPDATE fundamentals SET forward_pe=NULL WHERE ABS(forward_pe) >= 99999;
    UPDATE fundamentals SET pb=NULL WHERE ABS(pb) >= 99999;
    """,
    # 3: phase 3 - daily technical snapshot + fundamental scores (history accrues from now)
    """
    CREATE TABLE technicals(
        code TEXT NOT NULL,
        date TEXT NOT NULL,          -- last price date the values are computed from
        close REAL, sma20 REAL, sma50 REAL, sma200 REAL, ema20 REAL, ema50 REAL,
        rsi14 REAL, macd REAL, macd_signal REAL, macd_hist REAL,
        bb_upper REAL, bb_lower REAL, bb_pctb REAL, bb_width REAL, vol_z REAL,
        dist_52w_high REAL, dist_52w_low REAL,
        mom_1m REAL, mom_3m REAL, mom_6m REAL, mom_12m REAL, mom_12_1 REAL,
        vol_20d REAL, vol_60d REAL, trend TEXT,
        computed_at TEXT,
        PRIMARY KEY(code, date)
    ) WITHOUT ROWID;
    CREATE TABLE scores(
        code TEXT NOT NULL,
        as_of_date TEXT NOT NULL,
        sector TEXT, sector_n INTEGER,
        valuation REAL, quality REAL, growth REAL, dividend REAL, composite REAL,
        sector_rank INTEGER,
        detail_json TEXT,
        computed_at TEXT,
        PRIMARY KEY(code, as_of_date)
    );
    """,
    # 4: phase 4 - news, company links, tags, sentiment
    """
    CREATE TABLE news_items(
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        title TEXT NOT NULL,
        url TEXT,
        summary TEXT,                -- plain text, <= 300 chars; never full article text
        categories TEXT,
        published_at TEXT,           -- feed time (UTC), may be missing
        first_seen_at TEXT NOT NULL,
        available_at TEXT NOT NULL,  -- max(published_at, first_seen_at): point-in-time
        simhash INTEGER,
        cluster_id TEXT,             -- id of the first item of the same story
        sent_key TEXT,               -- -> text_sentiment.text_hash
        fetched_at TEXT
    );
    CREATE INDEX ix_news_avail ON news_items(available_at);
    CREATE INDEX ix_news_cluster ON news_items(cluster_id);
    CREATE TABLE news_links(
        item_id TEXT NOT NULL, code TEXT NOT NULL, method TEXT, confidence REAL, in_title INTEGER,
        PRIMARY KEY(item_id, code)
    );
    CREATE INDEX ix_news_links_code ON news_links(code);
    CREATE TABLE text_sentiment(
        text_hash TEXT PRIMARY KEY,  -- identical texts are scored once
        model TEXT, positive REAL, negative REAL, neutral REAL, label TEXT, score REAL, computed_at TEXT
    );
    CREATE TABLE item_tags(
        kind TEXT NOT NULL,          -- news | ann
        ref TEXT NOT NULL,           -- news_items.id | announcements.ids_id
        tag_type TEXT NOT NULL,      -- event | macro
        tag TEXT NOT NULL,
        PRIMARY KEY(kind, ref, tag_type, tag)
    );
    CREATE INDEX ix_item_tags_tag ON item_tags(tag_type, tag);
    ALTER TABLE announcements ADD COLUMN sent_key TEXT;
    ALTER TABLE announcements ADD COLUMN tagged INTEGER NOT NULL DEFAULT 0;
    CREATE INDEX ix_ann_sent ON announcements(sent_key);
    CREATE INDEX ix_ann_tagged ON announcements(tagged);
    """,
    # 5: strip Guardian newsletter boilerplate from stored summaries
    """
    UPDATE news_items SET summary = TRIM(REPLACE(REPLACE(summary,
        ' Get our breaking news email , free app or daily news podcast ', ' '),
        'Get our breaking news email , free app or daily news podcast', ''))
    WHERE summary LIKE '%Get our breaking news email%';
    """,
    # 6: phase 5 - forecasts + backtest runs
    """
    CREATE TABLE forecasts(
        code TEXT NOT NULL,
        as_of_date TEXT NOT NULL,     -- feature date (last close used)
        horizon INTEGER NOT NULL,     -- trading days: 5 | 21 | 63
        variant TEXT,                 -- news | no_news
        q10 REAL, q50 REAL, q90 REAL, -- total-return quantiles over the horizon
        p_up REAL,                    -- P(return > 0)
        p_beat REAL, ex50 REAL,       -- 21d only: P(beat STW), median excess return
        signal TEXT, confidence TEXT, rank INTEGER, eligible INTEGER,
        explain_json TEXT,
        model_version TEXT,
        computed_at TEXT,
        PRIMARY KEY(code, as_of_date, horizon)
    );
    CREATE INDEX ix_forecasts_date ON forecasts(as_of_date, horizon);
    CREATE TABLE backtest_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
        config_json TEXT, summary_json TEXT, curves_json TEXT, message TEXT
    );
    """,
    # 7: phase 6 - watchlist + paper trading
    """
    CREATE TABLE watchlist(
        code TEXT PRIMARY KEY,
        added_at TEXT NOT NULL,
        note TEXT
    );
    CREATE TABLE paper_portfolios(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,            -- manual | signals
        capital REAL NOT NULL,
        top_n INTEGER,                 -- signals: positions held
        created_at TEXT NOT NULL,
        last_rebalance TEXT,           -- signals: forecast as_of_date used for the last rebalance
        active INTEGER NOT NULL DEFAULT 1,
        note TEXT
    );
    CREATE TABLE paper_orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        portfolio_id INTEGER NOT NULL REFERENCES paper_portfolios(id) ON DELETE CASCADE,
        code TEXT NOT NULL,
        side TEXT NOT NULL,            -- buy | sell
        quantity INTEGER,              -- shares; sell NULL = whole position
        amount REAL,                   -- buy by A$ amount (incl. brokerage)
        placed_at TEXT NOT NULL,       -- UTC
        exec_after TEXT NOT NULL,      -- market date: fills at the open of the first trading day >= this
        reason TEXT,
        signal_date TEXT,              -- forecast as_of_date behind an automatic order
        status TEXT NOT NULL DEFAULT 'pending',   -- pending | filled | cancelled | rejected
        filled_date TEXT, price REAL, filled_qty INTEGER, fee REAL, value REAL, message TEXT
    );
    CREATE INDEX ix_paper_orders ON paper_orders(portfolio_id, status);
    """,
    # 8: phase 7 - real holdings (entered by hand) + alert log
    """
    CREATE TABLE holding_trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL,
        side TEXT NOT NULL,            -- buy | sell
        trade_date TEXT NOT NULL,      -- market date
        quantity REAL NOT NULL,        -- shares as traded (before any later split)
        price REAL NOT NULL,           -- A$ per share
        fees REAL NOT NULL DEFAULT 0,  -- brokerage
        note TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX ix_holding_trades ON holding_trades(code, trade_date);
    CREATE TABLE alerts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT NOT NULL UNIQUE,      -- dedupe key
        kind TEXT NOT NULL,            -- signal | announcement | job | test
        code TEXT,
        title TEXT NOT NULL,
        body TEXT,
        url TEXT,
        priority INTEGER NOT NULL DEFAULT 3,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL,          -- queued | sent | failed | skipped
        sent_at TEXT,
        channels TEXT,
        error TEXT
    );
    CREATE INDEX ix_alerts_status ON alerts(status, created_at);
    """,
    # 9: dividends.currency = the currency of `amount`. Yahoo's .AX amounts are already in AUD
    # (e.g. BHP's US$0.60 FY25 final = A$0.9193); the ASX "declared in USD" had been copied onto them.
    # ASX-only rows (amount still as the ASX quoted it, matching its calendar text) keep their currency.
    """
    UPDATE dividends SET currency='AUD' WHERE source='yahoo';
    UPDATE dividends SET currency='AUD' WHERE source='asx' AND currency IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM calendar_events e WHERE e.code=dividends.code AND e.event_type='ex_dividend'
        AND e.event_date=dividends.ex_date AND e.detail LIKE CAST(dividends.amount AS TEXT) || ' %');
    UPDATE dividends SET currency='AUD' WHERE currency IS NULL;
    """,
    # 10: AI announcement summaries; corporate actions on holdings
    """
    CREATE TABLE ai_summaries(
        ids_id TEXT PRIMARY KEY,       -- announcements.ids_id
        code TEXT NOT NULL,
        model TEXT,
        summary TEXT,
        key_points_json TEXT,
        events_json TEXT,
        tone TEXT,
        pages INTEGER,
        input_tokens INTEGER, output_tokens INTEGER,
        created_at TEXT NOT NULL,
        error TEXT                     -- set when it couldn't be summarised (not retried)
    );
    ALTER TABLE holding_trades ADD COLUMN other_code TEXT;   -- demerger: the new company's code
    ALTER TABLE holding_trades ADD COLUMN ratio REAL;        -- demerger: new shares per share held
    ALTER TABLE holding_trades ADD COLUMN cost_pct REAL;     -- demerger: % of cost base moved to the new shares
    """,
    # 11: calibrated forecasts (q10/q90/p_up/p_beat now hold calibrated values; raw model output kept)
    """
    ALTER TABLE forecasts ADD COLUMN q10_raw REAL;
    ALTER TABLE forecasts ADD COLUMN q90_raw REAL;
    ALTER TABLE forecasts ADD COLUMN p_up_raw REAL;
    ALTER TABLE forecasts ADD COLUMN p_beat_raw REAL;
    ALTER TABLE forecasts ADD COLUMN calibrated INTEGER;
    """,
]


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(path: Path | None = None) -> None:
    path = path or get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with _init_lock:
        conn = _connect(path)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
                conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version={i};\nCOMMIT;")
            # Any job left "running" by a crash/restart is now interrupted.
            conn.execute(
                "UPDATE job_runs SET status='interrupted', finished_at=? WHERE status='running'",
                (utcnow(),),
            )
            conn.commit()
        finally:
            conn.close()


@contextmanager
def get_conn(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(path or get_settings().db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert(
    conn: sqlite3.Connection,
    table: str,
    rows: Iterable[dict],
    pk: list[str],
    update: list[str] | None = None,
    keep_existing: list[str] | None = None,
) -> int:
    """INSERT ... ON CONFLICT(pk) DO UPDATE. Returns rows written.

    keep_existing: columns where a NULL incoming value must not overwrite an
    existing non-NULL one (merging partial sources).
    """
    rows = list(rows)
    if not rows:
        return 0
    cols = list(rows[0].keys())
    update = update if update is not None else [c for c in cols if c not in pk]
    keep = set(keep_existing or [])
    sets = []
    for c in update:
        if c in keep:
            sets.append(f"{c}=COALESCE(excluded.{c}, {table}.{c})")
        else:
            sets.append(f"{c}=excluded.{c}")
    sql = (
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
        f"ON CONFLICT({','.join(pk)}) "
        + (f"DO UPDATE SET {', '.join(sets)}" if sets else "DO NOTHING")
    )
    conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
    return len(rows)


def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )

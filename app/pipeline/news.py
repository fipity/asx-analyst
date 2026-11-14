"""News steps: RSS ingestion (dedupe, company matching, tags), announcement tags,
and FinBERT sentiment for both. All resumable and idempotent."""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from app.db import get_conn, kv_get, kv_set, upsert, utcnow
from app.jobs import JobContext, source_ok
from app.news import matching, sentiment, tagging, text
from app.sources import rss
from app.sources.base import HttpClient, SourceError

log = logging.getLogger(__name__)
DEDUPE_WINDOW_DAYS = 3
MIN_LINK_CONFIDENCE = 0.6
_sentiment_busy = threading.Lock()     # one model in memory at a time (~0.75 GB each)


def sent_key(t: str) -> str:
    return hashlib.sha1(" ".join((t or "").lower().split()).encode()).hexdigest()[:20]


def news_text(item: dict) -> str:
    """Model + matching input for an RSS item: headline plus summary."""
    s = item.get("summary") or ""
    return f"{item['title']}. {s}" if s else item["title"]


def _patterns(c, codes: list[str]):
    ph = ",".join("?" * len(codes)) if codes else "''"
    names = dict(c.execute(f"SELECT code, name FROM companies WHERE code IN ({ph})", codes).fetchall())
    listed = {r[0] for r in c.execute("SELECT code FROM companies WHERE status='listed'")}
    return matching.build_patterns(names), listed


def link_and_tag(c, items: list[dict], codes: list[str]) -> int:
    """Company links + event/macro tags for news items (dicts with id, title, summary)."""
    pats, listed = _patterns(c, codes)
    links, tags = [], []
    for it in items:
        body = news_text(it)
        title_hits = matching.match(it["title"], pats, listed)
        for code, (method, conf) in matching.match(body, pats, listed).items():
            in_title = code in title_hits
            conf = conf if in_title else round(conf * 0.8, 3)      # mentioned only in the summary
            if conf >= MIN_LINK_CONFIDENCE:
                links.append({"item_id": it["id"], "code": code, "method": method, "confidence": conf,
                              "in_title": int(in_title)})
        tags += [{"kind": "news", "ref": it["id"], "tag_type": "event", "tag": t} for t in tagging.event_tags(body)]
        tags += [{"kind": "news", "ref": it["id"], "tag_type": "macro", "tag": t} for t in tagging.macro_tags(body)]
    upsert(c, "news_links", links, ["item_id", "code"])
    upsert(c, "item_tags", tags, ["kind", "ref", "tag_type", "tag"], update=[])
    return len(links)


def step_rss(ctx: JobContext, codes: list[str]) -> list[str]:
    http = HttpClient("rss")
    new_items: list[dict] = []
    now = utcnow()
    try:
        for feed in rss.FEEDS:
            try:
                items = rss.fetch_feed(http, feed)
            except SourceError as e:
                ctx.error(f"rss-{feed.key}", feed.url, e)
                continue
            source_ok(f"rss-{feed.key}")
            with get_conn() as c:
                ids = [i["id"] for i in items]
                seen = {r[0] for r in c.execute(
                    f"SELECT id FROM news_items WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else set()
            new_items += [i for i in items if i["id"] not in seen]
    finally:
        http.close()
    if not new_items:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DEDUPE_WINDOW_DAYS)).replace(microsecond=0).isoformat()
    with get_conn() as c:
        recent = [dict(r) for r in c.execute(
            "SELECT id, source, title, simhash, cluster_id FROM news_items WHERE available_at >= ? ORDER BY available_at",
            (cutoff,))]
        rows = []
        # oldest first so the earliest copy of a story becomes the cluster head
        for it in sorted(new_items, key=lambda x: x["published_at"] or now):
            h = text.simhash(news_text(it))
            cluster = next((r["cluster_id"] for r in recent
                            if text.is_duplicate(it["title"], h, r["title"], r["simhash"] or 0,
                                                 same_source=r["source"] == it["source"])), it["id"])
            avail = max(it["published_at"] or now, now)
            row = {**it, "first_seen_at": now, "available_at": avail, "simhash": h, "cluster_id": cluster,
                   "fetched_at": now}
            rows.append(row)
            recent.append({"id": it["id"], "source": it["source"], "title": it["title"], "simhash": h,
                           "cluster_id": cluster})
        upsert(c, "news_items", rows, ["id"], update=[])
        n_links = link_and_tag(c, rows, codes)
    ctx.items += len(rows)
    ctx.notes.append(f"{len(rows)} new news items, {n_links} company links")
    return [r["id"] for r in rows]


def step_tag_announcements(ctx: JobContext, chunk: int = 20000) -> int:
    done = 0
    while True:
        with get_conn() as c:
            rows = c.execute("SELECT ids_id, headline FROM announcements WHERE tagged=0 LIMIT ?", (chunk,)).fetchall()
            if not rows:
                break
            tags = [{"kind": "ann", "ref": r[0], "tag_type": "event", "tag": t}
                    for r in rows for t in tagging.event_tags(r[1], "ann")]
            upsert(c, "item_tags", tags, ["kind", "ref", "tag_type", "tag"], update=[])
            c.executemany("UPDATE announcements SET tagged=1 WHERE ids_id=?", [(r[0],) for r in rows])
        done += len(rows)
    ctx.items += done
    return done


def step_sentiment(ctx: JobContext, limit: int | None = 3000, background: bool = False) -> int:
    """Score news items + announcement headlines that have no sentiment yet (newest first).
    limit=None processes the whole backlog in resumable chunks."""
    if sentiment.model_info() is None:
        ctx.notes.append("sentiment skipped: FinBERT model not installed (tools/install-finbert.sh)")
        return 0
    if not _sentiment_busy.acquire(blocking=False):
        ctx.notes.append("sentiment skipped: another sentiment run is in progress (it covers new items too)")
        return 0
    try:
        return _run_sentiment(ctx, limit, background)
    finally:
        _sentiment_busy.release()


def _run_sentiment(ctx: JobContext, limit: int | None, background: bool) -> int:
    if not background:
        return _score(ctx, limit)
    # Low priority: renice a dedicated thread (on Linux nice() is per thread, and the
    # onnxruntime worker threads it creates inherit it). Never renice a reused pool thread.
    result: dict = {}

    def work():
        try:
            os.nice(10)
        except OSError:
            pass
        try:
            result["n"] = _score(ctx, limit)
        except BaseException as e:  # noqa: BLE001 - re-raised in the job thread
            result["exc"] = e

    t = threading.Thread(target=work, name="sentiment-backfill", daemon=True)
    t.start()
    t.join()
    if "exc" in result:
        raise result["exc"]
    return result.get("n", 0)


def _score(ctx: JobContext, limit: int | None) -> int:
    model = None
    total = 0
    try:
        while True:
            with get_conn() as c:
                todo: list[tuple[str, str, str]] = []   # (kind, ref, text)
                n = 2000 if limit is None else max(0, limit - total)
                if n == 0:
                    break
                for r in c.execute("SELECT id, title, summary FROM news_items WHERE sent_key IS NULL "
                                   "ORDER BY available_at DESC LIMIT ?", (n,)):
                    todo.append(("news", r[0], news_text(dict(r))))
                if len(todo) < n:
                    for r in c.execute("SELECT ids_id, headline FROM announcements WHERE sent_key IS NULL "
                                       "ORDER BY published_at DESC LIMIT ?", (n - len(todo),)):
                        todo.append(("ann", r[0], r[1]))
                if not todo:
                    break
                keys = {sent_key(t) for _, _, t in todo}
                have = {r[0] for r in c.execute(
                    f"SELECT text_hash FROM text_sentiment WHERE text_hash IN ({','.join('?' * len(keys))})", list(keys))}
            need: dict[str, str] = {}
            for _, _, t in todo:
                k = sent_key(t)
                if k not in have:
                    need.setdefault(k, t)
            if need:
                if model is None:
                    model = sentiment.FinBert(threads=2)
                preds = model.predict(list(need.values()), batch_size=64)
                now = utcnow()
                rows = [{"text_hash": k, "model": sentiment.MODEL_NAME, **p, "computed_at": now}
                        for k, p in zip(need, preds)]
            else:
                rows = []
            with get_conn() as c:
                upsert(c, "text_sentiment", rows, ["text_hash"])
                c.executemany("UPDATE news_items SET sent_key=? WHERE id=?",
                              [(sent_key(t), ref) for kind, ref, t in todo if kind == "news"])
                c.executemany("UPDATE announcements SET sent_key=? WHERE ids_id=?",
                              [(sent_key(t), ref) for kind, ref, t in todo if kind == "ann"])
            total += len(todo)
            ctx.items += len(need)
            ctx.progress(f"sentiment: {total} items scored ({len(need)} new texts in last batch)")
    finally:
        if model is not None:
            model.close()
            del model
            _release_memory()
    return total


def _release_memory() -> None:
    """Return freed model memory (~0.5 GB) to the OS instead of keeping it in malloc arenas."""
    import ctypes
    import gc
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except OSError:
        pass


def backlog(c) -> dict:
    return {
        "news_unscored": c.execute("SELECT COUNT(*) FROM news_items WHERE sent_key IS NULL").fetchone()[0],
        "ann_unscored": c.execute("SELECT COUNT(*) FROM announcements WHERE sent_key IS NULL").fetchone()[0],
        "ann_untagged": c.execute("SELECT COUNT(*) FROM announcements WHERE tagged=0").fetchone()[0],
    }

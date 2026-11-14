"""Read-side queries for news on the news page, company pages and home."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from statistics import mean, pstdev

from app.news.tagging import EVENT_LABELS, MACRO_LABELS
from app.sources.rss import FEEDS_BY_KEY

SENTIMENT_FILTERS = {"positive": "Positive", "negative": "Negative", "neutral": "Neutral"}


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


def _attach(c: sqlite3.Connection, items: list[dict], kind: str) -> None:
    """Add codes, events, macro tags to news items (kind='news') or announcements (kind='ann')."""
    if not items:
        return
    key = "id" if kind == "news" else "ids_id"
    refs = [i[key] for i in items]
    ph = ",".join("?" * len(refs))
    tags: dict[str, dict[str, list[str]]] = {}
    for ref, tt, tag in c.execute(f"SELECT ref, tag_type, tag FROM item_tags WHERE kind=? AND ref IN ({ph})",
                                  [kind, *refs]):
        tags.setdefault(ref, {}).setdefault(tt, []).append(tag)
    links: dict[str, list[dict]] = {}
    if kind == "news":
        for item_id, code, conf, in_title in c.execute(
                f"SELECT item_id, code, confidence, in_title FROM news_links WHERE item_id IN ({ph}) "
                f"ORDER BY in_title DESC, confidence DESC", refs):
            links.setdefault(item_id, []).append({"code": code, "confidence": conf, "in_title": bool(in_title)})
    for i in items:
        t = tags.get(i[key], {})
        i["events"] = [(e, EVENT_LABELS.get(e, e)) for e in t.get("event", [])]
        i["macro"] = [(m, MACRO_LABELS.get(m, m)) for m in t.get("macro", [])]
        if kind == "news":
            i["codes"] = links.get(i[key], [])
            feed = FEEDS_BY_KEY.get(i["source"])
            i["source_name"] = feed.name if feed else i["source"]


def news_list(c: sqlite3.Connection, *, code: str | None = None, source: str | None = None,
              event: str | None = None, macro: str | None = None, sentiment: str | None = None,
              scope: str | None = None, q: str | None = None, days: int | None = 7,
              limit: int = 40, offset: int = 0) -> tuple[list[dict], bool]:
    """Story-level list (one row per dedupe cluster, newest first). Returns (items, has_more)."""
    where, args = ["1=1"], []
    if days:
        where.append("n.available_at >= ?")
        args.append(_since(days))
    if code:
        where.append("n.id IN (SELECT item_id FROM news_links WHERE code=?)")
        args.append(code.upper())
    if source:
        where.append("n.source = ?")
        args.append(source)
    if event:
        where.append("n.id IN (SELECT ref FROM item_tags WHERE kind='news' AND tag_type='event' AND tag=?)")
        args.append(event)
    if macro:
        where.append("n.id IN (SELECT ref FROM item_tags WHERE kind='news' AND tag_type='macro' AND tag=?)")
        args.append(macro)
    if scope == "company":
        where.append("n.id IN (SELECT item_id FROM news_links)")
    elif scope == "macro":
        where.append("n.id IN (SELECT ref FROM item_tags WHERE kind='news' AND tag_type='macro')")
    if sentiment in SENTIMENT_FILTERS:
        where.append("s.label = ?")
        args.append(sentiment)
    if q:
        where.append("(n.title LIKE ? OR n.summary LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    rows = [dict(r) for r in c.execute(
        f"""SELECT n.id, n.source, n.title, n.url, n.summary, n.published_at, n.available_at, n.cluster_id,
                   s.label AS sent_label, s.score AS sent_score
            FROM news_items n LEFT JOIN text_sentiment s ON s.text_hash = n.sent_key
            WHERE {' AND '.join(where)} ORDER BY n.available_at DESC""", args)]
    # one row per story: keep the newest matching copy, list the other sources
    seen: dict[str, dict] = {}
    stories: list[dict] = []
    for r in rows:
        head = seen.get(r["cluster_id"])
        if head is None:
            r["also"] = []
            seen[r["cluster_id"]] = r
            stories.append(r)
        elif r["source"] != head["source"]:
            head["also"].append({"source": FEEDS_BY_KEY[r["source"]].name if r["source"] in FEEDS_BY_KEY
                                 else r["source"], "url": r["url"]})
    page = stories[offset: offset + limit]
    _attach(c, page, "news")
    return page, len(stories) > offset + limit


def announcements_with_signals(c: sqlite3.Connection, code: str, ps_only: bool, limit: int, offset: int) -> list[dict]:
    where = "AND a.price_sensitive=1" if ps_only else ""
    rows = [dict(r) for r in c.execute(
        f"""SELECT a.ids_id, a.published_at, a.headline, a.price_sensitive, a.pages, a.url,
                   s.label AS sent_label, s.score AS sent_score
            FROM announcements a LEFT JOIN text_sentiment s ON s.text_hash = a.sent_key
            WHERE a.code=? {where} ORDER BY a.published_at DESC LIMIT ? OFFSET ?""", (code, limit, offset))]
    _attach(c, rows, "ann")
    from app.news.ai import for_ids
    ai = for_ids(c, [r["ids_id"] for r in rows])
    for r in rows:
        r["ai"] = ai.get(r["ids_id"])
    return rows


def _avg(xs: list[float]) -> float | None:
    return mean(xs) if xs else None


def company_summary(c: sqlite3.Connection, code: str) -> dict:
    """News + announcement activity and average sentiment for a company."""
    out: dict = {}
    for days in (7, 30):
        rows = c.execute(
            """SELECT n.cluster_id, s.score FROM news_items n JOIN news_links l ON l.item_id=n.id AND l.code=?
               LEFT JOIN text_sentiment s ON s.text_hash=n.sent_key WHERE n.available_at >= ?""",
            (code, _since(days))).fetchall()
        by_story = {}
        for cid, score in rows:
            if score is not None:
                by_story.setdefault(cid, score)
        out[f"news_{days}d"] = len({r[0] for r in rows})
        out[f"news_sent_{days}d"] = _avg(list(by_story.values()))
    ps = c.execute(
        """SELECT s.score FROM announcements a LEFT JOIN text_sentiment s ON s.text_hash=a.sent_key
           WHERE a.code=? AND a.price_sensitive=1 AND a.published_at >= ?""", (code, _since(90))).fetchall()
    out["ann_ps_90d"] = len(ps)
    out["ann_sent_90d"] = _avg([r[0] for r in ps if r[0] is not None])
    # announcement activity: last 30 days vs the previous 12 months (monthly counts)
    counts = []
    for k in range(13):
        lo, hi = _since(30 * (k + 1)), _since(30 * k)
        counts.append(c.execute("SELECT COUNT(*) FROM announcements WHERE code=? AND published_at >= ? AND published_at < ?",
                                (code, lo, hi)).fetchone()[0])
    out["ann_30d"] = counts[0]
    hist = counts[1:]
    out["ann_month_avg"] = mean(hist) if hist else None
    sd = pstdev(hist) if len(hist) > 1 else 0
    out["ann_activity_z"] = (counts[0] - mean(hist)) / sd if sd > 0 else None
    out["events_90d"] = [(t, EVENT_LABELS.get(t, t), n) for t, n in c.execute(
        """SELECT t.tag, COUNT(*) FROM item_tags t JOIN announcements a ON a.ids_id=t.ref
           WHERE t.kind='ann' AND t.tag_type='event' AND a.code=? AND a.published_at >= ?
           GROUP BY t.tag ORDER BY COUNT(*) DESC""", (code, _since(90)))]
    return out


def sources_summary(c: sqlite3.Connection) -> list[dict]:
    rows = {r[0]: (r[1], r[2]) for r in c.execute(
        "SELECT source, COUNT(*), MAX(available_at) FROM news_items GROUP BY source")}
    return [{"key": k, "name": f.name, "items": rows.get(k, (0, None))[0], "latest": rows.get(k, (0, None))[1]}
            for k, f in FEEDS_BY_KEY.items()]

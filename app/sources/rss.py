"""RSS news feeds. Stores headline, link, time and a short plain-text summary only:
full article text (e.g. the Motley Fool `content:encoded` field) is never kept.

Feeds were checked against each site's robots.txt on 2026-11-13, and HttpClient
re-checks robots.txt on every fetch. Google News is excluded (robots.txt
disallows it) and the AFR is paywalled.
"""
from __future__ import annotations

import calendar
import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser

from app.sources.base import HttpClient, SourceError

SUMMARY_CHARS = 300


@dataclass(frozen=True)
class Feed:
    key: str
    name: str
    url: str
    scope: str        # "market" (company + market news) | "macro"


FEEDS: list[Feed] = [
    Feed("abc", "ABC News Business", "https://www.abc.net.au/news/feed/51892/rss.xml", "market"),
    Feed("guardian", "The Guardian AU Business", "https://www.theguardian.com/au/business/rss", "market"),
    Feed("smh", "SMH Business", "https://www.smh.com.au/rss/business.xml", "market"),
    Feed("fool", "Motley Fool AU", "https://www.fool.com.au/feed/", "market"),
    Feed("rba", "RBA media releases", "https://www.rba.gov.au/rss/rss-cb-media-releases.xml", "macro"),
]
FEEDS_BY_KEY = {f.key: f for f in FEEDS}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Boilerplate some feeds append to summaries ("The post X appeared first on Y").
_BOILERPLATE = [re.compile(r"\bThe post .* appeared first on .*$", re.S), re.compile(r"\bContinue reading\.*$"),
                re.compile(r"\s*Get our breaking news email\s*,\s*free app or daily news podcast\s*", re.I)]


def plain_text(s: str | None, limit: int | None = None) -> str:
    if not s:
        return ""
    t = _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", s))).strip()
    for rx in _BOILERPLATE:
        t = rx.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    if limit and len(t) > limit:
        cut = t[:limit].rsplit(" ", 1)[0]
        t = cut.rstrip(",;:-") + "…"
    return t


def item_id(feed_key: str, guid: str) -> str:
    return hashlib.sha1(f"{feed_key}|{guid}".encode()).hexdigest()[:20]


def parse_feed(data: bytes, feed: Feed) -> list[dict]:
    d = feedparser.parse(data)
    if d.bozo and not d.entries:
        raise SourceError(f"{feed.key}: unparseable feed ({d.bozo_exception})")
    out = []
    for e in d.entries:
        title = plain_text(e.get("title"))
        link = e.get("link")
        if not title or not link:
            continue
        ts = e.get("published_parsed") or e.get("updated_parsed")
        published = (datetime.fromtimestamp(calendar.timegm(ts), tz=timezone.utc).replace(microsecond=0).isoformat()
                     if ts else None)
        out.append({
            "id": item_id(feed.key, e.get("id") or link),
            "source": feed.key,
            "title": title[:400],
            "url": link,
            "summary": plain_text(e.get("summary"), SUMMARY_CHARS),
            "published_at": published,
            "categories": ", ".join(t.term for t in e.get("tags", []) if getattr(t, "term", None))[:300] or None,
        })
    return out


def fetch_feed(http: HttpClient, feed: Feed) -> list[dict]:
    return parse_feed(http.get(feed.url, cache_ttl=300), feed)

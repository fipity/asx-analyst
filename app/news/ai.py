"""Optional AI summaries of price-sensitive ASX announcements (off unless LLM_API_KEY is set).

For each new price-sensitive announcement in scope (by default: stocks on your watchlist or in
your holdings), the PDF is downloaded from the ASX, its text extracted, and Claude writes a short
plain-English summary with event tags and tone (structured output). Only the summary is stored,
never the document text. A daily cap limits cost; FinBERT stays the default sentiment model.
"""
from __future__ import annotations

import io
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Literal

import anthropic
from pydantic import BaseModel, Field
from pypdf import PdfReader

from app.config import get_settings
from app.db import get_conn, utcnow
from app.news.tagging import EVENT_LABELS
from app.sources import asx
from app.sources.appendix3y import pdf_to_text
from app.sources.base import HttpClient, SourceError

log = logging.getLogger(__name__)
MAX_PAGES = 15
MAX_CHARS = 60_000          # ~15k tokens: the first pages hold the substance of results and updates
LOOKBACK_DAYS = 3           # only recent announcements (no backfill of history)

EventKey = Literal[tuple(EVENT_LABELS)]  # type: ignore[valid-type]


class Summary(BaseModel):
    summary: str = Field(description="2-4 plain-English sentences: what was announced and why it matters to a shareholder")
    key_points: list[str] = Field(description="Up to 4 short facts with the key numbers (e.g. 'Revenue up 12% to A$1.2B')")
    events: list[EventKey] = Field(description="Event types this announcement is about (empty if none fit)")
    tone: Literal["positive", "neutral", "negative", "mixed"] = Field(
        description="For existing shareholders, judged from the facts in the document")


SYSTEM = (
    "You summarise ASX company announcements for a private investor in Australia. Use only facts stated in the "
    "document; never guess, never give advice or a recommendation, and say plainly when numbers are missing. "
    "Use Australian spelling and A$ for Australian dollars. Keep the summary short and concrete.\n\n"
    "Event types: " + "; ".join(f"{k} = {v}" for k, v in EVENT_LABELS.items())
)


def enabled() -> bool:
    return bool(get_settings().llm_api_key)


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=get_settings().llm_api_key, timeout=120.0, max_retries=2)


def summarise_text(text: str, headline: str, code: str, pages: int, total_pages: int,
                   client: anthropic.Anthropic | None = None) -> tuple[Summary, dict]:
    """One structured-output call. Returns (summary, usage dict). Raises on API errors/refusals."""
    s = get_settings()
    note = f" (first {pages} of {total_pages} pages)" if total_pages > pages else ""
    resp = (client or _client()).messages.parse(
        model=s.llm_model,
        max_tokens=2000,
        system=SYSTEM,
        messages=[{"role": "user", "content": f"Company: {code}\nHeadline: {headline}\n\n"
                                              f"Announcement text{note}:\n<document>\n{text}\n</document>"}],
        output_format=Summary,
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("the model declined to summarise this document")
    if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
        raise RuntimeError(f"no usable summary (stop reason {resp.stop_reason})")
    u = resp.usage
    return resp.parsed_output, {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens, "model": resp.model}


def candidates(c: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """Recent price-sensitive announcements in scope that have no summary yet (oldest first)."""
    s = get_settings()
    now = now or datetime.now(timezone.utc)
    if s.llm_scope == "universe":
        from app.pipeline.ingest import latest_members
        codes = latest_members(c, s.universe)
    else:
        from app.alerts import followed_codes
        codes = list(followed_codes(c))
    if not codes:
        return []
    return [dict(r) for r in c.execute(
        f"SELECT a.ids_id, a.code, a.headline, a.published_at, a.pdf_url FROM announcements a "
        f"LEFT JOIN ai_summaries s ON s.ids_id=a.ids_id WHERE s.ids_id IS NULL AND a.price_sensitive=1 "
        f"AND a.published_at >= ? AND a.code IN ({','.join('?' * len(codes))}) ORDER BY a.published_at",
        [(now - timedelta(days=LOOKBACK_DAYS)).isoformat(), *codes])]


def used_today(c: sqlite3.Connection) -> int:
    start = datetime.now(timezone.utc) - timedelta(hours=24)
    return c.execute("SELECT COUNT(*) FROM ai_summaries WHERE created_at >= ? AND error IS NULL",
                     (start.isoformat(),)).fetchone()[0]


def step_ai_summaries(ctx) -> None:
    if not enabled():
        return
    s = get_settings()
    with get_conn() as c:
        todo = candidates(c)
        room = max(0, s.llm_daily_limit - used_today(c))
    if not todo:
        return
    if room <= 0:
        ctx.notes.append(f"AI summaries: daily limit of {s.llm_daily_limit} reached")
        return
    http = HttpClient("asx")
    client = _client()
    done = failed = 0
    try:
        for a in todo[:room]:
            row = {"ids_id": a["ids_id"], "code": a["code"], "model": s.llm_model, "summary": None,
                   "key_points_json": None, "events_json": None, "tone": None, "pages": None,
                   "input_tokens": None, "output_tokens": None, "created_at": utcnow(), "error": None}
            try:
                _, data = asx.fetch_pdf(http, a["ids_id"])
                total = len(PdfReader(io.BytesIO(data)).pages)
                text = pdf_to_text(data, max_pages=MAX_PAGES)[:MAX_CHARS].strip()
                if len(text) < 40:
                    raise RuntimeError("no readable text in the PDF (scanned image?)")
                summ, usage = summarise_text(text, a["headline"], a["code"], min(total, MAX_PAGES), total, client)
                row.update(summary=summ.summary.strip()[:1200],
                           key_points_json=json.dumps([p.strip()[:200] for p in summ.key_points[:4]]),
                           events_json=json.dumps(list(dict.fromkeys(summ.events))), tone=summ.tone,
                           pages=min(total, MAX_PAGES), input_tokens=usage["input_tokens"],
                           output_tokens=usage["output_tokens"], model=usage["model"])
                done += 1
            except anthropic.AuthenticationError:
                ctx.error("llm", a["ids_id"], "LLM_API_KEY was rejected (check the key)")
                return
            except (anthropic.RateLimitError, anthropic.APIConnectionError) as e:
                ctx.error("llm", a["ids_id"], e)   # transient: leave unsummarised, try next run
                return
            except (anthropic.APIStatusError, SourceError, RuntimeError, ValueError) as e:
                row["error"] = str(e)[:300]          # recorded so it isn't retried every 30 minutes
                failed += 1
                ctx.error("llm", a["ids_id"], e)
            with get_conn() as c:
                cols = list(row)
                c.execute(f"INSERT OR REPLACE INTO ai_summaries({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                          [row[k] for k in cols])
    finally:
        http.close()
    ctx.notes.append(f"AI summaries: {done} written" + (f", {failed} failed" if failed else ""))


# List prices in US$ per million tokens (input, output), for the Status page estimate only
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0)}


def usage(c: sqlite3.Connection) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    r = c.execute("SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(error IS NOT NULL) FROM ai_summaries "
                  "WHERE created_at >= ?", (since,)).fetchone()
    out = {"n": r[0] or 0, "input_tokens": r[1] or 0, "output_tokens": r[2] or 0, "failed": r[3] or 0}
    p = PRICES.get(get_settings().llm_model)
    out["cost_usd"] = (out["input_tokens"] * p[0] + out["output_tokens"] * p[1]) / 1e6 if p else None
    return out


def for_ids(c: sqlite3.Connection, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    out = {}
    for r in c.execute(f"SELECT * FROM ai_summaries WHERE error IS NULL AND ids_id IN ({','.join('?' * len(ids))})", ids):
        d = dict(r)
        d["key_points"] = json.loads(d["key_points_json"] or "[]")
        d["events"] = [(e, EVENT_LABELS.get(e, e)) for e in json.loads(d["events_json"] or "[]")]
        out[d["ids_id"]] = d
    return out

"""ASX: company announcements (HTML pages on asx.com.au) and key statistics
(Markit Digital JSON that powers asx.com.au company pages).

Announcement pages:
  - market-wide today / previous business day (one request each, ~500 rows)
  - per company per year (history back to ~2010)
Each row: ASX code, Sydney date+time, price-sensitive marker, headline, idsId.
The PDF URL is behind a terms page whose form carries the real PDF link.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from app.config import MARKET_TZ
from app.sources.base import HttpClient, NotFound, SourceError

BASE = "https://www.asx.com.au"
TODAY_URL = BASE + "/asx/v2/statistics/todayAnns.do"
PREV_URL = BASE + "/asx/v2/statistics/prevBusDayAnns.do"
YEAR_URL = BASE + "/asx/v2/statistics/announcements.do?by=asxCode&asxCode={code}&timeframe=Y&year={year}"
DISPLAY_URL = BASE + "/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId={ids_id}"
MARKIT = "https://asx.api.markitdigital.com/asx-research/1.0/companies/{code}/{what}"

_PDF_RE = re.compile(r"https://announcements\.asx\.com\.au/asxpdf/[^\"'\s<>]+\.pdf")
_IDS_RE = re.compile(r"idsId=(\d+)")


@dataclass
class Announcement:
    ids_id: str
    code: str
    published_at: str      # UTC ISO
    headline: str
    price_sensitive: bool
    pages: int | None
    url: str


def _to_utc(date_s: str, time_s: str) -> str:
    dt = datetime.strptime(f"{date_s.strip()} {time_s.strip().upper()}", "%d/%m/%Y %I:%M %p")
    return dt.replace(tzinfo=MARKET_TZ).astimezone(timezone.utc).isoformat()


def parse_announcements_html(html: bytes | str, code: str | None = None) -> list[Announcement]:
    """Parse a today/prev-day page (has an ASX Code column) or a company-year
    page (pass `code`)."""
    soup = BeautifulSoup(html, "lxml")
    out: list[Announcement] = []
    for tr in soup.find_all("tr"):
        a = tr.find("a", href=_IDS_RE)
        if a is None:
            continue
        tds = tr.find_all("td", recursive=False)
        if not tds:
            continue
        row_code = code
        date_td = None
        for td in tds:
            if td.find("span", class_="dates-time"):
                date_td = td
                break
        if date_td is None:
            continue
        if row_code is None:
            first = tds[0].get_text(strip=True)
            if not re.fullmatch(r"[A-Z0-9]{3,6}", first):
                continue
            row_code = first
        time_span = date_td.find("span", class_="dates-time")
        time_s = time_span.get_text(strip=True)
        date_s = date_td.get_text(" ", strip=True).replace(time_s, "").strip()
        ps = tr.find("img", class_="pricesens") is not None
        # Headline is the anchor's first text node (before <br>).
        headline = ""
        for s in a.strings:
            s = s.strip()
            if s:
                headline = s
                break
        pages = None
        pspan = a.find("span", class_="page")
        if pspan:
            m = re.search(r"(\d+)", pspan.get_text())
            pages = int(m.group(1)) if m else None
        ids_id = _IDS_RE.search(a["href"]).group(1)
        try:
            published = _to_utc(date_s, time_s)
        except ValueError:
            continue
        out.append(Announcement(
            ids_id=ids_id, code=row_code.upper(), published_at=published,
            headline=headline, price_sensitive=ps, pages=pages,
            url=DISPLAY_URL.format(ids_id=ids_id),
        ))
    return out


def fetch_today(http: HttpClient) -> list[Announcement]:
    return parse_announcements_html(http.get(TODAY_URL))


def fetch_prev_day(http: HttpClient) -> list[Announcement]:
    return parse_announcements_html(http.get(PREV_URL))


def fetch_company_year(http: HttpClient, code: str, year: int, cache_ttl: float = 0) -> list[Announcement]:
    return parse_announcements_html(http.get(YEAR_URL.format(code=code, year=year), cache_ttl=cache_ttl), code=code)


def parse_pdf_url(terms_html: bytes | str) -> str | None:
    text = terms_html.decode("utf-8", "replace") if isinstance(terms_html, bytes) else terms_html
    m = _PDF_RE.search(text)
    return m.group(0) if m else None


def fetch_pdf(http: HttpClient, ids_id: str) -> tuple[str, bytes]:
    url = parse_pdf_url(http.get(DISPLAY_URL.format(ids_id=ids_id)))
    if not url:
        raise SourceError(f"no PDF link for announcement {ids_id}")
    return url, http.get(url, cache_ttl=30 * 86400)


# --- Key statistics (fundamentals + franking) -------------------------------

def parse_key_statistics(data: bytes | str) -> dict:
    """Return a dict of normalised fields from Markit key-statistics JSON."""
    j = json.loads(data)
    d = j.get("data") or {}
    if not d:
        raise NotFound("empty key-statistics")
    income = d.get("incomeStatement") or []
    latest = income[0] if income else {}
    prev = income[1] if len(income) > 1 else {}

    def growth(a, b):
        try:
            return (a - b) / abs(b) if a is not None and b else None
        except TypeError:
            return None

    yield_pct = d.get("yieldAnnual")
    return {
        "pe": d.get("priceEarningsRatio"),
        "eps": d.get("earningsPerShare"),
        "dividend_yield": (yield_pct / 100.0) if yield_pct is not None else None,
        "franking_pct": d.get("frankingPercent"),
        "fcf_yield": d.get("freeCashFlowYield"),
        "week52_high": d.get("priceFiftyTwoWeekHigh"),
        "week52_low": d.get("priceFiftyTwoWeekLow"),
        "shares_outstanding": d.get("numOfShares"),
        "revenue": latest.get("revenue"),
        "net_income": latest.get("netIncome"),
        "revenue_growth": growth(latest.get("revenue"), prev.get("revenue")),
        "earnings_growth": growth(latest.get("netIncome"), prev.get("netIncome")),
        "profit_margin": (latest["netIncome"] / latest["revenue"])
        if latest.get("revenue") and latest.get("netIncome") is not None else None,
        "currency": d.get("currency"),
        # dividend record
        "dividend": {
            "ex_date": d.get("dateExDate"),
            "pay_date": d.get("datePayDate"),
            "record_date": d.get("dateRecordDate"),
            "amount": d.get("dividend"),
            "currency": d.get("dividendCurrency"),
            "franking_pct": d.get("frankingPercent"),
        } if d.get("dateExDate") else None,
        "raw": d,
    }


def fetch_key_statistics(http: HttpClient, code: str) -> dict:
    return parse_key_statistics(http.get(MARKIT.format(code=code.lower(), what="key-statistics")))


def fetch_header(http: HttpClient, code: str) -> dict:
    j = json.loads(http.get(MARKIT.format(code=code.lower(), what="header")))
    d = j.get("data") or {}
    if not d:
        raise NotFound(f"no header for {code}")
    return d

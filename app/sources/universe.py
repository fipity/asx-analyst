"""Index constituents and the ASX company directory.

There is no free official S&P/ASX constituent feed. We use the daily holdings
files of ETFs that track each index (they hold every constituent):
  ASX 200: SPDR STW (primary), iShares IOZ (fallback)
  ASX 50:  SPDR SFY (primary), else top 50 of the ASX 200 by weight (approx.)
  ASX 100: top 100 of the ASX 200 by weight (approximation; labelled)
  ASX 300: top 300 by market cap from the ASX directory (approximation; labelled)
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

import openpyxl

from app.sources.base import HttpClient, SourceError
from app.sources.sectors import normalise_sector, sector_for_group

log = logging.getLogger(__name__)

SPDR_URL = "https://www.ssga.com/library-content/products/fund-data/etfs/apac/holdings-daily-au-en-{fund}.xlsx"
IOZ_URL = (
    "https://www.blackrock.com/au/products/251852/ishares-core-sp-asx-200-etf/"
    "1478358644060.ajax?fileType=csv&fileName=IOZ_holdings&dataType=fund"
)
DIRECTORY_URL = "https://asx.api.markitdigital.com/asx-research/1.0/companies/directory/file"
LISTED_CSV_URL = "https://www.asx.com.au/asx/research/ASXListedCompanies.csv"

_CODE_RE = re.compile(r"^[A-Z0-9]{3,6}$")
EXCLUDED_GROUPS = {"not applic", "class pend"}


@dataclass
class Member:
    code: str
    name: str | None = None
    weight: float | None = None
    sector: str | None = None


@dataclass
class IndexSnapshot:
    index_code: str
    as_of: str
    source: str
    members: list[Member] = field(default_factory=list)
    approximate: bool = False


def _num(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).replace(",", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _parse_date(s: str) -> str:
    s = s.strip()
    for fmt in ("%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {s!r}")


def parse_spdr_xlsx(data: bytes) -> tuple[str, list[Member]]:
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    rows = list(wb.active.iter_rows(values_only=True))
    as_of = None
    header_idx = None
    for i, r in enumerate(rows[:20]):
        if r and r[0] and str(r[0]).startswith("Holdings"):
            m = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4})", str(r[1] or ""))
            if m:
                as_of = _parse_date(m.group(1))
        if r and "Ticker" in [str(c) for c in r if c]:
            header_idx = i
            break
    if header_idx is None or as_of is None:
        raise SourceError("SPDR holdings file: header/date not found")
    hdr = [str(c) if c else "" for c in rows[header_idx]]
    ix = {h: hdr.index(h) for h in hdr if h}
    out: list[Member] = []
    for r in rows[header_idx + 1:]:
        if not r or not r[ix["Ticker"]]:
            continue
        ticker = str(r[ix["Ticker"]]).strip()
        if not ticker.endswith("-AU"):
            continue
        code = ticker[:-3]
        if not _CODE_RE.match(code):
            continue
        out.append(Member(
            code=code,
            name=str(r[ix["Name"]]).strip() if "Name" in ix else None,
            weight=_num(r[ix["Weight (%)"]]) if "Weight (%)" in ix else None,
            sector=normalise_sector(str(r[ix["Sector Classification"]])) if "Sector Classification" in ix else None,
        ))
    return as_of, out


def parse_ishares_csv(data: bytes) -> tuple[str, list[Member]]:
    text = data.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    m = re.search(r'"(\d{1,2}-[A-Za-z]{3}-\d{4})"', lines[0])
    if not m:
        raise SourceError("iShares holdings: date not found")
    as_of = _parse_date(m.group(1))
    start = next(i for i, ln in enumerate(lines) if ln.startswith("Ticker,"))
    out: list[Member] = []
    for row in csv.DictReader(lines[start:]):
        code = (row.get("Ticker") or "").strip()
        if row.get("Asset Class") != "Equity" or not _CODE_RE.match(code):
            continue
        out.append(Member(code=code, name=row.get("Name"), weight=_num(row.get("Weight (%)")),
                          sector=normalise_sector(row.get("Sector"))))
    return as_of, out


@dataclass
class DirectoryEntry:
    code: str
    name: str
    industry_group: str | None
    sector: str | None
    listing_date: str | None
    market_cap: float | None


def parse_directory_csv(data: bytes) -> list[DirectoryEntry]:
    text = data.decode("utf-8-sig", errors="replace")
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        code = (row.get("ASX code") or "").strip()
        if not _CODE_RE.match(code):
            continue
        group = (row.get("GICs industry group") or "").strip() or None
        ld = (row.get("Listing date") or "").strip()
        try:
            listing = _parse_date(ld) if ld else None
        except ValueError:
            listing = None
        out.append(DirectoryEntry(
            code=code,
            name=(row.get("Company name") or "").strip(),
            industry_group=group,
            sector=sector_for_group(group),
            listing_date=listing,
            market_cap=_num(row.get("Market Cap")),
        ))
    return out


def fetch_directory(http: HttpClient) -> list[DirectoryEntry]:
    return parse_directory_csv(http.get(DIRECTORY_URL, cache_ttl=6 * 3600))


def _top_by_weight(snap: IndexSnapshot, n: int, index_code: str) -> IndexSnapshot:
    members = sorted(snap.members, key=lambda m: m.weight or 0, reverse=True)[:n]
    return IndexSnapshot(index_code, snap.as_of, f"top{n}-of-{snap.source}", members, approximate=True)


def fetch_asx200(http: HttpClient) -> IndexSnapshot:
    try:
        as_of, members = parse_spdr_xlsx(http.get(SPDR_URL.format(fund="stw"), cache_ttl=6 * 3600))
        if len(members) >= 180:
            return IndexSnapshot("asx200", as_of, "spdr-stw", members)
        log.warning("STW returned only %d members; trying IOZ", len(members))
    except SourceError as e:
        log.warning("STW holdings failed: %s; trying IOZ", e)
    as_of, members = parse_ishares_csv(http.get(IOZ_URL, cache_ttl=6 * 3600))
    if len(members) < 180:
        raise SourceError(f"IOZ returned only {len(members)} members")
    return IndexSnapshot("asx200", as_of, "ishares-ioz", members)


def fetch_index(http: HttpClient, index_code: str) -> IndexSnapshot:
    if index_code == "asx200":
        return fetch_asx200(http)
    if index_code == "asx50":
        try:
            as_of, members = parse_spdr_xlsx(http.get(SPDR_URL.format(fund="sfy"), cache_ttl=6 * 3600))
            if len(members) >= 45:
                return IndexSnapshot("asx50", as_of, "spdr-sfy", members)
        except SourceError as e:
            log.warning("SFY holdings failed: %s; approximating", e)
        return _top_by_weight(fetch_asx200(http), 50, "asx50")
    if index_code == "asx100":
        return _top_by_weight(fetch_asx200(http), 100, "asx100")
    if index_code == "asx300":
        base = fetch_asx200(http)
        directory = fetch_directory(http)
        in200 = {m.code for m in base.members}
        extra = sorted(
            (d for d in directory
             if d.code not in in200 and d.market_cap
             and (d.industry_group or "").lower() not in EXCLUDED_GROUPS),
            key=lambda d: d.market_cap or 0, reverse=True,
        )[: max(0, 300 - len(base.members))]
        members = list(base.members) + [Member(d.code, d.name, None, d.sector) for d in extra]
        return IndexSnapshot("asx300", base.as_of, f"{base.source}+directory-mcap", members, approximate=True)
    raise ValueError(f"unknown index {index_code}")

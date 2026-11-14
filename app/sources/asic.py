"""ASIC daily aggregated short position reports.

Index: short-selling-data.json lists every report {date, version}.
File:  RR{YYYYMMDD}-{version}-SSDailyAggShortPos.csv (tab-separated; older
files are UTF-16 with BOM, newer are ASCII/UTF-8).
ASIC publishes each report a few business days after the report date; we set
available_at = report date + PUBLICATION_LAG_BDAYS so backtests can't peek.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.sources.base import HttpClient, SourceError

INDEX_URL = "https://download.asic.gov.au/short-selling/short-selling-data.json"
FILE_URL = "https://download.asic.gov.au/short-selling/RR{date}-{version}-SSDailyAggShortPos.csv"
PUBLICATION_LAG_BDAYS = 4


@dataclass
class ShortRow:
    code: str
    short_qty: float | None
    issued_qty: float | None
    short_pct: float | None


def add_bdays(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def parse_index(data: bytes) -> list[tuple[str, str]]:
    """-> [(YYYY-MM-DD, version)] newest first, keeping the highest version per date."""
    best: dict[str, str] = {}
    for e in json.loads(data):
        d = datetime.strptime(str(e["date"]), "%Y%m%d").date().isoformat()
        v = str(e.get("version", "001"))
        if d not in best or v > best[d]:
            best[d] = v
    return sorted(best.items(), reverse=True)


def _decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _f(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.strip().replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_daily(data: bytes) -> list[ShortRow]:
    text = _decode(data)
    if not text.strip():
        return []          # ASIC publishes 0-byte files for some holidays (e.g. 2017-10-02)
    first = text.splitlines()[0] if text else ""
    delim = "\t" if "\t" in first else ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    header = [h.strip().lower() for h in next(reader, [])]
    try:
        i_code = next(i for i, h in enumerate(header) if h == "product code")
        i_short = next(i for i, h in enumerate(header) if h.startswith("reported short"))
        i_issue = next(i for i, h in enumerate(header) if h.startswith("total product"))
        i_pct = next(i for i, h in enumerate(header) if h.startswith("%"))
    except StopIteration:
        raise SourceError(f"ASIC: unexpected header {header}")
    out = []
    for r in reader:
        if len(r) <= max(i_code, i_short, i_issue, i_pct):
            continue
        code = r[i_code].strip().upper()
        if not code:
            continue
        out.append(ShortRow(code, _f(r[i_short]), _f(r[i_issue]), _f(r[i_pct])))
    return out


def fetch_index(http: HttpClient) -> list[tuple[str, str]]:
    return parse_index(http.get(INDEX_URL))


def fetch_daily(http: HttpClient, day: str, version: str) -> list[ShortRow]:
    url = FILE_URL.format(date=day.replace("-", ""), version=version)
    return parse_daily(http.get(url, cache_ttl=365 * 86400))

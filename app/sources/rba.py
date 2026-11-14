"""Reserve Bank of Australia statistical tables (CSV).

A2: cash rate target changes (effective date, new target).
F11.1: daily exchange rates (A$1=USD is series FXRUSD).
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime

from app.sources.base import HttpClient, SourceError

A2_URL = "https://www.rba.gov.au/statistics/tables/csv/a2-data.csv"
F11_URL = "https://www.rba.gov.au/statistics/tables/csv/f11.1-data.csv"


def _date(s: str) -> str | None:
    s = s.strip()
    for fmt in ("%d-%b-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_rba_csv(data: bytes, series_id: str) -> list[tuple[str, str]]:
    """Return [(date, raw value string)] for one series."""
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    try:
        hdr_i = next(i for i, r in enumerate(rows) if r and r[0].strip() == "Series ID")
    except StopIteration:
        raise SourceError("RBA CSV: no 'Series ID' row")
    try:
        col = [c.strip() for c in rows[hdr_i]].index(series_id)
    except ValueError:
        raise SourceError(f"RBA CSV: series {series_id} not found")
    out = []
    for r in rows[hdr_i + 1:]:
        if len(r) <= col or not r[0].strip():
            continue
        d = _date(r[0])
        v = r[col].strip()
        if d and v:
            out.append((d, v))
    return out


def _last_number(s: str) -> float | None:
    nums = re.findall(r"-?\d+(?:\.\d+)?", s)
    return float(nums[-1]) if nums else None


def parse_cash_rate(data: bytes) -> list[tuple[str, float]]:
    """Cash rate target at each change date. Ranges like '17.00 to 17.50'
    (pre-1990s) use the upper bound."""
    out = []
    for d, v in parse_rba_csv(data, "ARBAMPCNCRT"):
        n = _last_number(v)
        if n is not None:
            out.append((d, n))
    return out


def parse_audusd(data: bytes) -> list[tuple[str, float]]:
    out = []
    for d, v in parse_rba_csv(data, "FXRUSD"):
        try:
            out.append((d, float(v)))
        except ValueError:
            pass
    return out


def fetch_cash_rate(http: HttpClient) -> list[tuple[str, float]]:
    return parse_cash_rate(http.get(A2_URL))


def fetch_audusd(http: HttpClient) -> list[tuple[str, float]]:
    return parse_audusd(http.get(F11_URL))

"""Director trades from Appendix 3Y ("Change of Director's Interest Notice").

Best-effort text extraction from the standard ASX form. Anything we can't
parse cleanly is stored with trade_type='unparsed' and a link, never guessed.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import datetime

from pypdf import PdfReader

_NUM = r"(\d[\d,]*(?:\.\d+)?)"


@dataclass
class DirectorTrade:
    director: str | None
    change_date: str | None
    security_class: str | None
    acquired: float | None
    disposed: float | None
    value: float | None
    nature: str | None
    trade_type: str          # buy | sell | other | unparsed
    on_market: bool
    parse_ok: bool


def pdf_to_text(data: bytes, max_pages: int = 8) -> str:
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages[:max_pages])


def _clean(s: str) -> str:
    s = re.sub(r"For personal use only", " ", s)
    s = re.sub(r"\+ See chapter 19 for defined terms\.", " ", s)
    s = re.sub(r"Appendix 3Y\s+Page \d+\s+\d\d/\d\d/\d{4}|\d\d/\d\d/\d{4}\s+Appendix 3Y\s+Page \d+", " ", s)
    s = re.sub(r"Appendix 3Y\s*\n\s*Change of Director.s Interest Notice", " ", s)
    return re.sub(r"[ \t ]+", " ", s)


def _between(text: str, start: str, ends: list[str]) -> str | None:
    m = re.search(start, text, re.I)
    if not m:
        return None
    rest = text[m.end():]
    cut = len(rest)
    for e in ends:
        m2 = re.search(e, rest, re.I)
        if m2 and m2.start() < cut:
            cut = m2.start()
    return rest[:cut].strip()


def _first_number(s: str | None) -> float | None:
    if not s:
        return None
    if re.match(r"^\s*(nil|n/?a|-)\b", s, re.I):
        return 0.0
    m = re.search(_NUM, s)
    return float(m.group(1).replace(",", "")) if m else None


def _sum_numbers(s: str | None) -> float | None:
    """'Indirect 1 30,000,000' -> 30000000. Sums 'Direct x / Indirect y' parts,
    ignoring the small integers that label Indirect 1/2."""
    if not s:
        return None
    if re.match(r"^\s*(nil|n/?a|-)\s*$", s, re.I):
        return 0.0
    s2 = re.sub(r"\b(Indirect|Direct)\s+\d\b", " ", s, flags=re.I)
    s2 = re.sub(r"(?<![\d.])\d{1,2}\.(?=\s)", " ", s2)       # list labels "1." "2."
    s2 = re.sub(r"\(\w\)", " ", s2)
    nums = [float(x.replace(",", "")) for x in re.findall(_NUM, s2)]
    if not nums:
        return 0.0 if re.search(r"\bnil\b", s, re.I) else None
    return sum(nums)


_MONEY_RE = re.compile(r"(?:A\$|AU\$|AUD\s?\$?|\$)\s?" + _NUM, re.I)
_PER_UNIT_RE = re.compile(r"^\s*(?:per|/|each|a)\s*(?:share|security|unit|ordinary|right|option)?", re.I)


def _money(s: str | None, qty: float | None = None) -> float | None:
    """Total consideration. Handles '$101,250', 'A$100,000 (average of A$3.77 per Share)',
    'AUD 2.255 per share' (multiplied by qty)."""
    if not s:
        return None
    m = _MONEY_RE.search(s)
    if not m:
        return 0.0 if re.search(r"\bnil\b", s, re.I) else None
    amount = float(m.group(1).replace(",", ""))
    decimals = len(m.group(1).split(".")[1]) if "." in m.group(1) else 0
    # Totals rarely carry >2 decimals; '$1.558691' is an average price per share.
    if _PER_UNIT_RE.match(s[m.end():m.end() + 20]) or decimals > 2:
        return round(amount * qty, 2) if qty else None
    return amount


def _date(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip().split("\n")[0]
    for fmt in ("%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%d/%m/%y", "%d-%b-%Y", "%d-%m-%Y", "%B %d, %Y"):
        m = re.search(r"\d{1,2}(?:st|nd|rd|th)?[ /\-][A-Za-z0-9]+[ /\-]\d{2,4}", s)
        if not m:
            break
        cand = re.sub(r"(\d)(st|nd|rd|th)", r"\1", m.group(0))
        try:
            return datetime.strptime(cand, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_appendix_3y(raw: str) -> DirectorTrade:
    t = _clean(raw)
    director = _between(t, r"Name of Director", [r"Date of last notice", r"\n"])
    change_date = _date(_between(t, r"Date of change", [r"No\. of securities held prior", r"\n\s*\n"]))
    sec_class = _between(t, r"\bClass\b", [r"Number acquired"])
    acq_raw = _between(t, r"Number acquired", [r"Number disposed"])
    dis_raw = _between(t, r"Number disposed", [r"Value/?\s*Consideration"])
    val_raw = _between(t, r"Value/?\s*Consideration", [r"No\. of securities held after"])
    if val_raw:
        val_raw = re.sub(r"Note:\s*If consideration.*?valuation", " ", val_raw, flags=re.I | re.S)
    nature = _between(t, r"(?m)^\s*Nature of change", [r"Part 2", r"Detail of contract"])
    if nature:
        nature = re.sub(
            r"Example:.*?buy\s*-?\s*back", " ", nature, flags=re.I | re.S
        )
        nature = re.sub(r"\s+", " ", nature).strip()

    acquired = _sum_numbers(acq_raw)
    disposed = _sum_numbers(dis_raw)
    on_market = bool(nature and re.search(r"on[\s-]?market", nature, re.I)
                     and not re.search(r"off[\s-]?market", nature, re.I))
    traded = f"{sec_class or ''} {acq_raw or ''} {dis_raw or ''}".lower()
    mentions_shares = bool(re.search(r"ordinary|fully paid|\bshares?\b|\bfpo\b|\bunits?\b|stapled", traded))
    mentions_derivs = bool(re.search(r"right|option|warrant", traded))
    # On-market trades are of quoted securities; unless only rights/options are named, treat as shares.
    is_ordinary = mentions_shares or (on_market and not mentions_derivs)
    qty = (acquired or 0) if (acquired or 0) > 0 else (disposed or 0)
    value = _money(val_raw, qty or None)

    plan_related = bool(nature and re.search(
        r"\b(grant(ed)?|incentive|STI|LTI|plan|vest(ed|ing)?|dividend reinvestment|DRP|"
        r"issue of|allocation|trustee|salary sacrifice)\b", nature, re.I))
    parse_ok = director is not None and (acquired is not None or disposed is not None)
    if not parse_ok:
        trade_type = "unparsed"
    elif plan_related:
        trade_type = "other"   # employee/dividend plan acquisitions aren't discretionary trades
    elif on_market and is_ordinary and (acquired or 0) > 0 and not disposed:
        trade_type = "buy"
    elif on_market and is_ordinary and (disposed or 0) > 0 and not acquired:
        trade_type = "sell"
    else:
        trade_type = "other"   # rights/options issues, DRP, vesting, off-market transfers
    return DirectorTrade(
        director=re.sub(r"\s+", " ", director).strip() if director else None,
        change_date=change_date,
        security_class=re.sub(r"\s+", " ", sec_class).strip()[:120] if sec_class else None,
        acquired=acquired, disposed=disposed, value=value,
        nature=(nature or "")[:300] or None,
        trade_type=trade_type, on_market=on_market, parse_ok=parse_ok,
    )


def is_3y_headline(headline: str) -> bool:
    h = headline.lower()
    return "director" in h and "interest" in h and "initial" not in h and "final" not in h

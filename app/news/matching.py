"""Match news text to ASX companies.

Signals, strongest first:
  1. explicit code: "ASX: BHP", "(ASX:CBA)", "ASX-listed PLS", or "PLS shares" / "PLS share price"
  2. full company name without legal suffix ("Origin Energy", "Commonwealth Bank")
  3. curated aliases ("CommBank", "Westpac", "Fortescue")
  4. a bare code that is also the brand (BHP, CSL, QBE...), as a capitalised token
  5. a short single-word name. Common words ("Seek", "Block", "Perpetual") also need
     exact capitalisation and market context nearby ("shares", "ASX", "stock"...)

Only companies passed in (the tracked universe) are matched by name; explicit
code patterns match any listed company.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

LEGAL_SUFFIXES = r"(?:limited|ltd|inc|plc|nl|corporation|corp|co|company|n\.?v|s\.?a)\.*"
GROUP_WORDS = ("group", "holdings", "holding", "enterprises", "industries", "corporation", "co")

# Curated aliases: how the press refers to companies.
ALIASES: dict[str, list[str]] = {
    "CBA": ["Commonwealth Bank", "CommBank", "Comm Bank"],
    "WBC": ["Westpac"],
    "NAB": ["National Australia Bank"],
    "ANZ": ["ANZ Bank", "ANZ Group"],
    "MQG": ["Macquarie Group", "Macquarie Bank", "Macquarie Capital"],
    "WDS": ["Woodside"],
    "TLS": ["Telstra"],
    "WOW": ["Woolworths"],
    "TCL": ["Transurban"],
    "ALL": ["Aristocrat"],
    "NST": ["Northern Star"],
    "EVN": ["Evolution Mining"],
    "FMG": ["Fortescue"],
    "S32": ["South32"],
    "IAG": ["Insurance Australia Group"],
    "SOL": ["Soul Patts", "Soul Pattinson"],
    "QAN": ["Qantas"],
    "MPL": ["Medibank"],
    "JHX": ["James Hardie"],
    "TLC": ["Lottery Corporation", "Lottery Corp"],
    "PLS": ["Pilbara Minerals"],
    "CAR": ["Carsales", "CAR Group"],
    "LNW": ["Light & Wonder"],
    "ALD": ["Ampol"],
    "SGH": ["Seven Group"],
    "MIN": ["Mineral Resources", "MinRes"],
    "JBH": ["JB Hi-Fi"],
    "A2M": ["a2 Milk"],
    "XYZ": ["Block Inc", "Afterpay"],
    "TWE": ["Treasury Wine"],
    "NHF": ["nib Holdings"],
    "TPG": ["TPG Telecom"],
    "ZIP": ["Zip Co"],
    "360": ["Life360"],
    "NWS": ["News Corp"],
    "DMP": ["Domino's Pizza Enterprises"],
    "LLC": ["Lendlease"],
    "HVN": ["Harvey Norman"],
    "FLT": ["Flight Centre"],
    "APE": ["Eagers Automotive"],
    "BOQ": ["Bank of Queensland"],
    "BEN": ["Bendigo Bank", "Bendigo and Adelaide Bank"],
    "ASX": ["ASX Ltd", "ASX Limited", "ASX operator", "ASX boss", "ASX chief executive"],
    "NXT": ["NextDC"],
    "WTC": ["WiseTech"],
    "STO": ["Santos Ltd"],
    "ORG": ["Origin Energy"],
    "SUN": ["Suncorp"],
    "CPU": ["Computershare"],
    "BXB": ["Brambles"],
    "GMG": ["Goodman Group"],
    "SCG": ["Scentre", "Westfield owner"],
    "AGL": ["AGL Energy"],
    "SEK": ["Seek Ltd"],
    "PPT": ["Perpetual Ltd"],
    "CGF": ["Challenger Ltd"],
    "NEC": ["Nine Entertainment"],
    "AIA": ["Auckland Airport"],
    "COH": ["Cochlear Ltd"],
    "SHL": ["Sonic Healthcare"],
    "RHC": ["Ramsay Health Care"],
}

# Codes that are also the brand people write in text.
BRAND_CODES = {"BHP", "CSL", "QBE", "IAG", "NAB", "ANZ", "CBA", "AGL", "AMP", "IGO", "REA", "BOQ", "A2M", "S32"}
# Upper-case tokens that look like codes but are usually something else.
NOT_CODES = {"ASX", "THE", "AND", "FOR", "ALL", "CEO", "CFO", "USD", "AUD", "GDP", "CPI", "IPO", "ETF", "RBA",
             "NEW", "TOP", "BUY", "AGM", "EPS", "ESG", "API", "GPT", "DOW", "CIA", "ABB", "AIA"}

# Single words that are ordinary English (or common surnames/places) in news text.
COMMON_WORDS = {
    "seek", "block", "sims", "elders", "region", "vault", "perpetual", "challenger", "zip", "goodman", "santos",
    "reece", "worley", "downer", "cochlear", "chorus", "steadfast", "macquarie", "sigma", "vicinity", "evolution",
    "origin", "nine", "domino's", "dexus", "arena", "waypoint", "judo", "resolute", "premier", "pinnacle",
    "emerald", "genesis", "catalyst", "champion", "atlas", "service", "super", "news", "data", "capstone",
    "orora", "stockland", "amcor", "coles", "ansell", "metcash", "elevra", "firefly", "deterra", "perenti",
    "infratil", "codan", "austal", "iress", "imdex", "lovisa", "superloop", "ventia", "telix", "mesoblast",
}
# ...but these are distinctive enough on their own (checked against current headlines).
DISTINCTIVE = {"stockland", "amcor", "coles", "ansell", "metcash", "deterra", "perenti", "infratil", "codan",
               "austal", "iress", "imdex", "lovisa", "superloop", "ventia", "telix", "mesoblast", "orora", "dexus",
               "elevra"}

CONTEXT_RE = re.compile(r"\b(shares?|stock|ASX|listed|investors?|dividend|profit|results?|chief executive|CEO|"
                        r"board|market cap|company|ltd|limited|takeover|capital raising)\b", re.I)


def clean_name(name: str) -> str:
    n = re.sub(r"\s+", " ", name.strip().rstrip(".")).strip()
    n = re.sub(rf"(?:\s+{LEGAL_SUFFIXES})+$", "", n, flags=re.I).rstrip(" .,")
    n = re.sub(r"^the\s+", "", n, flags=re.I)
    return n


def _titlecase(n: str) -> str:
    return " ".join(w if (len(w) <= 3 and w.isupper() and w.isalpha() and w.lower() not in {"and", "of", "the"})
                    or any(ch.isdigit() for ch in w) else w.capitalize() for w in n.split(" "))


@dataclass
class Pattern:
    code: str
    regex: re.Pattern
    method: str
    confidence: float
    needs_context: bool = False


def build_patterns(companies: dict[str, str]) -> list[Pattern]:
    """companies: code -> official name (any case)."""
    pats: list[Pattern] = []
    for code, name in companies.items():
        names: set[str] = set()
        full = _titlecase(clean_name(name or ""))
        if full:
            names.add(full)
            words = full.split(" ")
            if len(words) > 1 and words[-1].lower() in GROUP_WORDS:
                names.add(" ".join(words[:-1]))
        for alias in ALIASES.get(code, []):
            pats.append(Pattern(code, re.compile(rf"(?<![\w&]){re.escape(alias)}(?![\w&])", re.I), "alias", 0.9))
        for n in names:
            if code == "ASX" or n.upper() == code:
                continue                                  # handled by code rules / aliases
            if " " in n:
                pats.append(Pattern(code, re.compile(rf"(?<![\w&]){re.escape(n)}(?![\w&])", re.I), "name", 0.9))
            elif len(n) >= 4:
                low = n.lower()
                ambiguous = low in COMMON_WORDS and low not in DISTINCTIVE
                # single words: case-sensitive (Capitalised or UPPER)
                rx = re.compile(rf"(?<![\w&])(?:{re.escape(n)}|{re.escape(n.upper())})(?!\w)")
                pats.append(Pattern(code, rx, "short_name", 0.6 if ambiguous else 0.8, needs_context=ambiguous))
        if code in BRAND_CODES:
            pats.append(Pattern(code, re.compile(rf"(?<![\w$.]){re.escape(code)}(?![\w-])"), "code", 0.85))
    return pats


_EXPLICIT = [
    re.compile(r"\(\s*ASX\s*[:：]\s*([A-Z0-9]{3})\s*\)"),
    re.compile(r"\bASX\s*[:：]\s*([A-Z0-9]{3})\b"),
    re.compile(r"\bASX-listed\s+([A-Z0-9]{3})\b"),
    re.compile(r"(?<![\w$])([A-Z0-9]{3})\s+(?i:shares|share price|stock)\b"),
]


def match(text: str, patterns: list[Pattern], listed: set[str]) -> dict[str, tuple[str, float]]:
    """Returns code -> (method, confidence), keeping the strongest signal per code."""
    out: dict[str, tuple[str, float]] = {}

    def keep(code: str, method: str, conf: float) -> None:
        if code not in out or conf > out[code][1]:
            out[code] = (method, conf)

    for rx in _EXPLICIT:
        for m in rx.finditer(text):
            code = m.group(1)
            if code in listed and code not in NOT_CODES:
                keep(code, "explicit_code", 1.0)
    has_context = bool(CONTEXT_RE.search(text))
    for p in patterns:
        if p.code in out and out[p.code][1] >= p.confidence:
            continue
        if p.needs_context and not has_context:
            continue
        if p.regex.search(text):
            keep(p.code, p.method, p.confidence)
    return out

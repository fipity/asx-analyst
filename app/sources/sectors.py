"""GICS industry group -> sector mapping (ASX files give industry groups)."""
from __future__ import annotations

SECTORS = [
    "Energy", "Materials", "Industrials", "Consumer Discretionary", "Consumer Staples",
    "Health Care", "Financials", "Information Technology", "Communication Services",
    "Utilities", "Real Estate",
]

_GROUP_TO_SECTOR = {
    "energy": "Energy",
    "materials": "Materials",
    "capital goods": "Industrials",
    "commercial & professional services": "Industrials",
    "transportation": "Industrials",
    "automobiles & components": "Consumer Discretionary",
    "consumer durables & apparel": "Consumer Discretionary",
    "consumer services": "Consumer Discretionary",
    "consumer discretionary distribution & retail": "Consumer Discretionary",
    "retailing": "Consumer Discretionary",
    "consumer staples distribution & retail": "Consumer Staples",
    "food & staples retailing": "Consumer Staples",
    "food, beverage & tobacco": "Consumer Staples",
    "household & personal products": "Consumer Staples",
    "health care equipment & services": "Health Care",
    "pharmaceuticals, biotechnology & life sciences": "Health Care",
    "banks": "Financials",
    "financial services": "Financials",
    "diversified financials": "Financials",
    "insurance": "Financials",
    "software & services": "Information Technology",
    "technology hardware & equipment": "Information Technology",
    "semiconductors & semiconductor equipment": "Information Technology",
    "telecommunication services": "Communication Services",
    "media & entertainment": "Communication Services",
    "media": "Communication Services",
    "utilities": "Utilities",
    "equity real estate investment trusts (reits)": "Real Estate",
    "real estate management & development": "Real Estate",
    "real estate": "Real Estate",
}

# Normalise names used by ETF files / Yahoo to the GICS sector list.
_SECTOR_ALIASES = {
    "communication": "Communication Services",
    "telecommunication": "Communication Services",
    "information technology": "Information Technology",
    "technology": "Information Technology",
    "consumer discretionary": "Consumer Discretionary",
    "consumer cyclical": "Consumer Discretionary",
    "consumer staples": "Consumer Staples",
    "consumer defensive": "Consumer Staples",
    "health": "Health Care",
    "financial": "Financials",
    "real estate": "Real Estate",
    "basic materials": "Materials",
    "materials": "Materials",
    "industrials": "Industrials",
    "energy": "Energy",
    "utilities": "Utilities",
}


def sector_for_group(group: str | None) -> str | None:
    if not group:
        return None
    return _GROUP_TO_SECTOR.get(group.strip().lower())


def normalise_sector(name: str | None) -> str | None:
    if not name:
        return None
    n = name.strip().lower()
    if name.strip() in SECTORS:
        return name.strip()
    for key, val in _SECTOR_ALIASES.items():
        if n.startswith(key):
            return val
    return None

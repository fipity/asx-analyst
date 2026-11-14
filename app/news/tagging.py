"""Rule-based event tags (company news + ASX announcement headlines) and macro tags.

Rules are deliberately conservative regexes on the headline (+ summary for news).
They are checked in tests against real ASX headlines and RSS items."""
from __future__ import annotations

import re

EVENT_LABELS = {
    "earnings": "Results",
    "guidance": "Guidance",
    "dividend": "Dividend",
    "raising": "Capital raising",
    "takeover": "Takeover / M&A",
    "management": "Management change",
    "legal": "Legal / regulatory",
    "broker": "Broker rating",
    "halt": "Trading halt",
    "buyback": "Buy-back",
    "quarterly": "Quarterly report",
    "holder": "Substantial holder",
}

_EVENT_RULES: list[tuple[str, str]] = [
    ("earnings", r"\b(half[- ]year(ly)? (results?|report|accounts)|full[- ]year (results?|report)|annual (results?|report)|"
                 r"appendix 4[de]|preliminary final report|(fy|h1|h2|1h|2h)\s?\d{2,4} (results?|earnings|profit)|"
                 r"(net )?profit (rises|jumps|falls|slumps|lifts|drops|surges|plunges|soars|up|down)|"
                 r"(record|bumper|interim|annual) (profit|earnings)|earnings (beat|miss|season)|results? (beat|miss))\b"),
    ("guidance", r"\b(guidance|outlook (update|statement)|profit (warning|upgrade|downgrade)|earnings (upgrade|downgrade|warning)|"
                 r"trading update|reaffirms|(cuts|lifts|raises|lowers|upgrades|downgrades|withdraws) (its )?(full[- ]year )?"
                 r"(forecast|outlook|guidance))\b"),
    ("dividend", r"\b(dividend|distribution (announcement|declared|rate|reinvestment)|special dividend|drp\b|"
                 r"dividend reinvestment)\b"),
    ("raising", r"\b(placement|capital raising|entitlement offer|rights issue|share purchase plan|\bspp\b|"
                r"institutional (offer|placement)|equity raising|raises \$\d|convertible notes?|"
                r"proposed issue of securities|cleansing (notice|statement))\b"),
    ("takeover", r"\b(takeover|scheme (of arrangement|booklet|implementation|meeting)|bidder'?s statement|"
                 r"target'?s statement|non-binding (indicative )?(offer|proposal)|acquisition of|to acquire|acquires|"
                 r"merger|binding offer|buyout|bid for)\b"),
    ("holder", r"\b(becoming a substantial holder|ceasing to be a substantial holder|change in substantial holding)\b"),
    ("management", r"\b(ceo|chief executive|managing director|chair(man|woman|person)?|cfo|chief financial officer|"
                   r"company secretary|non-executive director|board) (appointment|appointed|resigns?|resignation|"
                   r"retire(s|ment)?|departure|steps? down|to step down|change|succession|exits?)\b|"
                   r"\b(appoints|appointment of|resignation of|retirement of) (new )?(ceo|chief executive|managing director|"
                   r"chair|cfo|director)\b|\bceo (to )?(quit|depart|leave)s?\b|"
                   r"\bappointment of (an? )?(new )?(independent |non-executive |executive )*(director|chair\w*|ceo|cfo)\b"),
    ("legal", r"\b(asic|accc|apra|austrac|takeovers panel|class action|lawsuit|sued|court|penalty|fined?|"
              r"prosecut\w*|investigation|probe|infringement notice|enforceable undertaking|"
              r"legal proceedings|settlement|tribunal)\b"),
    ("broker", r"\b((upgrade[sd]?|downgrade[sd]?) (to|from) (buy|sell|hold|neutral|overweight|underweight|"
               r"outperform|underperform|equal[- ]weight|accumulate|reduce)|price target|target price|"
               r"initiates? coverage|buy rating|sell rating|hold rating|broker notes?|buy, hold, sell|"
               r"brokers? (say|tip|rate|upgrade|downgrade)|upgraded and downgraded|analysts? (say|tip|rate))\b"),
    ("halt", r"\b(trading halt|voluntary suspension|suspension from (official )?quotation|reinstatement to "
             r"(official )?quotation)\b"),
    ("buyback", r"\b(buy[- ]?back|appendix 3[cfe]\b|daily share buy[- ]?back)\b"),
    ("quarterly", r"\b(quarterly (activities|cash ?flow|report|update|production|activity)|appendix 4c|appendix 5b|"
                  r"(september|december|march|june) quarter (report|update|production|activities))\b"),
]
_EVENT_RX = [(k, re.compile(rx, re.I)) for k, rx in _EVENT_RULES]
# Phrases that look like an event but aren't (e.g. a "board" in "on board").
_NOT_BROKER = re.compile(r"\b(credit rating|rating agency)\b", re.I)

MACRO_LABELS = {
    "rates": "Interest rates", "inflation": "Inflation", "iron_ore": "Iron ore", "china": "China",
    "oil": "Oil & gas", "gold": "Gold", "aud": "Australian dollar", "housing": "Housing",
    "jobs": "Jobs", "consumer": "Consumer spending", "trade": "Trade & tariffs", "lithium": "Lithium",
}
# Which sectors each macro theme mostly touches (shown on the news page; used as a feature in phase 5).
MACRO_SECTORS = {
    "rates": ["Financials", "Real Estate", "Utilities"],
    "inflation": ["Consumer Staples", "Consumer Discretionary", "Financials"],
    "iron_ore": ["Materials"],
    "china": ["Materials", "Consumer Staples"],
    "oil": ["Energy"],
    "gold": ["Materials"],
    "aud": ["Materials", "Health Care", "Information Technology"],
    "housing": ["Financials", "Real Estate", "Consumer Discretionary"],
    "jobs": ["Consumer Discretionary", "Financials"],
    "consumer": ["Consumer Discretionary", "Consumer Staples"],
    "trade": ["Materials", "Industrials"],
    "lithium": ["Materials"],
}
_MACRO_RULES = {
    "rates": r"\b(rba|reserve bank|cash rate|interest rates?|rate (cut|hike|rise|decision)s?|monetary policy|fed\b|"
             r"federal reserve|bond yields?)\b",
    "inflation": r"\b(inflation|cpi|consumer price index|price pressures)\b",
    "iron_ore": r"\b(iron ore|steel(maker)?s?)\b",
    "china": r"\b(china|chinese|beijing)\b",
    "oil": r"\b(oil price|crude|brent|opec|lng|natural gas|petrol price)\b",
    "gold": r"\bgold (price|miners?|stocks|hits|record)\b|\bprice of gold\b",
    "aud": r"\b(australian dollar|aussie dollar|\baud\b)\b",
    "housing": r"\b(house prices?|housing (market|prices|approvals|construction)|home (loans?|prices|values)|"
               r"mortgages?|property (market|prices))\b",
    "jobs": r"\b(unemployment|jobless|labour market|employment (data|figures)|wages? (growth|price index))\b",
    "consumer": r"\b(retail sales|consumer (spending|confidence|sentiment)|household spending)\b",
    "trade": r"\b(tariffs?|trade war|exports?|imports?|trade (deal|surplus|deficit))\b",
    "lithium": r"\blithium\b",
}
_MACRO_RX = {k: re.compile(rx, re.I) for k, rx in _MACRO_RULES.items()}


def event_tags(text: str, kind: str = "news") -> list[str]:
    """kind: "news" or "ann" (ASX announcement headline; companies don't announce broker ratings)."""
    tags = [k for k, rx in _EVENT_RX if rx.search(text) and not (k == "broker" and kind == "ann")]
    if "broker" in tags and _NOT_BROKER.search(text) and not re.search(r"price target|buy, hold", text, re.I):
        tags.remove("broker")
    # "Dividend" in a capital-raising / DRP context is still a dividend event; quarterly reports are not results.
    if "quarterly" in tags and "earnings" in tags and not re.search(r"half|full[- ]year|annual|appendix 4[de]", text, re.I):
        tags.remove("earnings")
    return tags


def macro_tags(text: str) -> list[str]:
    return [k for k, rx in _MACRO_RX.items() if rx.search(text)]

"""Adapters' pure parsers against recorded real responses (tests/fixtures)."""
from datetime import date

import pandas as pd
import pytest

from app.sources import asic, asx, rba, universe, yahoo
from app.sources.appendix3y import is_3y_headline, parse_appendix_3y
from app.sources.base import NotFound, SourceError


# --- universe -----------------------------------------------------------------

def test_spdr_holdings(fixture_bytes):
    as_of, members = universe.parse_spdr_xlsx(fixture_bytes("spdr_stw.xlsx"))
    assert as_of == "2026-11-11"
    assert 195 <= len(members) <= 205
    bhp = next(m for m in members if m.code == "BHP")
    assert bhp.weight == pytest.approx(11.07, abs=0.01)
    assert bhp.sector == "Materials"
    assert all("-" not in m.code for m in members)


def test_ishares_holdings(fixture_bytes):
    as_of, members = universe.parse_ishares_csv(fixture_bytes("ishares_ioz.csv"))
    assert as_of == "2026-11-11"
    assert members[0].code == "BHP" and members[0].weight == pytest.approx(11.03)
    assert members[1].sector == "Financials"


def test_directory(fixture_bytes):
    entries = universe.parse_directory_csv(fixture_bytes("asx_directory.csv"))
    assert len(entries) > 20
    e = next(x for x in entries if x.code == "14D")
    assert e.industry_group == "Capital Goods" and e.sector == "Industrials"
    assert e.listing_date == "2018-09-12"


# --- ASX announcements -----------------------------------------------------------

def test_company_year_page(fixture_bytes):
    anns = asx.parse_announcements_html(fixture_bytes("asx_annc_year_bhp_2019.html"), code="BHP")
    assert len(anns) == 62
    first = next(a for a in anns if a.ids_id == "02188386")
    assert first.headline == "Andrew Mackenzie retirement date 31 March 2020"
    # 23/12/2019 8:29 am Sydney (AEDT, UTC+11) = 22/12 21:29 UTC
    assert first.published_at == "2019-12-22T21:29:00+00:00"
    assert first.pages == 4
    assert first.code == "BHP"
    assert "idsId=02188386" in first.url
    assert len({a.ids_id for a in anns}) == len(anns)


def test_today_page_codes_and_price_sensitive(fixture_bytes):
    anns = asx.parse_announcements_html(fixture_bytes("asx_today_anns.html"))
    assert len(anns) == 5
    assert sum(a.price_sensitive for a in anns) == 2
    assert anns[0].code == "VPR"
    assert anns[0].headline.startswith("Update - Notification of buy-back")


def test_pdf_url_from_terms_page():
    html = '<form><input name="pdfURL" value="https://announcements.asx.com.au/asxpdf/20191223/pdf/44ctp59zsg88l3.pdf"></form>'
    assert asx.parse_pdf_url(html).endswith("44ctp59zsg88l3.pdf")
    assert asx.parse_pdf_url("<html>nothing</html>") is None


def test_key_statistics(fixture_bytes):
    ks = asx.parse_key_statistics(fixture_bytes("asx_keystats_bhp.json"))
    assert ks["franking_pct"] == 100
    assert ks["dividend"]["ex_date"] == "2026-09-03"
    assert 0 < ks["dividend_yield"] < 0.2
    assert ks["revenue"] == 58760000000
    assert ks["revenue_growth"] == pytest.approx(58760 / 51262 - 1, rel=1e-6)


def test_key_statistics_empty():
    with pytest.raises(NotFound):
        asx.parse_key_statistics(b'{"data": {}}')


# --- Appendix 3Y -------------------------------------------------------------

def test_3y_rights_issue_is_not_a_trade(fixture_bytes):
    t = parse_appendix_3y(fixture_bytes("appendix3y_rights.txt").decode())
    assert t.director == "David Hainsworth"
    assert t.change_date == "2026-11-11"
    assert t.acquired == 30_000_000
    assert t.disposed == 0
    assert t.trade_type == "other"
    assert not t.on_market
    assert t.parse_ok


ON_MARKET_BUY = """
Appendix 3Y
Change of Director's Interest Notice
Name of entity Example Resources Limited
Name of Director Jane Citizen
Date of last notice 2 March 2026
Direct or indirect interest Direct
Date of change 14 October 2026
No. of securities held prior to change 100,000 ordinary shares
Class Fully paid ordinary shares
Number acquired 25,000
Number disposed Nil
Value/Consideration
Note: If consideration is non-cash, provide details and estimated valuation $101,250.00
No. of securities held after change 125,000 ordinary shares
Nature of change
Example: on-market trade, off-market trade, exercise of options, issue of
securities under dividend reinvestment plan, participation in buy-back
On-market trade
Part 2 - Change of director's interests in contracts
"""


def test_3y_on_market_buy():
    t = parse_appendix_3y(ON_MARKET_BUY)
    assert t.director == "Jane Citizen"
    assert t.change_date == "2026-10-14"
    assert (t.acquired, t.disposed, t.value) == (25_000, 0, 101_250)
    assert t.on_market and t.trade_type == "buy"


def test_3y_on_market_sell():
    text = (ON_MARKET_BUY.replace("Number acquired 25,000", "Number acquired Nil")
            .replace("Number disposed Nil", "Number disposed 40,000"))
    t = parse_appendix_3y(text)
    assert t.trade_type == "sell" and t.disposed == 40_000


def _form(cls, acq, dis, value, nature):
    return (f"Name of Director A Director\nDate of last notice 1 May 2026\nDate of change 10 November 2026\n"
            f"No. of securities held prior to change 1\nClass {cls}\nNumber acquired {acq}\n"
            f"Number disposed {dis}\nValue/Consideration\nNote: If consideration is non -cash, provide details "
            f"and estimated\nvaluation\n{value}\nNo. of securities held after change 2\n"
            f"Nature of change\nExample: on-market trade, off-market trade, exercise of options, issue\n"
            f"of securities under dividend reinvestment plan, participation in buy -\nback\n{nature}\nPart 2")


def test_3y_real_variants():
    # AMP 03165201: price per share in AUD, disposed N/A
    t = parse_appendix_3y(_form("Ordinary Shares", "25,000 Ordinary Shares", "N/A", "AUD 2.255 per share", "On-Market Trade"))
    assert (t.trade_type, t.acquired, t.disposed, t.value) == ("buy", 25_000, 0, 56_375.0)
    # APE 03165007: class refers elsewhere; shares named in "Number acquired"
    t = parse_appendix_3y(_form("As described above", "100,000 ordinary shares", "Nil", "$15.4583 per share", "On-market trade"))
    assert t.trade_type == "buy" and t.value == 1_545_830.0
    # ELV 03165044: numbered multi-class lists, total value with average price
    t = parse_appendix_3y(_form("1. Fully paid ordinary shares (Shares)\n2. Unlisted performance rights",
                                "1. 26,525 Shares\n2. Nil", "1. Nil\n2. Nil",
                                "A$100,000 (average of A$3.77 per Share)", "On-market purchase of Shares."))
    assert (t.trade_type, t.acquired, t.disposed, t.value) == ("buy", 26_525, 0, 100_000)
    # ELS: bare average price with many decimals
    t = parse_appendix_3y(_form("Ordinary shares", "32,000", "Nil", "$1.558691", "On-market Purchase"))
    assert t.value == pytest.approx(49_878.11)
    # TCL: value text mentions 'Nature of change'; shares bought on market by a plan trustee
    t = parse_appendix_3y(_form("Deferred STI Securities", "81,113 Deferred STI Securities", "Nil",
                                "N/A (see 'Nature of change' below)",
                                "Ms X was granted 81,113 Deferred STI Securities. The securities were purchased on market by the trustee"))
    assert t.trade_type == "other" and "granted" in t.nature
    # Rights issued off-market are not trades
    t = parse_appendix_3y(_form("Performance Rights", "509,253", "Nil", "Nil", "Issue of performance rights approved at AGM"))
    assert t.trade_type == "other"


def test_3y_garbage_is_unparsed():
    t = parse_appendix_3y("Scanned image, no text")
    assert t.trade_type == "unparsed" and not t.parse_ok


def test_3y_headline_filter():
    assert is_3y_headline("Change of Director's Interest Notice")
    assert is_3y_headline("Change of Director's Interest Notice - J Smith")
    assert not is_3y_headline("Final Director's Interest Notice")
    assert not is_3y_headline("Initial Director's Interest Notice")


# --- ASIC ---------------------------------------------------------------------

def test_asic_current_format(fixture_bytes):
    rows = asic.parse_daily(fixture_bytes("asic_20261106.csv"))
    assert len(rows) >= 15
    r = next(r for r in rows if r.code == "4DX")
    assert r.short_qty == 71631358 and r.issued_qty == 602339504
    assert r.short_pct == pytest.approx(11.89218996)


def test_asic_old_utf16_format(fixture_bytes):
    rows = asic.parse_daily(fixture_bytes("asic_20120629_utf16.csv"))
    assert rows and rows[0].code == "ABP"
    assert rows[0].short_qty == 848632


def test_asic_bad_header():
    with pytest.raises(SourceError):
        asic.parse_daily(b"foo\tbar\n1\t2\n")


def test_asic_empty_holiday_file():
    assert asic.parse_daily(b"") == []


def test_asic_index_keeps_highest_version():
    idx = asic.parse_index(b'[{"date":20261106,"version":"001"},{"date":20261106,"version":"002"},'
                           b'{"date":20261105,"version":"001"}]')
    assert idx == [("2026-11-06", "002"), ("2026-11-05", "001")]


def test_add_bdays_skips_weekend():
    assert asic.add_bdays(date(2026, 11, 6), 4) == date(2026, 11, 12)  # Fri -> Thu


# --- RBA ----------------------------------------------------------------------

def test_rba_cash_rate(fixture_bytes):
    rows = rba.parse_cash_rate(fixture_bytes("rba_a2.csv"))
    assert rows[0] == ("1990-01-23", 17.50)
    assert all(0 <= v < 20 for _, v in rows)
    assert rows[-1][0] >= "2025-01-01"


def test_rba_audusd(fixture_bytes):
    rows = rba.parse_audusd(fixture_bytes("rba_f11.csv"))
    assert rows[0] == ("2023-01-03", 0.6828)


def test_rba_missing_series(fixture_bytes):
    with pytest.raises(SourceError):
        rba.parse_rba_csv(fixture_bytes("rba_f11.csv"), "NOPE")


# --- Yahoo normalisation -----------------------------------------------------------

def test_yahoo_normalise_history():
    idx = pd.DatetimeIndex(["2026-11-10", "2026-11-11", "2026-11-12"]).tz_localize("Australia/Sydney")
    df = pd.DataFrame({
        "Open": [1, 2, 3], "High": [1, 2, 3], "Low": [1, 2, 3], "Close": [1.0, float("nan"), 3.0],
        "Adj Close": [0.9, 1.9, 2.9], "Volume": [10, 20, 30],
        "Dividends": [0, 0.5, 0], "Stock Splits": [0, 0, 2],
    }, index=idx)
    prices, divs, splits = yahoo.normalise_history(df)
    assert [p["date"] for p in prices] == ["2026-11-10", "2026-11-12"]   # NaN close skipped
    assert divs == [{"ex_date": "2026-11-11", "amount": 0.5}]
    assert splits == [{"date": "2026-11-12", "ratio": 2.0}]


def test_yahoo_map_info_percent_yield():
    m = yahoo.map_info({"marketCap": 1e9, "dividendYield": 4.0, "freeCashflow": 5e7, "sector": "Basic Materials"})
    assert m["dividend_yield"] == pytest.approx(0.04)
    # sub-1% yields are percent too (NEM: 0.85 -> 0.0085); P/E sentinel dropped
    m2 = yahoo.map_info({"marketCap": 1e9, "dividendYield": 0.85, "trailingPE": -99999.99})
    assert m2["dividend_yield"] == pytest.approx(0.0085)
    assert m2["pe"] is None
    assert m["fcf_yield"] == pytest.approx(0.05)
    assert m["pe"] is None


def test_yahoo_map_calendar():
    ev = yahoo.map_calendar({"Earnings Date": [date(2027, 2, 15)], "Ex-Dividend Date": date(2026, 9, 3)})
    assert {"event_type": "earnings", "event_date": "2027-02-15"} in ev
    assert {"event_type": "ex_dividend", "event_date": "2026-09-03"} in ev

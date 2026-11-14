"""Phase 4: RSS parsing, dedupe, company matching, tags, sentiment pipeline."""
from pathlib import Path

import pytest

from app.db import get_conn, upsert
from app.news import matching, tagging, text
from app.sources import rss

NOW = "2026-11-13T00:00:00+00:00"
REAL_MODEL = Path(__file__).resolve().parents[1] / "data" / "models" / "finbert"


# --- RSS -----------------------------------------------------------------------

@pytest.mark.parametrize("fixture,key,min_items", [
    ("rss_abc_business.xml", "abc", 20), ("rss_guardian_au_business.xml", "guardian", 20),
    ("rss_smh_business.xml", "smh", 15), ("rss_fool_au.xml", "fool", 15), ("rba_media.xml", "rba", 1)])
def test_parse_feeds(fixture_bytes, fixture, key, min_items):
    items = rss.parse_feed(fixture_bytes(fixture), rss.FEEDS_BY_KEY[key])
    assert len(items) >= min_items
    for it in items:
        assert it["title"] and it["url"].startswith("http") and len(it["id"]) == 20
        assert it["published_at"] is None or it["published_at"].endswith("+00:00")
        assert len(it["summary"]) <= rss.SUMMARY_CHARS + 1
        assert "<" not in it["summary"] and "appeared first on" not in it["summary"]
        assert "breaking news email" not in it["summary"]


def test_fool_full_text_is_not_kept(fixture_bytes):
    raw = fixture_bytes("rss_fool_au.xml").decode()
    assert "content:encoded" in raw                       # the feed carries full articles...
    items = rss.parse_feed(fixture_bytes("rss_fool_au.xml"), rss.FEEDS_BY_KEY["fool"])
    assert max(len(i["summary"]) for i in items) <= rss.SUMMARY_CHARS + 1   # ...we keep a short summary


def test_plain_text():
    assert rss.plain_text("<p>Shares &amp; bonds</p> <b>up</b>") == "Shares & bonds up"
    assert rss.plain_text("word " * 100, 20).endswith("…") and len(rss.plain_text("word " * 100, 20)) <= 21


# --- dedupe --------------------------------------------------------------------

def test_simhash_dedupe():
    a = "Qantas to cut 850 Australian jobs and outsource work to India"
    b = "Qantas to cut 850 jobs and outsource work to India"
    c = "Westpac lifts profit as mortgage arrears ease"
    ha, hb, hc = (text.simhash(x) for x in (a, b, c))
    assert -(1 << 63) <= ha < (1 << 63)
    assert text.is_duplicate(a, ha, b, hb)
    assert not text.is_duplicate(a, ha, c, hc)
    assert text.is_duplicate("CBA sued!", 0, "cba  SUED", 12345)      # same normalised title
    assert text.hamming(5, 5) == 0 and text.hamming(0, -1) == 64
    fri, thu = "5 things to watch on the ASX 200 on Friday", "5 things to watch on the ASX 200 on Thursday"
    assert not text.is_duplicate(fri, text.simhash(fri), thu, text.simhash(thu), same_source=True)


# --- matching ------------------------------------------------------------------

UNIVERSE = {"BHP": "BHP GROUP LIMITED", "CBA": "COMMONWEALTH BANK OF AUSTRALIA.", "QAN": "QANTAS AIRWAYS LIMITED",
            "SEK": "SEEK LIMITED", "ORG": "ORIGIN ENERGY LIMITED", "XRO": "XERO LIMITED", "PLS": "PLS GROUP LIMITED",
            "GPT": "GPT GROUP", "ASX": "ASX LIMITED", "WBC": "WESTPAC BANKING CORPORATION", "CSL": "CSL LIMITED",
            "NXT": "NEXTDC LIMITED", "JBH": "JB HI-FI LIMITED", "PPT": "PERPETUAL LIMITED"}
LISTED = set(UNIVERSE) | {"ZZZ"}


@pytest.fixture(scope="module")
def pats():
    return matching.build_patterns(UNIVERSE)


@pytest.mark.parametrize("txt,expected", [
    ("Buy, hold, sell: CSL, NextDC, PLS shares", {"CSL", "NXT", "PLS"}),
    ("Commonwealth Bank allegedly overcharged business customers", {"CBA"}),
    ("CommBank and Westpac lift rates", {"CBA", "WBC"}),
    ("Why are Xero shares flying 12% higher today?", {"XRO"}),
    ("Small cap ZZZ (ASX: ZZZ) jumps 40%", {"ZZZ"}),                 # explicit code, outside the universe
    ("ASX 200 closes higher as banks rally", set()),                 # 'ASX' is the exchange, not ASX Ltd
    ("ASX Ltd fined over CHESS outage", {"ASX"}),
    ("ChatGPT maker unveils GPT-6 model", set()),                     # GPT the AI model, not GPT Group
    ("Companies seek higher prices as costs rise", set()),           # 'seek' the verb
    ("Seek shares slump after job ads fall", {"SEK"}),               # capitalised + market context
    ("Seek reveals new office", set()),                              # ambiguous word, no market context
    ("The origin of the crisis", set()),
    ("Origin Energy lifts guidance", {"ORG"}),
    ("A perpetual problem for investors", set()),                    # lower-case common word
    ("JB Hi-Fi sales jump", {"JBH"}),
    ("BHP and Rio Tinto ship more ore", {"BHP"}),
])
def test_matching(pats, txt, expected):
    assert set(matching.match(txt, pats, LISTED)) == expected


def test_clean_name():
    assert matching.clean_name("COMMONWEALTH BANK OF AUSTRALIA.") == "COMMONWEALTH BANK OF AUSTRALIA"
    assert matching.clean_name("ZIP CO LIMITED..") == "ZIP"
    assert matching.clean_name("THE A2 MILK COMPANY LIMITED") == "A2 MILK"


# --- tagging -------------------------------------------------------------------

@pytest.mark.parametrize("headline,kind,expected", [
    ("Half Year Results and Appendix 4D", "ann", {"earnings"}),
    ("Appendix 4E and Annual Report", "ann", {"earnings"}),
    ("Dividend/Distribution - CBA", "ann", {"dividend"}),
    ("Trading Halt", "ann", {"halt"}),
    ("Proposed issue of securities - SFR", "ann", {"raising"}),
    ("Entitlement Offer Cleansing Notice", "ann", {"raising"}),
    ("Becoming a substantial holder", "ann", {"holder"}),
    ("Scheme Implementation Deed signed", "ann", {"takeover"}),
    ("Upgrade to FY25 guidance and May trading update", "ann", {"guidance"}),
    ("Appointment of Non-Executive Director", "ann", {"management"}),
    ("CEO to step down", "ann", {"management"}),
    ("September 2026 Quarterly Activities Report", "ann", {"quarterly"}),
    ("Update - Notification of buy-back - TLS", "ann", {"buyback"}),
    ("Change of Director's Interest Notice", "ann", set()),
    ("6 ASX shares upgraded and downgraded by the experts this week", "news", {"broker"}),
    ("Macquarie Bank to pay $14m penalty after confusing promotion", "news", {"legal"}),
    ("Westpac shareholders file class action", "news", {"legal"}),
])
def test_event_tags(headline, kind, expected):
    assert set(tagging.event_tags(headline, kind)) == expected


def test_macro_tags():
    assert set(tagging.macro_tags("RBA holds cash rate as inflation eases")) == {"rates", "inflation"}
    assert "iron_ore" in tagging.macro_tags("Iron ore slides on weak Chinese steel demand")
    assert "china" in tagging.macro_tags("Iron ore slides on weak Chinese steel demand")
    assert tagging.macro_tags("Company appoints new chair") == []
    assert set(tagging.MACRO_LABELS) == set(tagging.MACRO_SECTORS)


# --- pipeline ------------------------------------------------------------------

class FakeModel:
    calls: list = []

    def __init__(self, threads=2):
        pass

    def predict(self, texts, batch_size=32):
        FakeModel.calls.append(list(texts))
        out = []
        for t in texts:
            neg = 0.9 if any(w in t.lower() for w in ("sued", "halt", "slump", "penalty")) else 0.05
            pos = 0.9 if any(w in t.lower() for w in ("record", "jump", "flying")) else 0.05
            out.append({"positive": pos, "negative": neg, "neutral": 1 - pos - neg if pos + neg < 1 else 0,
                        "label": "negative" if neg > pos else "positive" if pos > neg else "neutral", "score": pos - neg})
        return out

    def close(self):
        pass


@pytest.fixture
def seeded_companies():
    with get_conn() as c:
        upsert(c, "companies", [{"code": k, "name": v, "status": "listed", "sector": "Financials"}
                                for k, v in UNIVERSE.items()], ["code"])
        upsert(c, "index_membership", [{"index_code": "asx200", "code": k, "as_of": "2026-11-12", "weight": 1.0}
                                       for k in UNIVERSE], ["index_code", "as_of", "code"])


def test_rss_pipeline_dedupes_links_tags_and_scores(monkeypatch, seeded_companies):
    from app.jobs import JobContext
    from app.news import sentiment
    from app.pipeline import news

    feeds = {
        "abc": [{"id": "a1", "source": "abc", "title": "Commonwealth Bank sued over business fees",
                 "url": "https://x/a1", "summary": "CBA faces a class action.", "published_at": "2026-11-12T20:00:00+00:00",
                 "categories": None}],
        "smh": [{"id": "s1", "source": "smh", "title": "Commonwealth Bank sued over business fees, lawyers say",
                 "url": "https://x/s1", "summary": "", "published_at": "2026-11-12T21:00:00+00:00", "categories": None},
                {"id": "s2", "source": "smh", "title": "RBA holds cash rate steady as inflation cools",
                 "url": "https://x/s2", "summary": "", "published_at": None, "categories": None}],
        "fool": [{"id": "f1", "source": "fool", "title": "Why are Xero shares flying 12% higher today?",
                  "url": "https://x/f1", "summary": "Record result.", "published_at": "2026-11-12T22:00:00+00:00",
                  "categories": None}],
    }
    monkeypatch.setattr(rss, "fetch_feed", lambda http, feed: feeds.get(feed.key, []))
    ctx = JobContext("t", 0)
    new = news.step_rss(ctx, list(UNIVERSE))
    assert set(new) == {"a1", "s1", "s2", "f1"}
    assert news.step_rss(ctx, list(UNIVERSE)) == []              # idempotent
    with get_conn() as c:
        rows = {r["id"]: dict(r) for r in c.execute("SELECT * FROM news_items")}
        links = {(r[0], r[1]) for r in c.execute("SELECT item_id, code FROM news_links")}
        tags = {(r[0], r[1]) for r in c.execute("SELECT ref, tag FROM item_tags WHERE kind='news'")}
    assert rows["s1"]["cluster_id"] == "a1" and rows["a1"]["cluster_id"] == "a1"   # same story
    assert rows["s2"]["cluster_id"] == "s2"
    assert rows["a1"]["available_at"] >= rows["a1"]["first_seen_at"] >= rows["a1"]["published_at"] or \
        rows["a1"]["available_at"] == max(rows["a1"]["published_at"], rows["a1"]["first_seen_at"])
    assert {("a1", "CBA"), ("s1", "CBA"), ("f1", "XRO")} <= links
    assert ("a1", "legal") in tags and ("s2", "rates") in tags and ("s2", "inflation") in tags

    # sentiment: model missing -> skipped quietly
    ctx2 = JobContext("t", 0)
    assert news.step_sentiment(ctx2) == 0 and "not installed" in ctx2.notes[0]

    # announcements share the text cache: identical headlines are scored once
    with get_conn() as c:
        upsert(c, "announcements", [
            {"ids_id": f"0{i}", "code": "CBA", "published_at": f"2026-11-1{i % 3}T00:00:00+00:00",
             "headline": "Trading Halt", "price_sensitive": 1} for i in range(5)], ["ids_id"])
    monkeypatch.setattr(sentiment, "model_info", lambda: {"labels": ["positive", "negative", "neutral"]})
    monkeypatch.setattr(sentiment, "FinBert", FakeModel)
    FakeModel.calls = []
    assert news.step_tag_announcements(JobContext("t", 0)) == 5
    n = news.step_sentiment(JobContext("t", 0), limit=None)
    assert n == 9
    texts = [t for call in FakeModel.calls for t in call]
    assert texts.count("Trading Halt") == 1                      # deduped by text hash
    with get_conn() as c:
        s = dict(c.execute("SELECT n.id, t.score FROM news_items n JOIN text_sentiment t ON t.text_hash=n.sent_key"))
        assert s["a1"] < 0 < s["f1"]
        assert c.execute("SELECT COUNT(*) FROM announcements WHERE sent_key IS NULL").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM item_tags WHERE kind='ann' AND tag='halt'").fetchone()[0] == 5
    assert news.step_sentiment(JobContext("t", 0), limit=None) == 0     # nothing left


@pytest.mark.skipif(not (REAL_MODEL / "model.onnx").exists(), reason="FinBERT model not installed")
def test_real_finbert(monkeypatch):
    from app.news import sentiment
    monkeypatch.setattr(sentiment, "model_dir", lambda: REAL_MODEL)
    fb = sentiment.FinBert(threads=1)
    out = fb.predict(["Shares plunge as company slashes guidance and announces capital raising",
                      "Revenue rose 12 per cent, beating analyst expectations",
                      "Company announces date of annual general meeting"])
    assert [o["label"] for o in out] == ["negative", "positive", "neutral"]
    assert out[0]["score"] < -0.8 and out[1]["score"] > 0.8
    assert all(abs(o["positive"] + o["negative"] + o["neutral"] - 1) < 1e-5 for o in out)


def test_news_pages(monkeypatch, seeded_companies):
    from fastapi.testclient import TestClient

    from app.jobs import JobContext
    from app.main import app
    from app.pipeline import news

    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).replace(microsecond=0).isoformat()
    feeds = {
        "abc": [{"id": "a1", "source": "abc", "title": "Commonwealth Bank sued over business fees", "url": "https://x/a1",
                 "summary": "CBA faces a class action.", "published_at": now, "categories": None}],
        "guardian": [{"id": "g1", "source": "guardian", "title": "Commonwealth Bank sued over business fees, say lawyers",
                      "url": "https://x/g1", "summary": "", "published_at": now, "categories": None}],
        "smh": [{"id": "s2", "source": "smh", "title": "RBA holds cash rate steady as inflation cools",
                 "url": "https://x/s2", "summary": "", "published_at": now, "categories": None}],
    }
    monkeypatch.setattr(rss, "fetch_feed", lambda http, feed: feeds.get(feed.key, []))
    news.step_rss(JobContext("t", 0), list(UNIVERSE))
    with get_conn() as c:
        upsert(c, "announcements", [{"ids_id": "0900", "code": "CBA", "published_at": now, "headline": "Trading Halt",
                                     "price_sensitive": 1, "url": "https://x/ann"}], ["ids_id"])
    news.step_tag_announcements(JobContext("t", 0))
    with TestClient(app) as c:
        r = c.get("/news")
        assert r.status_code == 200 and "not financial advice" in r.text
        assert r.text.count('class="news-item"') == 2                          # duplicate story shown once
        assert "also:" in r.text and "Interest rates" in r.text and "Legal / regulatory" in r.text
        r = c.get("/news?scope=macro", headers={"HX-Request": "true", "HX-Target": "news-results"})
        assert "<html" not in r.text and "RBA holds" in r.text and "Commonwealth" not in r.text
        assert "Commonwealth" in c.get("/news?code=cba").text
        assert c.get("/api/news?event=legal").json()["count"] == 1
        assert c.get("/news?days=abc").status_code == 200                     # junk input tolerated
        page = c.get("/company/CBA").text
        assert "News &amp; sentiment" in page and "Commonwealth Bank sued" in page
        assert "Trading halt" in page                                          # event chip on the announcement
        assert "Market news" in c.get("/").text

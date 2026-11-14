"""HttpClient: retries, outages, stale cache, robots.txt."""
import httpx
import pytest

from app.sources.base import HttpClient, NotFound, SourceError


def make_client(handler, tmp_path, robots=False):
    return HttpClient("test", transport=httpx.MockTransport(handler), cache_dir=tmp_path / "c",
                      rate_limit=0, backoff=0, check_robots=robots)


def test_retries_then_succeeds(tmp_path):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(429 if calls["n"] < 3 else 200, content=b"ok")
    assert make_client(handler, tmp_path).get("https://x.test/a") == b"ok"
    assert calls["n"] == 3


def test_404_is_not_found(tmp_path):
    with pytest.raises(NotFound):
        make_client(lambda r: httpx.Response(404), tmp_path).get("https://x.test/a")


def test_outage_raises_source_error(tmp_path):
    def handler(req):
        raise httpx.ConnectError("down")
    with pytest.raises(SourceError):
        make_client(handler, tmp_path).get("https://x.test/a")


def test_stale_cache_served_during_outage(tmp_path):
    state = {"up": True}

    def handler(req):
        if state["up"]:
            return httpx.Response(200, content=b"cached")
        return httpx.Response(503)
    client = make_client(handler, tmp_path)
    assert client.get("https://x.test/a", cache_ttl=1) == b"cached"
    import os, time
    p = client._cache_path("https://x.test/a")
    os.utime(p, (time.time() - 100, time.time() - 100))   # expire it
    state["up"] = False
    assert client.get("https://x.test/a", cache_ttl=1) == b"cached"


def test_robots_disallow_blocks(tmp_path):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /rss\n")
        return httpx.Response(200, content=b"feed")
    client = make_client(handler, tmp_path, robots=True)
    assert client.get("https://x.test/ok") == b"feed"
    with pytest.raises(SourceError, match="robots"):
        client.get("https://x.test/rss/search?q=bhp")

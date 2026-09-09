"""Agentic V4 — constrained web-discovery adapter + SDK tool-catalog probe (prompt s.5)."""

from __future__ import annotations

from atlas.pilot.discovery_tools import (
    WebDiscoveryTools,
    is_trusted_official_url,
    probe_builtin_web_tools,
)


def test_sdk_has_no_web_builtins_so_fallback_applies():
    p = probe_builtin_web_tools()
    # The installed SDK (1.0.13) isolates a fixed builtin set with no web tools.
    assert p["web_search"] is False
    assert p["web_fetch"] is False
    assert p["fallback_adapter"] is True
    assert isinstance(p["builtin_isolated"], list)


def test_trust_gate_accepts_official_and_ats_only():
    assert is_trusted_official_url("https://careers.fiserv.com/job/1", "fiserv.com")
    assert is_trusted_official_url("https://fiserv.wd5.myworkdayjobs.com/EXT/job/x", "fiserv.com")
    assert is_trusted_official_url("https://boards.greenhouse.io/acme/jobs/1", "acme.com")
    # non-HTTPS and unrelated hosts are refused
    assert not is_trusted_official_url("http://careers.fiserv.com/job/1", "fiserv.com")
    assert not is_trusted_official_url("https://randomjobboard.example/job/1", "fiserv.com")


def _fake_fetcher(pages):
    def _f(url, accept):
        for key, (status, ct, body) in pages.items():
            if key in url:
                return status, ct, body
        return 404, "text/html", "<html>not found</html>"
    return _f


def test_web_fetch_official_returns_text_and_job_links():
    html = (
        "<html><body><h2>Careers</h2>"
        "<a href='https://careers.acme.com/job/100'>Java Backend Engineer</a>"
        "<a href='https://randomjobboard.example/job/9'>Off-domain</a>"
        "4&#43; years experience.</body></html>"
    )
    tools = WebDiscoveryTools(official_domain="acme.com",
                              fetcher=_fake_fetcher({"careers.acme.com/list": (200, "text/html", html)}))
    out = tools.web_fetch_official("https://careers.acme.com/list")
    assert out["status"] == 200
    assert "4+ years experience." in out["text"]  # entity decoded by normalizer
    urls = [l["url"] for l in out["official_job_links"]]
    assert "https://careers.acme.com/job/100" in urls
    assert "https://randomjobboard.example/job/9" not in urls  # off-domain filtered


def test_web_fetch_official_refuses_untrusted():
    tools = WebDiscoveryTools(official_domain="acme.com", fetcher=_fake_fetcher({}))
    out = tools.web_fetch_official("https://evil.example/job/1")
    assert "error" in out
    assert tools.calls[-1]["tool"] == "web_fetch_official"


def test_web_search_leads_unwraps_and_trust_flags():
    ddg = (
        "<html><body>"
        "<a class='result__a' href='//duckduckgo.com/l/?uddg=https%3A%2F%2Fcareers.acme.com%2Fjob%2F1'>Acme Java Job</a>"
        "<a class='result__a' href='//duckduckgo.com/l/?uddg=https%3A%2F%2Frandomjobboard.example%2Fjob%2F2'>Aggregator</a>"
        "</body></html>"
    )
    tools = WebDiscoveryTools(official_domain="acme.com",
                              fetcher=_fake_fetcher({"duckduckgo": (200, "text/html", ddg)}))
    out = tools.web_search_leads("Java Developer")
    urls = {l["url"]: l["trusted"] for l in out["leads"]}
    assert urls.get("https://careers.acme.com/job/1") is True
    assert urls.get("https://randomjobboard.example/job/2") is False
    assert out["trusted_count"] == 1
    # site-scoped query when a domain is known
    assert "site:acme.com" in out["query"]


def test_web_search_leads_degrades_when_unavailable():
    def _boom(url, accept):
        raise ConnectionError("network down")

    tools = WebDiscoveryTools(official_domain="acme.com", fetcher=_boom)
    out = tools.web_search_leads("Java")
    assert out["leads"] == []
    assert "unavailable" in out["limitation"]

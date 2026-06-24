"""Tests for site-map crawl discovery (issue #21)."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from link_coroner.scanner.site import (
    RobotsRules,
    discover_site,
    parse_robots,
)


def _run(coro):
    return asyncio.run(coro)


def test_parse_robots_wildcard_and_named():
    text = """
    User-agent: *
    Disallow: /private/
    Crawl-delay: 2

    User-agent: link-coroner
    Disallow: /draft/

    Sitemap: https://example.com/sitemap.xml
    Sitemap: https://example.com/news.xml
    # this is a comment
    """
    rules = parse_robots(text, user_agent="link-coroner/0.1")
    assert "/private/" in rules.disallowed
    assert "/draft/" in rules.disallowed
    assert rules.crawl_delay == 2.0
    assert rules.sitemaps == (
        "https://example.com/sitemap.xml",
        "https://example.com/news.xml",
    )
    assert not rules.allows("https://example.com/private/x")
    assert not rules.allows("https://example.com/draft/y")
    assert rules.allows("https://example.com/public/post")


def test_parse_robots_ignores_other_agents():
    rules = parse_robots(
        """
        User-agent: googlebot
        Disallow: /
        """,
        user_agent="link-coroner",
    )
    assert rules.disallowed == ()
    assert rules.allows("https://example.com/anything")


def test_robots_rules_empty_allows_everything():
    rules = RobotsRules()
    assert rules.allows("https://example.com/x")
    assert rules.allows("https://example.com/")


@respx.mock
def test_discover_uses_sitemap_and_respects_robots():
    base = "https://site.test"
    respx.get(f"{base}/robots.txt").mock(
        return_value=httpx.Response(
            200,
            text=(
                "User-agent: *\n"
                "Disallow: /secret/\n"
                f"Sitemap: {base}/sitemap.xml\n"
            ),
        )
    )
    sitemap_body = f"""<?xml version="1.0"?>
    <urlset>
      <url><loc>{base}/a</loc></url>
      <url><loc>{base}/secret/b</loc></url>
      <url><loc>{base}/c</loc></url>
    </urlset>"""
    respx.get(f"{base}/sitemap.xml").mock(
        return_value=httpx.Response(200, text=sitemap_body)
    )

    result = _run(discover_site(base))
    assert result.urls == [f"{base}/a", f"{base}/c"]
    assert not result.used_fallback
    assert f"{base}/sitemap.xml" in result.sitemaps_seen
    assert "/secret/" in result.robots.disallowed


@respx.mock
def test_discover_recurses_sitemap_index():
    base = "https://idx.test"
    respx.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
    index_body = f"""<?xml version="1.0"?>
    <sitemapindex>
      <sitemap><loc>{base}/sm-1.xml</loc></sitemap>
      <sitemap><loc>{base}/sm-2.xml</loc></sitemap>
    </sitemapindex>"""
    respx.get(f"{base}/sitemap.xml").mock(
        return_value=httpx.Response(200, text=index_body)
    )
    respx.get(f"{base}/sm-1.xml").mock(
        return_value=httpx.Response(
            200,
            text=f"<urlset><url><loc>{base}/one</loc></url></urlset>",
        )
    )
    respx.get(f"{base}/sm-2.xml").mock(
        return_value=httpx.Response(
            200,
            text=f"<urlset><url><loc>{base}/two</loc></url></urlset>",
        )
    )

    result = _run(discover_site(base))
    assert sorted(result.urls) == [f"{base}/one", f"{base}/two"]
    assert len(result.sitemaps_seen) == 3


@respx.mock
def test_discover_falls_back_to_homepage_crawl():
    base = "https://nomap.test"
    respx.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{base}/sitemap.xml").mock(return_value=httpx.Response(404))
    home = """<html><body>
      <a href="/about">about</a>
      <a href="https://nomap.test/contact">contact</a>
      <a href="https://other.test/leak">external</a>
      <a href="mailto:hi@example.com">mail</a>
    </body></html>"""
    respx.get(f"{base}/").mock(return_value=httpx.Response(200, text=home))

    result = _run(discover_site(base))
    assert result.used_fallback
    # Homepage itself, then same-host links; external dropped.
    assert f"{base}/" in result.urls
    assert f"{base}/about" in result.urls
    assert f"{base}/contact" in result.urls
    assert not any("other.test" in u for u in result.urls)


@respx.mock
def test_discover_dedupes_urls():
    base = "https://dup.test"
    respx.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
    body = (
        "<urlset>"
        f"<url><loc>{base}/x</loc></url>"
        f"<url><loc>{base}/x</loc></url>"
        f"<url><loc>{base}/y</loc></url>"
        "</urlset>"
    )
    respx.get(f"{base}/sitemap.xml").mock(return_value=httpx.Response(200, text=body))

    result = _run(discover_site(base))
    assert result.urls == [f"{base}/x", f"{base}/y"]


@respx.mock
def test_discover_handles_robots_fetch_failure_gracefully():
    base = "https://flaky.test"
    respx.get(f"{base}/robots.txt").mock(side_effect=httpx.ConnectError("nope"))
    respx.get(f"{base}/sitemap.xml").mock(
        return_value=httpx.Response(
            200, text=f"<urlset><url><loc>{base}/page</loc></url></urlset>"
        )
    )

    result = _run(discover_site(base))
    assert result.urls == [f"{base}/page"]
    assert result.robots.disallowed == ()


def test_discover_rejects_invalid_site():
    with pytest.raises(ValueError):
        _run(discover_site(""))


# --- CLI wiring ---

def test_cli_scan_site(monkeypatch):
    from typer.testing import CliRunner

    from link_coroner.cli import app
    from link_coroner.scanner import site as site_mod

    async def fake_discover(site, **kw):
        return site_mod.SiteDiscovery(
            urls=["https://x.test/a", "https://x.test/b"],
            sitemaps_seen=["https://x.test/sitemap.xml"],
            robots=RobotsRules(),
            used_fallback=False,
        )

    monkeypatch.setattr("link_coroner.cli.discover_site", fake_discover)
    result = CliRunner().invoke(app, ["scan", "--site", "https://x.test"])
    assert result.exit_code == 0, result.stdout
    assert "https://x.test/a" in result.stdout
    assert "https://x.test/b" in result.stdout
    assert "found" in result.stdout


def test_cli_scan_rejects_path_and_site(tmp_path):
    from typer.testing import CliRunner

    from link_coroner.cli import app

    (tmp_path / "x.md").write_text("https://nope.test")
    result = CliRunner().invoke(
        app, ["scan", str(tmp_path), "--site", "https://x.test"]
    )
    assert result.exit_code != 0

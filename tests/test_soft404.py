"""Tests for the M4 soft-404 + parked-domain heuristics."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from link_coroner.diagnosis import Cause, diagnose
from link_coroner.forensics import probe as probe_mod
from link_coroner.forensics.probe import ProbeConfig, Verdict, probe_urls
from link_coroner.forensics.soft404 import analyze_content


@pytest.fixture(autouse=True)
def _stub_dns(monkeypatch):
    async def _ok(host: str, timeout: float):
        return True, ""

    monkeypatch.setattr(probe_mod, "_resolve_dns", _ok)


def _run(coro):
    return asyncio.run(coro)


# ---- pure heuristic unit tests -----------------------------------------------


def test_analyze_content_detects_soft_404_phrase():
    body = """
    <html><head><title>Oops! Page not found</title></head>
    <body><h1>Sorry, that page doesn't exist.</h1>
    <p>Try going back to the homepage.</p></body></html>
    """
    v = analyze_content(body, content_type="text/html; charset=utf-8")
    assert v.suspicious is True
    assert v.reason == "SOFT_404"


def test_analyze_content_detects_parked_phrase():
    body = """
    <html><head><title>example.com - For Sale</title></head>
    <body><h1>This domain is for sale</h1>
    <p>Sponsored Listings related to example.com</p></body></html>
    """
    v = analyze_content(body, content_type="text/html")
    assert v.suspicious is True
    assert v.reason == "PARKED"


def test_analyze_content_detects_parked_via_final_url():
    v = analyze_content(b"", content_type="text/html", final_url="https://www.hugedomains.com/x")
    assert v.suspicious is True
    assert v.reason == "PARKED"


def test_analyze_content_healthy_page_is_not_suspicious():
    body = """
    <html><head><title>My Real Blog</title></head>
    <body><article>
    <p>Here is a substantive article about Python performance,
    with several paragraphs of actual content and links to other
    real pages on the same site. It easily clears the tiny-body
    threshold and contains no parker or 404 phrases at all.</p>
    <p>Another paragraph for good measure with more genuine text.</p>
    </article></body></html>
    """
    v = analyze_content(body, content_type="text/html")
    assert v.suspicious is False
    assert v.reason == ""


def test_analyze_content_non_html_is_ignored():
    v = analyze_content(b'{"ok":true}', content_type="application/json")
    assert v.suspicious is False


def test_analyze_content_empty_body_is_soft_404():
    v = analyze_content("", content_type="text/html")
    assert v.suspicious is True
    assert v.reason == "SOFT_404"


# ---- probe integration -------------------------------------------------------


@respx.mock
def test_probe_upgrades_soft_404_to_suspicious():
    soft = (
        "<html><head><title>404</title></head>"
        "<body><h1>Page not found</h1></body></html>"
    )
    respx.head("https://soft.example.com/x").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"})
    )
    respx.get("https://soft.example.com/x").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html"}, content=soft.encode("utf-8")
        )
    )

    results = _run(probe_urls(["https://soft.example.com/x"]))
    r = results[0]
    assert r.verdict is Verdict.UNREACHABLE
    assert r.reason == "SOFT_404"
    assert diagnose(r) is Cause.SOFT_404


@respx.mock
def test_probe_upgrades_parked_domain_to_suspicious():
    parked = (
        "<html><head><title>example.com</title></head>"
        "<body><h1>This domain is for sale</h1>"
        "<p>Sponsored Listings.</p></body></html>"
    )
    respx.head("https://parked.example.com/").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"})
    )
    respx.get("https://parked.example.com/").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html"}, content=parked.encode("utf-8")
        )
    )

    results = _run(probe_urls(["https://parked.example.com/"]))
    r = results[0]
    assert r.verdict is Verdict.UNREACHABLE
    assert r.reason == "PARKED"
    assert diagnose(r) is Cause.PARKED


@respx.mock
def test_probe_leaves_healthy_html_alone():
    healthy = (
        "<html><head><title>Real Site</title></head>"
        "<body><article><p>" + ("Genuine content. " * 50) + "</p></article></body></html>"
    )
    respx.head("https://ok.example.com/").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"})
    )
    respx.get("https://ok.example.com/").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html"}, content=healthy.encode("utf-8")
        )
    )

    results = _run(probe_urls(["https://ok.example.com/"]))
    assert results[0].verdict is Verdict.ALIVE
    assert results[0].reason == "HTTP_200"


@respx.mock
def test_probe_skips_sniff_for_non_html_2xx():
    respx.head("https://api.example.com/data.json").mock(
        return_value=httpx.Response(200, headers={"content-type": "application/json"})
    )

    results = _run(probe_urls(["https://api.example.com/data.json"]))
    assert results[0].verdict is Verdict.ALIVE
    # No follow-up GET should be needed; respx would have raised if an unmocked
    # GET went out.


@respx.mock
def test_probe_detect_soft_404_can_be_disabled():
    soft = "<html><body>Page not found</body></html>"
    respx.head("https://soft.example.com/").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"})
    )
    respx.get("https://soft.example.com/").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html"}, content=soft.encode("utf-8")
        )
    )

    cfg = ProbeConfig(detect_soft_404=False)
    results = _run(probe_urls(["https://soft.example.com/"], config=cfg))
    assert results[0].verdict is Verdict.ALIVE

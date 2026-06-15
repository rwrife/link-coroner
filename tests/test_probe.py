"""Tests for the basic autopsy probe (M2).

We avoid real DNS / network: respx mocks httpx, and we monkeypatch the DNS
resolver helper to a no-op so the probe stays offline.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from link_coroner.forensics import probe as probe_mod
from link_coroner.forensics.probe import ProbeConfig, Verdict, probe_urls


@pytest.fixture(autouse=True)
def _stub_dns(monkeypatch):
    async def _ok(host: str, timeout: float):
        return True, ""

    monkeypatch.setattr(probe_mod, "_resolve_dns", _ok)


def _run(coro):
    return asyncio.run(coro)


@respx.mock
def test_alive_dead_unreachable_mix():
    respx.head("https://alive.example.com/").mock(return_value=httpx.Response(200))
    respx.head("https://dead.example.com/page").mock(return_value=httpx.Response(404))
    respx.head("https://slow.example.com/").mock(side_effect=httpx.ConnectTimeout("nope"))

    urls = [
        "https://alive.example.com/",
        "https://dead.example.com/page",
        "https://slow.example.com/",
        "https://alive.example.com/",  # duplicate
    ]
    results = _run(probe_urls(urls, config=ProbeConfig(concurrency=4, per_host_concurrency=2)))

    assert [r.url for r in results] == urls[:3]
    by_url = {r.url: r for r in results}
    assert by_url["https://alive.example.com/"].verdict is Verdict.ALIVE
    assert by_url["https://alive.example.com/"].status_code == 200
    assert by_url["https://dead.example.com/page"].verdict is Verdict.DEAD
    assert by_url["https://dead.example.com/page"].status_code == 404
    assert by_url["https://slow.example.com/"].verdict is Verdict.UNREACHABLE
    assert by_url["https://slow.example.com/"].reason == "TIMEOUT"


@respx.mock
def test_head_405_falls_back_to_get():
    respx.head("https://picky.example.com/").mock(return_value=httpx.Response(405))
    respx.get("https://picky.example.com/").mock(return_value=httpx.Response(200))

    results = _run(probe_urls(["https://picky.example.com/"]))
    assert results[0].verdict is Verdict.ALIVE
    assert results[0].status_code == 200


@respx.mock
def test_alive_status_codes_include_401_403():
    respx.head("https://auth.example.com/").mock(return_value=httpx.Response(401))
    respx.get("https://auth.example.com/").mock(return_value=httpx.Response(401))

    results = _run(probe_urls(["https://auth.example.com/"]))
    assert results[0].verdict is Verdict.ALIVE
    assert results[0].status_code == 401


def test_empty_input_returns_empty_list():
    assert _run(probe_urls([])) == []


def test_dns_nxdomain_marks_unreachable(monkeypatch):
    async def _nxdomain(host: str, timeout: float):
        return False, "NXDOMAIN"

    monkeypatch.setattr(probe_mod, "_resolve_dns", _nxdomain)
    results = _run(probe_urls(["https://nope.invalid/"]))
    assert results[0].verdict is Verdict.UNREACHABLE
    assert results[0].reason == "NXDOMAIN"
    assert results[0].status_code is None


def test_to_dict_serializes_verdict_as_string():
    from link_coroner.forensics.probe import ProbeResult

    r = ProbeResult(url="https://x", verdict=Verdict.ALIVE, reason="HTTP_200", status_code=200)
    data = r.to_dict()
    assert data["verdict"] == "ALIVE"
    assert data["status_code"] == 200

"""Tests for the Wayback resurrection module (M5)."""

from __future__ import annotations

import asyncio

import httpx
import respx

from link_coroner.wayback import (
    AVAILABILITY_API,
    CDX_API,
    WaybackSnapshot,
    estimate_time_of_death,
    lookup_snapshot,
    resurrect_many,
)


def _run(coro):
    return asyncio.run(coro)


def test_lookup_snapshot_returns_closest():
    payload = {
        "url": "https://gone.example.com/",
        "archived_snapshots": {
            "closest": {
                "available": True,
                "url": "https://web.archive.org/web/20210101000000/https://gone.example.com/",
                "timestamp": "20210101000000",
                "status": "200",
            }
        },
    }
    with respx.mock(assert_all_called=False) as mock:
        mock.get(AVAILABILITY_API).respond(200, json=payload)
        snap = _run(lookup_snapshot("https://gone.example.com/"))
    assert snap.snapshot_url and "web.archive.org" in snap.snapshot_url
    assert snap.timestamp == "20210101000000"
    assert snap.archived_at and snap.archived_at.year == 2021


def test_lookup_snapshot_handles_no_results():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(AVAILABILITY_API).respond(200, json={"archived_snapshots": {}})
        snap = _run(lookup_snapshot("https://nope.example.com/"))
    assert snap.snapshot_url is None
    assert snap.timestamp is None


def test_lookup_snapshot_handles_http_error():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(AVAILABILITY_API).mock(side_effect=httpx.ConnectError("boom"))
        snap = _run(lookup_snapshot("https://err.example.com/"))
    assert snap.snapshot_url is None


def test_estimate_time_of_death_bisection():
    # newest -> oldest; the first 2xx/3xx row is the estimated TOD
    rows = [
        ["timestamp", "statuscode"],
        ["20250101000000", "404"],
        ["20240601000000", "404"],
        ["20240301000000", "200"],
        ["20230101000000", "200"],
    ]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(CDX_API).respond(200, json=rows)
        tod = _run(estimate_time_of_death("https://gone.example.com/"))
    assert tod is not None and tod.startswith("2024-03-01")


def test_estimate_time_of_death_returns_none_when_empty():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(CDX_API).respond(200, json=[["timestamp", "statuscode"]])
        tod = _run(estimate_time_of_death("https://gone.example.com/"))
    assert tod is None


def test_resurrect_many_collects_results():
    availability = {
        "archived_snapshots": {
            "closest": {
                "available": True,
                "url": "https://web.archive.org/web/20220202020202/https://a.example/",
                "timestamp": "20220202020202",
            }
        }
    }
    cdx = [
        ["timestamp", "statuscode"],
        ["20220101000000", "200"],
    ]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(AVAILABILITY_API).respond(200, json=availability)
        mock.get(CDX_API).respond(200, json=cdx)
        snaps = _run(
            resurrect_many(
                ["https://a.example/", "https://a.example/"],  # dedupe
                concurrency=2,
            )
        )
    assert list(snaps.keys()) == ["https://a.example/"]
    assert isinstance(snaps["https://a.example/"], WaybackSnapshot)
    assert snaps["https://a.example/"].snapshot_url
    assert snaps["https://a.example/"].time_of_death is not None


def test_resurrect_many_empty():
    assert _run(resurrect_many([])) == {}

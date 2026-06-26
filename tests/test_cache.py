"""Tests for the SQLite probe-history cache (issue #22)."""

from __future__ import annotations

from link_coroner.cache import SCHEMA_VERSION, ProbeCache, ProbeEvent, is_dead_verdict
from link_coroner.forensics.probe import ProbeResult, Verdict


def test_cache_creates_schema_and_round_trips(tmp_path):
    db = tmp_path / "cache.sqlite"
    with ProbeCache(db) as cache:
        results = [
            ProbeResult(
                url="https://dead.example.com/", verdict=Verdict.DEAD,
                reason="HTTP_404", status_code=404,
            ),
            ProbeResult(
                url="https://ok.example.com/", verdict=Verdict.ALIVE,
                reason="HTTP_200", status_code=200,
            ),
        ]
        inserted = cache.record_probe_results(
            results, observed_at=1_700_000_000,
            url_to_file={"https://dead.example.com/": "docs/index.md"},
        )
        assert inserted == 2

    with ProbeCache(db) as cache:
        events = cache.all_events()
        assert len(events) == 2
        by_url = {e.url: e for e in events}
        assert by_url["https://dead.example.com/"].file_path == "docs/index.md"
        assert by_url["https://dead.example.com/"].host == "dead.example.com"
        assert by_url["https://dead.example.com/"].verdict == "DEAD"
        # PRAGMA preserved across reopen
        version = cache._conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION


def test_iter_events_respects_window(tmp_path):
    cache = ProbeCache(tmp_path / "c.sqlite")
    cache.record_events([
        ProbeEvent("https://a/", "a", None, "DEAD", "HTTP_4XX", 1_000),
        ProbeEvent("https://b/", "b", None, "DEAD", "HTTP_4XX", 2_000),
        ProbeEvent("https://c/", "c", None, "DEAD", "HTTP_4XX", 3_000),
    ])
    inside = cache.all_events(since=1_500, until=2_500)
    assert [e.url for e in inside] == ["https://b/"]


def test_is_dead_verdict_helper():
    assert is_dead_verdict("DEAD")
    assert is_dead_verdict("UNREACHABLE")
    assert not is_dead_verdict("ALIVE")

"""Tests for `.coroner-ignore` quarantine config (issue #29)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from link_coroner.cli import app
from link_coroner.ignore import (
    IgnoreError,
    IgnoreState,
    append_ignore_entry,
    classify_urls,
    load_ignore_file,
    load_ignore_state,
    parse_duration,
    parse_ignore_text,
    save_ignore_state,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_duration_units() -> None:
    assert parse_duration("60s") == 60
    assert parse_duration("5m") == 300
    assert parse_duration("2h") == 7200
    assert parse_duration("1d") == 86400
    assert parse_duration("1w") == 604800
    assert parse_duration("42") == 42  # bare seconds


def test_parse_duration_rejects_garbage() -> None:
    with pytest.raises(IgnoreError):
        parse_duration("forever")
    with pytest.raises(IgnoreError):
        parse_duration("10y")


def test_parse_ignore_text_basic() -> None:
    text = """
    # comments are ignored
    https://example.invalid
    https://vendor.example.com/*  @ttl=30d
    *://intranet.local/*  # trailing comment
    """
    parsed = parse_ignore_text(text)
    assert len(parsed.rules) == 3
    assert parsed.rules[0].pattern == "https://example.invalid"
    assert parsed.rules[0].ttl_seconds is None
    assert parsed.rules[1].pattern == "https://vendor.example.com/*"
    assert parsed.rules[1].ttl_seconds == 30 * 86400
    assert parsed.rules[2].pattern == "*://intranet.local/*"


def test_parse_ignore_text_ttl_without_url_rejected() -> None:
    with pytest.raises(IgnoreError):
        parse_ignore_text("@ttl=10m")


def test_glob_matching() -> None:
    parsed = parse_ignore_text("https://vendor.example.com/*")
    rule = parsed.rules[0]
    assert rule.matches("https://vendor.example.com/foo")
    assert rule.matches("https://vendor.example.com/a/b/c")
    assert not rule.matches("https://other.example.com/foo")
    assert not rule.matches("http://vendor.example.com/foo")


def test_scheme_glob_matches_http_and_https() -> None:
    parsed = parse_ignore_text("*://intranet.local/*")
    rule = parsed.rules[0]
    assert rule.matches("http://intranet.local/x")
    assert rule.matches("https://intranet.local/x")
    assert not rule.matches("https://public.example.com/x")


# ---------------------------------------------------------------------------
# TTL state + classification
# ---------------------------------------------------------------------------


def test_classify_skips_quarantined_without_ttl() -> None:
    ignore = parse_ignore_text("https://example.invalid")
    state = IgnoreState()
    to_probe, quarantined = classify_urls(
        ["https://example.invalid", "https://ok.example.com"], ignore, state
    )
    assert to_probe == ["https://ok.example.com"]
    assert len(quarantined) == 1
    assert quarantined[0].url == "https://example.invalid"
    assert quarantined[0].recheck is False


def test_classify_reprobes_when_ttl_elapsed() -> None:
    ignore = parse_ignore_text("https://vendor.example.com/*  @ttl=60s")
    state = IgnoreState()
    url = "https://vendor.example.com/widget"
    # First call: never checked → must re-probe.
    to_probe, quarantined = classify_urls([url], ignore, state, now=1000.0)
    assert url in to_probe
    assert quarantined[0].recheck is True
    state.mark_checked(url, now=1000.0)

    # 30s later: TTL not elapsed → skip.
    to_probe, quarantined = classify_urls([url], ignore, state, now=1030.0)
    assert url not in to_probe
    assert quarantined[0].recheck is False

    # 120s later: TTL elapsed → re-probe.
    to_probe, quarantined = classify_urls([url], ignore, state, now=1200.0)
    assert url in to_probe
    assert quarantined[0].recheck is True


def test_ignore_state_roundtrip(tmp_path: Path) -> None:
    state = IgnoreState()
    state.mark_checked("https://x.example", now=1234.5)
    target = tmp_path / "state.json"
    save_ignore_state(state, target)

    loaded = load_ignore_state(target)
    assert loaded.last_checked == {"https://x.example": 1234.5}


def test_ignore_state_handles_corrupt(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("not-json", encoding="utf-8")
    loaded = load_ignore_state(target)
    assert loaded.last_checked == {}


# ---------------------------------------------------------------------------
# append_ignore_entry (`ignore add`)
# ---------------------------------------------------------------------------


def test_append_ignore_entry_creates_and_dedupes(tmp_path: Path) -> None:
    target = tmp_path / ".coroner-ignore"
    added, line = append_ignore_entry(target, "https://example.invalid")
    assert added is True
    assert line == "https://example.invalid"
    assert target.read_text(encoding="utf-8").endswith("https://example.invalid\n")

    added, _ = append_ignore_entry(target, "https://example.invalid")
    assert added is False

    added, _ = append_ignore_entry(target, "https://vendor.example.com/*", ttl="30d")
    assert added is True
    text = target.read_text(encoding="utf-8")
    assert "https://vendor.example.com/*  @ttl=30d" in text


def test_append_ignore_entry_validates_ttl(tmp_path: Path) -> None:
    target = tmp_path / ".coroner-ignore"
    with pytest.raises(IgnoreError):
        append_ignore_entry(target, "https://x", ttl="forever")


def test_append_ignore_entry_upserts_ttl(tmp_path: Path) -> None:
    target = tmp_path / ".coroner-ignore"
    append_ignore_entry(target, "https://vendor.example.com/*", ttl="30d")
    added, _ = append_ignore_entry(target, "https://vendor.example.com/*", ttl="7d")
    assert added is True
    text = target.read_text(encoding="utf-8")
    assert "@ttl=7d" in text
    assert "@ttl=30d" not in text


def test_load_ignore_file_missing_is_empty(tmp_path: Path) -> None:
    parsed = load_ignore_file(tmp_path / "nope")
    assert parsed.rules == []
    assert not parsed


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_ignore_add_creates_file(tmp_path: Path) -> None:
    target = tmp_path / ".coroner-ignore"
    result = runner.invoke(
        app,
        ["ignore", "add", "https://example.invalid", "--file", str(target)],
    )
    assert result.exit_code == 0, result.output
    assert "https://example.invalid" in target.read_text(encoding="utf-8")


def test_cli_ignore_add_with_ttl(tmp_path: Path) -> None:
    target = tmp_path / ".coroner-ignore"
    result = runner.invoke(
        app,
        [
            "ignore",
            "add",
            "https://vendor.example.com/*",
            "--ttl",
            "30d",
            "--file",
            str(target),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "@ttl=30d" in target.read_text(encoding="utf-8")


def test_cli_ignore_list(tmp_path: Path) -> None:
    target = tmp_path / ".coroner-ignore"
    target.write_text(
        "https://example.invalid\nhttps://vendor.example.com/*  @ttl=30d\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["ignore", "list", "--file", str(target)])
    assert result.exit_code == 0
    assert "https://example.invalid" in result.output
    assert "https://vendor.example.com/*" in result.output


def test_cli_autopsy_skips_quarantined(tmp_path: Path, monkeypatch) -> None:
    """A quarantined URL without TTL should be silently skipped and not fail CI."""
    repo = tmp_path
    (repo / "README.md").write_text(
        "See https://example.invalid for the dead one.\n", encoding="utf-8"
    )
    (repo / ".coroner-ignore").write_text(
        "https://example.invalid\n", encoding="utf-8"
    )

    # Stub probe_urls so the test doesn't hit the network — should never be
    # called because the only URL is quarantined.
    from link_coroner import cli as cli_mod

    async def fake_probe(urls, *, config):  # noqa: ANN001
        assert urls == [], f"quarantined URL leaked into probe: {urls}"
        return []

    monkeypatch.setattr(cli_mod, "probe_urls", fake_probe)

    result = runner.invoke(app, ["autopsy", str(repo), "--format", "json"])
    assert result.exit_code == 0, result.output
    assert "quarantined" in result.output.lower() or "QUARANTINED" in result.output


def test_cli_autopsy_broken_out_quarantine_fails(tmp_path: Path, monkeypatch) -> None:
    """A TTL-elapsed quarantined URL that probes ALIVE should fail the run."""
    repo = tmp_path
    (repo / "README.md").write_text(
        "See https://maybe.example.com here.\n", encoding="utf-8"
    )
    (repo / ".coroner-ignore").write_text(
        "https://maybe.example.com  @ttl=1s\n", encoding="utf-8"
    )

    from link_coroner import cli as cli_mod
    from link_coroner.forensics.probe import ProbeResult, Verdict

    async def fake_probe(urls, *, config):  # noqa: ANN001
        return [
            ProbeResult(
                url=u,
                verdict=Verdict.ALIVE,
                reason="HTTP_200",
                status_code=200,
            )
            for u in urls
        ]

    monkeypatch.setattr(cli_mod, "probe_urls", fake_probe)

    result = runner.invoke(app, ["autopsy", str(repo), "--format", "json"])
    assert result.exit_code != 0, result.output
    # The state file should also have been written next to the ignore file.
    assert (repo / ".coroner-ignore.state.json").is_file()

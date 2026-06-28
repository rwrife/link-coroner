"""Tests for the README badge generator (issue #27)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from link_coroner.cli import app
from link_coroner.reporting.badge import (
    BadgeSummary,
    color_for,
    render_markdown,
    render_shields_endpoint,
    render_svg,
    summarize,
    summarize_from_json,
)

runner = CliRunner()


def _item(cause: str, verdict: str | None = None) -> dict[str, str]:
    return {
        "cause": cause,
        "verdict": verdict
        or ("ALIVE" if cause == "ALIVE" else "DEAD" if cause.startswith("HTTP") else "UNREACHABLE"),
    }


def test_summarize_buckets_causes() -> None:
    summary = summarize(
        [
            _item("ALIVE"),
            _item("ALIVE"),
            _item("HTTP_4XX"),
            _item("SOFT_404"),
            _item("PARKED"),
        ]
    )
    assert summary.total == 5
    assert summary.alive == 2
    assert summary.dead == 1
    assert summary.suspicious == 2


def test_color_for_picks_worst_severity() -> None:
    assert color_for(BadgeSummary(0, 0, 0, 0)) == "brightgreen"
    assert color_for(summarize([_item("ALIVE")])) == "brightgreen"
    assert color_for(summarize([_item("ALIVE"), _item("SOFT_404")])) == "yellow"
    assert color_for(summarize([_item("ALIVE"), _item("HTTP_4XX")])) == "red"
    # Dead beats suspicious.
    assert (
        color_for(summarize([_item("HTTP_4XX"), _item("SOFT_404")])) == "red"
    )


def test_message_strings_are_terse() -> None:
    healthy = summarize([_item("ALIVE"), _item("ALIVE")])
    assert healthy.message == "🪦 0 dead / 2 alive"
    mixed = summarize([_item("ALIVE"), _item("HTTP_4XX"), _item("SOFT_404")])
    assert "1 dead" in mixed.message
    assert "1 suspicious" in mixed.message
    assert "1 alive" in mixed.message
    assert summarize([]).message == "no links"


def test_render_svg_is_valid_standalone_svg() -> None:
    svg = render_svg("link health", "🪦 0 dead / 5 alive", "brightgreen")
    assert svg.startswith("<svg")
    assert svg.rstrip().endswith("</svg>")
    # Self-contained — no external <image> / xlink:href references.
    assert "xlink:href" not in svg
    assert "http://" not in svg or "xmlns" in svg.split(">", 1)[0]
    # Colour is baked in as the shields brightgreen hex.
    assert "#4c1" in svg
    # Both label and message text appear at least once each.
    assert svg.count("link health") >= 1
    assert svg.count("alive") >= 1


def test_render_shields_endpoint_payload() -> None:
    payload = json.loads(
        render_shields_endpoint("link health", "🪦 0 dead / 5 alive", "brightgreen")
    )
    assert payload == {
        "schemaVersion": 1,
        "label": "link health",
        "message": "🪦 0 dead / 5 alive",
        "color": "brightgreen",
    }


def test_render_markdown_static_and_endpoint() -> None:
    static = render_markdown("link health", "🪦 0 dead / 5 alive", "brightgreen")
    assert static.startswith("![")
    assert "img.shields.io/badge/" in static
    # Spaces are percent-encoded so the URL is paste-safe.
    assert " " not in static.split("](", 1)[1]

    hosted = render_markdown(
        "link health",
        "🪦 0 dead / 5 alive",
        "brightgreen",
        endpoint_url="https://example.com/link-coroner.json",
    )
    assert "img.shields.io/endpoint?url=https://example.com/link-coroner.json" in hosted


def test_summarize_from_json_roundtrip() -> None:
    payload = json.dumps(
        [
            {"url": "https://a", "verdict": "ALIVE", "cause": "ALIVE"},
            {"url": "https://b", "verdict": "DEAD", "cause": "HTTP_4XX"},
        ]
    )
    summary = summarize_from_json(payload)
    assert summary.total == 2
    assert summary.dead == 1
    assert summary.alive == 1


def test_cli_badge_from_json_file_svg(tmp_path: Path) -> None:
    results = tmp_path / "results.json"
    results.write_text(
        json.dumps(
            [
                {"url": "https://a", "verdict": "ALIVE", "cause": "ALIVE"},
                {"url": "https://b", "verdict": "DEAD", "cause": "HTTP_4XX"},
            ]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "badge.svg"
    result = runner.invoke(
        app,
        ["badge", "--from", str(results), "--format", "svg", "-o", str(out)],
    )
    assert result.exit_code == 0, result.output
    svg = out.read_text(encoding="utf-8")
    assert svg.startswith("<svg")
    # 1 dead → red shields hex.
    assert "#e05d44" in svg


def test_cli_badge_shields_endpoint_to_stdout(tmp_path: Path) -> None:
    results = tmp_path / "results.json"
    results.write_text(
        json.dumps([{"url": "https://a", "verdict": "ALIVE", "cause": "ALIVE"}]),
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        ["badge", "--from", str(results), "--format", "shields-endpoint"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schemaVersion"] == 1
    assert payload["color"] == "brightgreen"
    assert payload["label"] == "link health"


def test_cli_badge_markdown_custom_label(tmp_path: Path) -> None:
    results = tmp_path / "results.json"
    results.write_text(
        json.dumps([{"url": "https://a", "verdict": "ALIVE", "cause": "ALIVE"}]),
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "badge",
            "--from",
            str(results),
            "--format",
            "markdown",
            "--label",
            "rot meter",
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.strip().startswith("![rot meter")
    assert "rot%20meter" in result.stdout


def test_cli_badge_requires_source() -> None:
    result = runner.invoke(app, ["badge", "--format", "svg"])
    assert result.exit_code != 0
    assert "--from" in result.output or "--cache" in result.output


def test_cli_badge_rejects_both_sources(tmp_path: Path) -> None:
    results = tmp_path / "results.json"
    results.write_text("[]", encoding="utf-8")
    cache = tmp_path / "cache.sqlite"
    cache.write_bytes(b"")
    result = runner.invoke(
        app,
        [
            "badge",
            "--from",
            str(results),
            "--cache",
            str(cache),
            "--format",
            "svg",
        ],
    )
    assert result.exit_code != 0


def test_cli_badge_from_cache_uses_latest_per_url(tmp_path: Path) -> None:
    """A URL that died then came back alive should count as alive."""
    from link_coroner.cache import ProbeCache, ProbeEvent

    cache_path = tmp_path / "cache.sqlite"
    with ProbeCache(cache_path) as cache:
        cache.record_events(
            [
                ProbeEvent(
                    url="https://flaky.example/",
                    host="flaky.example",
                    file_path=None,
                    verdict="DEAD",
                    cause="HTTP_4XX",
                    observed_at=100,
                ),
                ProbeEvent(
                    url="https://flaky.example/",
                    host="flaky.example",
                    file_path=None,
                    verdict="ALIVE",
                    cause="ALIVE",
                    observed_at=200,
                ),
            ]
        )

    result = runner.invoke(
        app,
        [
            "badge",
            "--cache",
            str(cache_path),
            "--format",
            "shields-endpoint",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["color"] == "brightgreen"
    assert "1 alive" in payload["message"]

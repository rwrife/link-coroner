"""Snapshot-ish tests for M3 death-certificate rendering + summary footer."""

from __future__ import annotations

import io
import json

from rich.console import Console

from link_coroner.forensics.probe import ProbeResult, Verdict
from link_coroner.reporting.autopsy import (
    render_certificates,
    render_json,
    render_pretty,
)


def _console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, force_terminal=False, width=100, color_system=None, record=False), buf


def _sample_results() -> list[ProbeResult]:
    return [
        ProbeResult(
            url="https://alive.example.com/",
            verdict=Verdict.ALIVE,
            reason="HTTP_200",
            status_code=200,
            elapsed_ms=42,
        ),
        ProbeResult(
            url="https://dead.example.com/page",
            verdict=Verdict.DEAD,
            reason="HTTP_404",
            status_code=404,
            elapsed_ms=88,
        ),
        ProbeResult(
            url="https://gone.invalid/",
            verdict=Verdict.UNREACHABLE,
            reason="NXDOMAIN",
        ),
    ]


def test_render_certificates_emits_panels_only_for_deceased():
    console, buf = _console()
    render_certificates(_sample_results(), console)
    output = buf.getvalue()

    # alive URL must NOT appear inside a death-certificate panel
    assert "CERTIFICATE OF DEATH" in output
    assert "PRESUMED DEAD" in output
    assert "https://dead.example.com/page" in output
    assert "https://gone.invalid/" in output
    # alive URL only appears in the summary footer counts, not a card
    assert "https://alive.example.com/" not in output
    # cause taxonomy shows up
    assert "HTTP_4XX" in output
    assert "NXDOMAIN" in output
    # summary footer present
    assert "autopsy summary" in output
    assert "ALIVE: 1" in output
    assert "DEAD: 1" in output
    assert "SUSPICIOUS: 1" in output


def test_render_certificates_all_alive_shows_celebration():
    console, buf = _console()
    render_certificates(
        [
            ProbeResult(
                url="https://alive.example.com/",
                verdict=Verdict.ALIVE,
                reason="HTTP_200",
                status_code=200,
            )
        ],
        console,
    )
    output = buf.getvalue()
    assert "No certificates required" in output
    assert "ALIVE: 1" in output
    assert "DEAD: 0" in output


def test_render_pretty_still_works_for_table_format():
    console, buf = _console()
    render_pretty(_sample_results(), console)
    output = buf.getvalue()
    assert "autopsy results" in output
    assert "ALIVE" in output and "DEAD" in output and "UNREACHABLE" in output


def test_render_json_includes_cause_taxonomy():
    data = json.loads(render_json(_sample_results()))
    by_url = {row["url"]: row for row in data}
    assert by_url["https://alive.example.com/"]["cause"] == "ALIVE"
    assert by_url["https://dead.example.com/page"]["cause"] == "HTTP_4XX"
    assert by_url["https://gone.invalid/"]["cause"] == "NXDOMAIN"
    # blurbs included
    assert all("cause_blurb" in row for row in data)

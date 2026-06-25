"""Tests for the link-rot heatmap renderer (issue #22)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from link_coroner.cache import ProbeCache, ProbeEvent
from link_coroner.cli import app
from link_coroner.heatmap import (
    build_grid,
    render_ansi,
    render_html,
    render_svg,
)


def _ts(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp())


def _events():
    return [
        # Two URLs die in docs/ during week of 2026-01-05
        ProbeEvent("https://a/", "a.example", "docs/a.md", "DEAD", "HTTP_4XX", _ts(2026, 1, 6)),
        ProbeEvent("https://b/", "b.example", "docs/b.md", "UNREACHABLE", "TIMEOUT", _ts(2026, 1, 7)),
        # One URL dies in src/ in week of 2026-01-12
        ProbeEvent("https://c/", "c.example", "src/foo/bar.py", "DEAD", "HTTP_5XX", _ts(2026, 1, 13)),
        # Alive probe — must not count, but should anchor host MTBF window
        ProbeEvent("https://a/", "a.example", "docs/a.md", "ALIVE", "ALIVE", _ts(2026, 1, 20)),
        # Re-confirmation of the same dead URL must NOT inflate counts
        ProbeEvent("https://a/", "a.example", "docs/a.md", "DEAD", "HTTP_4XX", _ts(2026, 1, 19)),
    ]


def test_build_grid_aggregates_first_deaths_by_week_and_path():
    events = _events()
    grid = build_grid(events, since_ts=_ts(2026, 1, 1), until_ts=_ts(2026, 1, 25), path_depth=1)

    assert grid.stats.total_deaths == 3  # 3 unique URLs ever died
    # Path bucket order: docs (2) before src (1)
    assert grid.paths[0] == "docs/"
    assert "src/" in grid.paths

    # The week buckets cover every Monday in range, contiguous.
    assert grid.weeks[0].weekday() == 0
    assert grid.weeks[-1].weekday() == 0
    diffs = {(grid.weeks[i + 1] - grid.weeks[i]).days for i in range(len(grid.weeks) - 1)}
    assert diffs == {7}

    # Cell intensity matches the first-death week.
    # Find the docs row's week containing 2026-01-06
    docs_cell_week = next(w for w in grid.weeks if w <= _date(2026, 1, 6) < w + timedelta(days=7))
    assert grid.cell("docs/", docs_cell_week) == 2


def _date(y, m, d):
    return datetime(y, m, d, tzinfo=UTC).date()


def test_render_ansi_contains_paths_and_legend():
    grid = build_grid(_events(), since_ts=_ts(2026, 1, 1), until_ts=_ts(2026, 1, 25))
    out = render_ansi(grid, color=False)
    assert "link-rot heatmap" in out
    assert "docs/" in out
    assert "legend:" in out
    assert "total deaths: 3" in out
    assert "top rotting paths" in out


def test_render_ansi_with_empty_grid_tells_user():
    grid = build_grid([], since_ts=_ts(2026, 1, 1), until_ts=_ts(2026, 1, 25))
    out = render_ansi(grid, color=False)
    assert "No probe history" in out


def test_render_svg_and_html_emit_valid_markers():
    grid = build_grid(_events(), since_ts=_ts(2026, 1, 1), until_ts=_ts(2026, 1, 25))
    svg = render_svg(grid)
    assert svg.startswith("<svg")
    assert "</svg>" in svg
    assert "docs/" in svg

    html = render_html(grid)
    assert "<!doctype html>" in html
    assert "<svg" in html
    assert "Top rotting paths" in html


def test_heatmap_cli_reads_cache_and_writes_output(tmp_path):
    db = tmp_path / "cache.sqlite"
    with ProbeCache(db) as cache:
        cache.record_events(_events())

    out_file = tmp_path / "rot.html"
    result = CliRunner().invoke(
        app,
        [
            "heatmap",
            "--cache", str(db),
            "--format", "html",
            "--since", "9999d",
            "--output", str(out_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out_file.exists()
    body = out_file.read_text()
    assert "link-rot heatmap" in body
    assert "Top rotting paths" in body


def test_heatmap_cli_errors_when_cache_missing(tmp_path):
    result = CliRunner().invoke(
        app, ["heatmap", "--cache", str(tmp_path / "nope.sqlite")]
    )
    assert result.exit_code == 1
    assert "cache not found" in result.output


def test_autopsy_cli_populates_cache(tmp_path, monkeypatch):
    # Stub out probe_urls so we don't hit the network.
    from link_coroner import cli as cli_mod
    from link_coroner.forensics.probe import ProbeResult, Verdict

    async def fake_probe_urls(urls, config=None):
        return [
            ProbeResult(url=u, verdict=Verdict.DEAD, reason="HTTP_404", status_code=404)
            for u in urls
        ]

    monkeypatch.setattr(cli_mod, "probe_urls", fake_probe_urls)

    src = tmp_path / "doc.md"
    src.write_text("see https://dead.example.com/page for details\n")

    db = tmp_path / "history.sqlite"
    result = CliRunner().invoke(
        app,
        [
            "autopsy", str(src),
            "--format", "json",
            "--no-fail-on-dead",
            "--cache", str(db),
        ],
    )
    assert result.exit_code == 0, result.output
    assert db.exists()
    with ProbeCache(db) as cache:
        events = cache.all_events()
    assert len(events) == 1
    assert events[0].url == "https://dead.example.com/page"
    assert events[0].verdict == "DEAD"

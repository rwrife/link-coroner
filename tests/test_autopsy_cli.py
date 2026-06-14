"""Smoke tests for the autopsy CLI command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from link_coroner.cli import app
from link_coroner.forensics import probe as probe_mod
from link_coroner.forensics.probe import ProbeResult, Verdict


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    (tmp_path / "README.md").write_text(
        "See https://alive.example.com and https://dead.example.com/x"
    )
    return tmp_path


def _fake_results(urls):
    out = []
    for url in urls:
        if "dead" in url:
            out.append(ProbeResult(url=url, verdict=Verdict.DEAD, reason="HTTP_404", status_code=404))
        else:
            out.append(ProbeResult(url=url, verdict=Verdict.ALIVE, reason="HTTP_200", status_code=200))
    return out


def test_autopsy_pretty_exits_nonzero_on_dead(monkeypatch, sample_repo):
    async def fake_probe(urls, *, config=None, client=None):
        return _fake_results(list(urls))

    monkeypatch.setattr(probe_mod, "probe_urls", fake_probe)
    # cli imports the symbol at module load, so patch there too:
    from link_coroner import cli as cli_mod

    monkeypatch.setattr(cli_mod, "probe_urls", fake_probe)

    result = CliRunner().invoke(app, ["autopsy", str(sample_repo)])
    assert result.exit_code == 1, result.output
    assert "DEAD" in result.output
    assert "ALIVE" in result.output


def test_autopsy_json_output(monkeypatch, sample_repo):
    async def fake_probe(urls, *, config=None, client=None):
        return _fake_results(list(urls))

    from link_coroner import cli as cli_mod

    monkeypatch.setattr(cli_mod, "probe_urls", fake_probe)

    result = CliRunner().invoke(
        app,
        ["autopsy", str(sample_repo), "--format", "json", "--no-fail-on-dead"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert {row["verdict"] for row in data} == {"ALIVE", "DEAD"}


def test_autopsy_no_urls_found(tmp_path: Path):
    (tmp_path / "empty.md").write_text("no links here")
    result = CliRunner().invoke(app, ["autopsy", str(tmp_path)])
    assert result.exit_code == 0
    assert "nothing to autopsy" in result.output.lower() or "no urls" in result.output.lower()

"""Tests for persona modes (issue #7)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from link_coroner.cli import app
from link_coroner.diagnosis import Cause
from link_coroner.forensics.probe import ProbeResult, Verdict
from link_coroner.personas import (
    DEFAULT_PERSONA,
    PERSONAS,
    get_persona,
    list_personas,
)
from link_coroner.reporting.autopsy import render_json


def test_default_persona_blurb_matches_canonical():
    from link_coroner.diagnosis import cause_blurb

    p = get_persona(None)
    assert p.name == DEFAULT_PERSONA
    for cause in Cause:
        assert p.blurb(cause) == cause_blurb(cause)


@pytest.mark.parametrize("name", ["noir-detective", "victorian-doctor",
                                  "crime-scene-photographer", "deadpan-medical-examiner"])
def test_persona_overrides_all_causes(name):
    p = get_persona(name)
    for cause in Cause:
        assert p.blurb(cause), f"{name} missing blurb for {cause}"
    # And the overrides should differ from canonical for at least the headline causes.
    from link_coroner.diagnosis import cause_blurb

    assert p.blurb(Cause.NXDOMAIN) != cause_blurb(Cause.NXDOMAIN)


def test_get_persona_case_insensitive_and_unknown():
    assert get_persona("NOIR-DETECTIVE").name == "noir-detective"
    with pytest.raises(ValueError):
        get_persona("sherlock")


def test_list_personas_puts_default_first():
    listed = list_personas()
    assert listed[0].name == DEFAULT_PERSONA
    names = {p.name for p in listed}
    assert names == set(PERSONAS)


def test_render_json_includes_persona_fields():
    result = ProbeResult(
        url="https://gone.example.com",
        verdict=Verdict.DEAD,
        status_code=404,
        elapsed_ms=12,
        reason="HTTP_404",
        final_url=None,
    )
    payload = json.loads(render_json([result], persona="noir-detective"))
    assert payload[0]["persona"] == "noir-detective"
    assert payload[0]["persona_blurb"]
    assert payload[0]["persona_blurb"] != payload[0]["cause_blurb"]


def test_render_json_default_persona_omits_persona_field():
    result = ProbeResult(
        url="https://gone.example.com",
        verdict=Verdict.DEAD,
        status_code=404,
        elapsed_ms=12,
        reason="HTTP_404",
        final_url=None,
    )
    payload = json.loads(render_json([result]))
    assert "persona" not in payload[0]
    assert "persona_blurb" not in payload[0]


def test_cli_personas_command_lists_all():
    runner = CliRunner()
    res = runner.invoke(app, ["personas"])
    assert res.exit_code == 0
    for name in PERSONAS:
        assert name in res.stdout


def test_cli_autopsy_rejects_unknown_persona(tmp_path):
    sample = tmp_path / "doc.md"
    sample.write_text("see https://example.com\n")
    runner = CliRunner()
    res = runner.invoke(app, ["autopsy", str(sample), "--persona", "sherlock",
                              "--no-fail-on-dead"])
    assert res.exit_code != 0
    assert "Unknown persona" in (res.stdout + (res.stderr or ""))

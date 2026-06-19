"""Tests for JUnit XML and SARIF reporters (M6)."""

from __future__ import annotations

import json
from xml.etree import ElementTree as ET

from link_coroner.forensics.probe import ProbeResult, Verdict
from link_coroner.reporting.junit_out import render_junit
from link_coroner.reporting.sarif_out import SARIF_VERSION, render_sarif


def _results() -> list[ProbeResult]:
    return [
        ProbeResult(
            url="https://alive.example.com/",
            verdict=Verdict.ALIVE,
            reason="HTTP_200",
            status_code=200,
            elapsed_ms=42,
        ),
        ProbeResult(
            url="https://dead.example.com/missing",
            verdict=Verdict.DEAD,
            reason="HTTP_404",
            status_code=404,
            elapsed_ms=120,
        ),
        ProbeResult(
            url="https://flaky.example.com/",
            verdict=Verdict.UNREACHABLE,
            reason="CONN_ERROR:timed out",
            status_code=None,
            elapsed_ms=10000,
        ),
    ]


def test_render_junit_well_formed_with_failure_and_error() -> None:
    xml = render_junit(_results())
    root = ET.fromstring(xml)

    assert root.tag == "testsuites"
    assert root.attrib["tests"] == "3"
    assert root.attrib["failures"] == "1"
    assert root.attrib["errors"] == "1"

    suite = root.find("testsuite")
    assert suite is not None
    cases = suite.findall("testcase")
    assert len(cases) == 3

    by_name = {c.attrib["name"]: c for c in cases}
    assert by_name["https://alive.example.com/"].find("failure") is None
    assert by_name["https://alive.example.com/"].find("error") is None

    failure = by_name["https://dead.example.com/missing"].find("failure")
    assert failure is not None
    assert failure.attrib["type"] == "HTTP_4XX"
    assert "HTTP_404" in failure.attrib["message"]

    error = by_name["https://flaky.example.com/"].find("error")
    assert error is not None


def test_render_junit_handles_empty() -> None:
    xml = render_junit([])
    root = ET.fromstring(xml)
    assert root.attrib["tests"] == "0"


def test_render_junit_escapes_xml_special_chars() -> None:
    results = [
        ProbeResult(
            url="https://example.com/?q=1&a=<x>",
            verdict=Verdict.DEAD,
            reason='HTTP_404 "gone"',
            status_code=404,
            elapsed_ms=1,
        )
    ]
    xml = render_junit(results)
    # Must parse cleanly despite ampersands and angle brackets in URL/reason.
    root = ET.fromstring(xml)
    case = root.find("testsuite/testcase")
    assert case is not None
    assert case.attrib["name"] == "https://example.com/?q=1&a=<x>"


def test_render_sarif_schema_and_results() -> None:
    sarif = json.loads(render_sarif(_results()))

    assert sarif["version"] == SARIF_VERSION
    assert "$schema" in sarif
    assert len(sarif["runs"]) == 1
    run = sarif["runs"][0]
    assert run["tool"]["driver"]["name"] == "link-coroner"

    rules = run["tool"]["driver"]["rules"]
    rule_ids = {r["id"] for r in rules}
    assert {"HTTP_4XX", "NXDOMAIN", "TLS_EXPIRED"}.issubset(rule_ids)

    # Only deceased + unreachable end up as results.
    results = run["results"]
    assert len(results) == 2
    rule_ids_used = {r["ruleId"] for r in results}
    assert "HTTP_4XX" in rule_ids_used

    levels = {r["level"] for r in results}
    assert levels == {"error", "warning"}


def test_render_sarif_alive_only_returns_empty_results() -> None:
    only_alive = [
        ProbeResult(
            url="https://alive.example.com/",
            verdict=Verdict.ALIVE,
            reason="HTTP_200",
            status_code=200,
            elapsed_ms=10,
        )
    ]
    sarif = json.loads(render_sarif(only_alive))
    assert sarif["runs"][0]["results"] == []

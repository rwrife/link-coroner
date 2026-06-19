"""JUnit XML output (M6).

Each autopsied URL is rendered as a ``<testcase>``. Deceased URLs become
failures; suspicious (UNREACHABLE) URLs become errors so CI dashboards can
distinguish "definitely broken" from "couldn't reach". Alive URLs pass.

The format is the de-facto Jenkins/JUnit subset that GitHub, GitLab, Azure
DevOps, Buildkite, etc. all consume.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from xml.sax.saxutils import escape, quoteattr

from ..diagnosis import diagnose
from ..forensics.probe import ProbeResult, Verdict


def render_junit(results: Iterable[ProbeResult]) -> str:
    """Render an iterable of ``ProbeResult`` as a JUnit XML document."""
    results = list(results)
    failures = sum(1 for r in results if r.verdict is Verdict.DEAD)
    errors = sum(1 for r in results if r.verdict is Verdict.UNREACHABLE)
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")

    lines: list[str] = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append(
        "<testsuites "
        f'name="link-coroner" tests="{len(results)}" '
        f'failures="{failures}" errors="{errors}">'
    )
    lines.append(
        "  <testsuite "
        f'name="link-coroner.autopsy" tests="{len(results)}" '
        f'failures="{failures}" errors="{errors}" '
        f'timestamp="{timestamp}">'
    )

    for r in results:
        cause = diagnose(r)
        elapsed = (r.elapsed_ms or 0) / 1000.0
        attrs = (
            f"name={quoteattr(r.url)} "
            f'classname="link-coroner.{r.verdict.value.lower()}" '
            f'time="{elapsed:.3f}"'
        )
        if r.verdict is Verdict.ALIVE:
            lines.append(f"    <testcase {attrs}/>")
            continue

        tag = "failure" if r.verdict is Verdict.DEAD else "error"
        message = f"{cause.value}: {r.reason}"
        body = (
            f"URL: {r.url}\n"
            f"Verdict: {r.verdict.value}\n"
            f"Cause: {cause.value}\n"
            f"Reason: {r.reason}\n"
            f"Status: {r.status_code if r.status_code is not None else '-'}\n"
            f"Final URL: {r.final_url or '-'}\n"
        )
        lines.append(f"    <testcase {attrs}>")
        lines.append(
            f"      <{tag} message={quoteattr(message)} "
            f'type="{cause.value}">{escape(body)}</{tag}>'
        )
        lines.append("    </testcase>")

    lines.append("  </testsuite>")
    lines.append("</testsuites>")
    return "\n".join(lines) + "\n"

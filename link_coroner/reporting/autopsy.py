"""Reporting helpers for autopsy results.

M2 shipped a table renderer. M3 adds:
- A "death certificate" card per deceased URL, rendered with rich.
- A summary footer (alive / dead / suspicious).
- JSON output enriched with the cause taxonomy from ``diagnosis``.

The original :func:`render_pretty` table renderer is kept for the
``--format table`` mode so users (and tests) who liked it still have it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..diagnosis import Cause, cause_blurb, diagnose
from ..forensics.probe import ProbeResult, Verdict

_VERDICT_STYLES = {
    Verdict.ALIVE: "green",
    Verdict.DEAD: "red",
    Verdict.UNREACHABLE: "yellow",
}

_CAUSE_GLYPH = {
    Cause.NXDOMAIN: "🪦",
    Cause.DNS_FAILURE: "📡",
    Cause.CONN_REFUSED: "🚪",
    Cause.TLS_EXPIRED: "🔓",
    Cause.TLS_ERROR: "🔐",
    Cause.HTTP_4XX: "🚫",
    Cause.HTTP_5XX: "💥",
    Cause.TIMEOUT: "⌛",
    Cause.REDIRECT_LOOP: "🌀",
    Cause.BAD_URL: "❓",
    Cause.SOFT_404: "🧟",
    Cause.PARKED: "🅿️",
    Cause.UNKNOWN: "❔",
}


# ---- legacy table renderer (still useful for compact output) ----------------------


def render_pretty(results: Iterable[ProbeResult], console: Console) -> None:
    results = list(results)
    table = Table(title="🪦 link-coroner — autopsy results", header_style="bold")
    table.add_column("Verdict", no_wrap=True)
    table.add_column("Status", justify="right", no_wrap=True)
    table.add_column("Time", justify="right", no_wrap=True)
    table.add_column("URL", overflow="fold")
    table.add_column("Reason", no_wrap=True)

    for r in results:
        style = _VERDICT_STYLES.get(r.verdict, "white")
        table.add_row(
            f"[{style}]{r.verdict.value}[/{style}]",
            "-" if r.status_code is None else str(r.status_code),
            "-" if r.elapsed_ms is None else f"{r.elapsed_ms}ms",
            r.url,
            r.reason,
        )

    console.print(table)
    _print_summary(results, console)


# ---- death certificates (M3) ------------------------------------------------------


def _certificate_for(result: ProbeResult, *, now: datetime | None = None) -> Panel:
    cause = diagnose(result)
    glyph = _CAUSE_GLYPH.get(cause, "🪦")
    style = _VERDICT_STYLES.get(result.verdict, "white")
    timestamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")

    body = Text()
    body.append("URL:           ", style="bold")
    body.append(f"{result.url}\n")
    body.append("Verdict:       ", style="bold")
    body.append(f"{result.verdict.value}\n", style=style)
    body.append("Cause:         ", style="bold")
    body.append(f"{cause.value}\n", style=style)
    body.append("Notes:         ", style="bold")
    body.append(f"{cause_blurb(cause)}\n", style="italic")
    body.append("HTTP status:   ", style="bold")
    body.append(f"{result.status_code if result.status_code is not None else '—'}\n")
    body.append("Probe latency: ", style="bold")
    body.append(f"{result.elapsed_ms}ms\n" if result.elapsed_ms is not None else "—\n")
    body.append("Raw reason:    ", style="bold")
    body.append(f"{result.reason or '—'}\n", style="dim")
    if result.final_url:
        body.append("Final URL:     ", style="bold")
        body.append(f"{result.final_url}\n", style="dim")
    body.append("Time of death: ", style="bold")
    body.append(timestamp, style="dim")

    title = f"{glyph}  CERTIFICATE OF DEATH"
    if result.verdict is Verdict.UNREACHABLE:
        title = f"{glyph}  PRESUMED DEAD"
    return Panel(
        body,
        title=f"[{style}]{title}[/{style}]",
        border_style=style,
        padding=(1, 2),
    )


def _print_summary(results: list[ProbeResult], console: Console) -> None:
    counts: dict[str, int] = {v.value: 0 for v in Verdict}
    for r in results:
        counts[r.verdict.value] += 1
    suspicious = counts["UNREACHABLE"]
    console.print(
        Panel(
            Text.assemble(
                ("ALIVE: ", "bold"), (f"{counts['ALIVE']}  ", "green"),
                ("DEAD: ", "bold"), (f"{counts['DEAD']}  ", "red"),
                ("SUSPICIOUS: ", "bold"), (f"{suspicious}", "yellow"),
            ),
            title="🪦 autopsy summary",
            border_style="bold",
        )
    )


def render_certificates(results: Iterable[ProbeResult], console: Console) -> None:
    """Render a death-certificate panel for each non-ALIVE result, plus a summary."""
    results = list(results)
    deceased = [r for r in results if r.verdict is not Verdict.ALIVE]

    if deceased:
        console.print(
            Group(*[_certificate_for(r) for r in deceased])
        )
    else:
        console.print(
            Panel(
                Text("All URLs are breathing. No certificates required. 🎉", style="green"),
                title="🪦 link-coroner",
                border_style="green",
            )
        )

    _print_summary(results, console)


def render_json(results: Iterable[ProbeResult]) -> str:
    """JSON output, enriched with the M3 cause taxonomy."""
    payload = []
    for r in results:
        item = r.to_dict()
        cause = diagnose(r)
        item["cause"] = cause.value
        item["cause_blurb"] = cause_blurb(cause)
        payload.append(item)
    return json.dumps(payload, indent=2, sort_keys=True)

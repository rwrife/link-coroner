"""Reporting helpers for autopsy results."""

from __future__ import annotations

import json
from collections.abc import Iterable

from rich.console import Console
from rich.table import Table

from ..forensics.probe import ProbeResult, Verdict

_VERDICT_STYLES = {
    Verdict.ALIVE: "green",
    Verdict.DEAD: "red",
    Verdict.UNREACHABLE: "yellow",
}


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

    counts: dict[str, int] = {v.value: 0 for v in Verdict}
    for r in results:
        counts[r.verdict.value] += 1
    console.print(
        f"[green]ALIVE[/green]: {counts['ALIVE']}  "
        f"[red]DEAD[/red]: {counts['DEAD']}  "
        f"[yellow]UNREACHABLE[/yellow]: {counts['UNREACHABLE']}"
    )


def render_json(results: Iterable[ProbeResult]) -> str:
    payload = [r.to_dict() for r in results]
    return json.dumps(payload, indent=2, sort_keys=True)

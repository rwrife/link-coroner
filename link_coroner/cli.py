"""Command-line entry-point for link-coroner."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console

from . import __version__
from .forensics.probe import ProbeConfig, Verdict, probe_urls
from .reporting.autopsy import render_json, render_pretty
from .scanner.extractors import extract_urls
from .scanner.walker import walk_paths

app = typer.Typer(
    name="link-coroner",
    help="A forensic pathologist for the dead links rotting inside your repo.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"link-coroner {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """link-coroner — autopsy report for the URLs in your repo."""


@app.command()
def scan(
    path: Path = typer.Argument(
        Path("."),
        exists=True,
        file_okay=True,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        help="File or directory to scan.",
    ),
    unique: bool = typer.Option(
        True,
        "--unique/--all",
        help="Deduplicate URLs across files.",
    ),
) -> None:
    """Walk PATH and print every URL we'd autopsy (no probing yet — M1)."""
    seen: set[str] = set()
    total = 0
    for file_path in walk_paths(path):
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            console.print(f"[yellow]skip[/yellow] {file_path}: {exc}")
            continue
        for url in extract_urls(text, suffix=file_path.suffix.lower()):
            total += 1
            if unique and url in seen:
                continue
            seen.add(url)
            console.print(f"{url}  [dim]({file_path})[/dim]")

    shown = len(seen) if unique else total
    console.print(
        f"\n[bold]🪦 link-coroner[/bold]: found [cyan]{shown}[/cyan] URL(s) "
        f"in [cyan]{path}[/cyan]."
    )


def _collect_urls(path: Path) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for file_path in walk_paths(path):
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            console.print(f"[yellow]skip[/yellow] {file_path}: {exc}")
            continue
        for url in extract_urls(text, suffix=file_path.suffix.lower()):
            if url in seen:
                continue
            seen.add(url)
            ordered.append(url)
    return ordered


@app.command()
def autopsy(
    path: Path = typer.Argument(
        Path("."),
        exists=True,
        file_okay=True,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        help="File or directory to scan.",
    ),
    concurrency: int = typer.Option(16, "--concurrency", "-c", min=1, max=256),
    per_host: int = typer.Option(4, "--per-host", min=1, max=64),
    timeout: float = typer.Option(10.0, "--timeout", min=0.1),
    output: str = typer.Option("pretty", "--format", "-f", case_sensitive=False),
    fail_on_dead: bool = typer.Option(
        True,
        "--fail-on-dead/--no-fail-on-dead",
        help="Exit non-zero if any DEAD links are found.",
    ),
) -> None:
    """Walk PATH, probe every URL, and verdict each as ALIVE/DEAD/UNREACHABLE."""
    fmt = output.lower()
    if fmt not in {"pretty", "json"}:
        raise typer.BadParameter("--format must be one of: pretty, json")

    urls = _collect_urls(path)
    if not urls:
        if fmt == "json":
            typer.echo("[]")
        else:
            console.print("[dim]No URLs found — nothing to autopsy.[/dim]")
        raise typer.Exit(0)

    cfg = ProbeConfig(
        concurrency=concurrency,
        per_host_concurrency=per_host,
        timeout=timeout,
    )
    results = asyncio.run(probe_urls(urls, config=cfg))

    if fmt == "json":
        typer.echo(render_json(results))
    else:
        render_pretty(results, console)

    if fail_on_dead and any(r.verdict is Verdict.DEAD for r in results):
        raise typer.Exit(1)


if __name__ == "__main__":  # pragma: no cover
    app()

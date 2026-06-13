"""Command-line entry-point for link-coroner."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from . import __version__
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


if __name__ == "__main__":  # pragma: no cover
    app()

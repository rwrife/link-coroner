"""Command-line entry-point for link-coroner."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console

from . import __version__
from .diagnosis import exit_code_for
from .forensics.probe import ProbeConfig, Verdict, probe_urls
from .mortician import (
    MorticianPolicy,
    build_pr_body,
    filter_snapshots,
    open_pull_request,
)
from .personas import PERSONAS, get_persona, list_personas
from .reporting.autopsy import render_certificates, render_json, render_pretty
from .reporting.junit_out import render_junit
from .reporting.sarif_out import render_sarif
from .rewrite import rewrite_files
from .scanner.extractors import extract_urls
from .scanner.walker import walk_paths
from .wayback import resurrect_many

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


def _emit(payload: str, output_file: Path | None) -> None:
    """Write a serialized report to ``output_file`` or stdout."""
    if output_file is None:
        typer.echo(payload)
        return
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(payload, encoding="utf-8")
    console.print(f"[dim]wrote report → {output_file}[/dim]")


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
    output_file: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write report to this file instead of stdout (great for CI artifacts).",
    ),
    fail_on_dead: bool = typer.Option(
        True,
        "--fail-on-dead/--no-fail-on-dead",
        help="Exit non-zero if any DEAD links are found.",
    ),
    fail_on: str = typer.Option(
        "dead",
        "--fail-on",
        case_sensitive=False,
        help="Severity threshold for non-zero exit: dead | suspicious | never.",
    ),
    resurrect: bool = typer.Option(
        False,
        "--resurrect/--no-resurrect",
        help="Query the Wayback Machine for each deceased URL and add a resurrect link.",
    ),
    persona: str = typer.Option(
        "coroner",
        "--persona",
        "-p",
        case_sensitive=False,
        help="Narrator voice for certificates: " + ", ".join(sorted(PERSONAS)) + ".",
    ),
) -> None:
    """Walk PATH, probe every URL, and verdict each as ALIVE/DEAD/UNREACHABLE."""
    fmt = output.lower()
    if fmt not in {"pretty", "json", "certificates", "table", "junit", "sarif"}:
        raise typer.BadParameter(
            "--format must be one of: pretty, certificates, table, json, junit, sarif"
        )
    fail_on_norm = fail_on.lower()
    if fail_on_norm not in {"dead", "suspicious", "never"}:
        raise typer.BadParameter("--fail-on must be one of: dead, suspicious, never")

    try:
        persona_obj = get_persona(persona)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    urls = _collect_urls(path)
    if not urls:
        if fmt == "json":
            _emit("[]", output_file)
        elif fmt == "junit":
            _emit(render_junit([]), output_file)
        elif fmt == "sarif":
            _emit(render_sarif([]), output_file)
        else:
            console.print("[dim]No URLs found — nothing to autopsy.[/dim]")
        raise typer.Exit(0)

    cfg = ProbeConfig(
        concurrency=concurrency,
        per_host_concurrency=per_host,
        timeout=timeout,
    )
    results = asyncio.run(probe_urls(urls, config=cfg))

    snapshots = {}
    if resurrect:
        dead_urls = [r.url for r in results if r.verdict is not Verdict.ALIVE]
        if dead_urls:
            snapshots = asyncio.run(resurrect_many(dead_urls))

    if fmt == "json":
        payload = render_json(results, snapshots=snapshots, persona=persona_obj)
        _emit(payload, output_file)
    elif fmt == "junit":
        _emit(render_junit(results), output_file)
    elif fmt == "sarif":
        _emit(render_sarif(results), output_file)
    elif fmt == "table":
        render_pretty(results, console)
    else:
        # "pretty" now means certificates (M3); "certificates" is the explicit alias.
        render_certificates(results, console, snapshots=snapshots, persona=persona_obj)

    if fail_on_norm == "never":
        raise typer.Exit(0)
    threshold = Verdict.DEAD if fail_on_norm == "dead" else Verdict.UNREACHABLE
    # --no-fail-on-dead still wins as a kill-switch for backwards compat.
    if not fail_on_dead:
        raise typer.Exit(0)
    raise typer.Exit(exit_code_for(results, threshold=threshold))


@app.command()
def rewrite(
    path: Path = typer.Argument(
        Path("."),
        exists=True,
        file_okay=True,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        help="File or directory to scan and patch.",
    ),
    concurrency: int = typer.Option(16, "--concurrency", "-c", min=1, max=256),
    per_host: int = typer.Option(4, "--per-host", min=1, max=64),
    timeout: float = typer.Option(10.0, "--timeout", min=0.1),
    apply_changes: bool = typer.Option(
        False,
        "--apply/--dry-run",
        help="Actually write the changes. Defaults to dry-run for safety.",
    ),
    backup: bool = typer.Option(
        True,
        "--backup/--no-backup",
        help="Write a .bak sibling before overwriting each touched file.",
    ),
) -> None:
    """Patch dead URLs in PATH with their Wayback Machine snapshots.

    Dry-run by default. Pass ``--apply`` to actually overwrite files.
    """
    urls = _collect_urls(path)
    if not urls:
        console.print("[dim]No URLs found — nothing to resurrect.[/dim]")
        raise typer.Exit(0)

    cfg = ProbeConfig(
        concurrency=concurrency,
        per_host_concurrency=per_host,
        timeout=timeout,
    )
    results = asyncio.run(probe_urls(urls, config=cfg))
    dead_urls = [r.url for r in results if r.verdict is not Verdict.ALIVE]
    if not dead_urls:
        console.print("[green]No deceased URLs detected — nothing to rewrite.[/green]")
        raise typer.Exit(0)

    snapshots = asyncio.run(resurrect_many(dead_urls, include_time_of_death=False))
    result = rewrite_files(path, snapshots, dry_run=not apply_changes, backup=backup)

    if not result.changes:
        console.print("[yellow]Found dead URLs but no Wayback snapshots are available.[/yellow]")
        raise typer.Exit(0)

    mode = "DRY-RUN" if result.dry_run else "APPLIED"
    console.print(f"[bold]{mode}[/bold]: {result.files_modified} file(s) affected")
    for ch in result.changes:
        console.print(f"  {ch.path}: {ch.url} → {ch.replacement} ({ch.count}x)")
    if result.dry_run:
        console.print("\n[dim]Re-run with --apply to actually patch files.[/dim]")
    raise typer.Exit(0)


@app.command()
def mortician(
    path: Path = typer.Argument(
        Path("."),
        exists=True,
        file_okay=True,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        help="File or directory to scan and patch.",
    ),
    concurrency: int = typer.Option(16, "--concurrency", "-c", min=1, max=256),
    per_host: int = typer.Option(4, "--per-host", min=1, max=64),
    timeout: float = typer.Option(10.0, "--timeout", min=0.1),
    apply_changes: bool = typer.Option(
        False,
        "--apply/--dry-run",
        help="Actually write the changes (and open a PR). Defaults to dry-run.",
    ),
    backup: bool = typer.Option(
        False,
        "--backup/--no-backup",
        help="Write .bak siblings before overwriting (off by default in mortician mode "
        "since changes are tracked by git).",
    ),
    policy_file: Path | None = typer.Option(
        None,
        "--policy",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        help="Path to a 'do not resurrect' allowlist file.",
    ),
    open_pr: bool = typer.Option(
        False,
        "--open-pr/--no-open-pr",
        help="After applying changes, create a branch, commit, push, and open a PR via gh.",
    ),
    branch: str = typer.Option(
        "mortician/auto-resurrect",
        "--branch",
        help="Branch name to use when --open-pr is set.",
    ),
    base: str = typer.Option(
        "main",
        "--base",
        help="Base branch for the PR.",
    ),
    pr_title: str = typer.Option(
        "🪬Mortician auto-PR: resurrect dead links",
        "--pr-title",
        help="Title for the auto-PR.",
    ),
) -> None:
    """Resurrect dead URLs and optionally open a pull request.

    This is the convenience entry-point used by CI: scan, autopsy,
    fetch Wayback snapshots, filter by policy, rewrite files, and (with
    ``--open-pr``) create a branch + PR.
    """
    policy = MorticianPolicy.from_file(policy_file) if policy_file else MorticianPolicy.empty()

    urls = _collect_urls(path)
    if not urls:
        console.print("[dim]No URLs found — nothing to resurrect.[/dim]")
        raise typer.Exit(0)

    cfg = ProbeConfig(
        concurrency=concurrency,
        per_host_concurrency=per_host,
        timeout=timeout,
    )
    results = asyncio.run(probe_urls(urls, config=cfg))
    dead_urls = [r.url for r in results if r.verdict is not Verdict.ALIVE]
    if not dead_urls:
        console.print("[green]No deceased URLs detected — mortician is idle.[/green]")
        raise typer.Exit(0)

    snapshots = asyncio.run(resurrect_many(dead_urls, include_time_of_death=False))
    kept, skipped = filter_snapshots(snapshots, policy)
    no_snapshot = [
        url for url in dead_urls
        if url not in skipped and (url not in snapshots or not snapshots[url].snapshot_url)
    ]

    if not kept:
        console.print(
            "[yellow]Nothing to do: every dead URL is either policy-blocked "
            "or has no Wayback snapshot.[/yellow]"
        )
        raise typer.Exit(0)

    result = rewrite_files(path, kept, dry_run=not apply_changes, backup=backup)
    body = build_pr_body(result, skipped_by_policy=skipped, no_snapshot=no_snapshot)

    mode = "DRY-RUN" if result.dry_run else "APPLIED"
    console.print(f"[bold]{mode}[/bold]: {result.files_modified} file(s) affected")
    for ch in result.changes:
        console.print(f"  {ch.path}: {ch.url} → {ch.replacement} ({ch.count}x)")
    if skipped:
        console.print(f"[yellow]Skipped {len(skipped)} URL(s) per policy.[/yellow]")
    if no_snapshot:
        console.print(
            f"[yellow]{len(no_snapshot)} URL(s) had no Wayback snapshot available.[/yellow]"
        )

    if result.dry_run:
        console.print(
            "\n[dim]Re-run with --apply to patch files"
            " (and --open-pr to open a pull request).[/dim]"
        )
        raise typer.Exit(0)

    if open_pr:
        pr = open_pull_request(
            path if path.is_dir() else path.parent,
            branch=branch,
            title=pr_title,
            body=body,
            base=base,
        )
        if pr.pr_url:
            console.print(f"[green]Opened PR:[/green] {pr.pr_url}")
        else:
            console.print(f"[green]Pushed branch[/green] {pr.branch} — PR creation result returned no URL.")

    raise typer.Exit(0)


@app.command("personas")
def personas_cmd() -> None:
    """List available narrator personas for the death-certificate report."""
    for p in list_personas():
        marker = " (default)" if p.name == "coroner" else ""
        console.print(f"[bold cyan]{p.name}[/bold cyan]{marker} — {p.description}")


if __name__ == "__main__":  # pragma: no cover
    app()

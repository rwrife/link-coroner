"""Command-line entry-point for link-coroner."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console

from . import __version__
from . import diff as diff_mod
from .cache import ProbeCache
from .diagnosis import exit_code_for
from .forensics.probe import ProbeConfig, Verdict, probe_urls
from .heatmap import build_grid, render_ansi, render_html, render_svg
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
from .reporting.webhook import (
    build_digest,
    detect_provider,
    load_state,
    post_digest,
    render_payload,
    save_state,
)
from .rewrite import rewrite_files
from .scanner.extractors import extract_urls
from .scanner.site import discover_site
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
    path: Path | None = typer.Argument(
        None,
        exists=True,
        file_okay=True,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        help="File or directory to scan (omit when using --site).",
    ),
    site: str | None = typer.Option(
        None,
        "--site",
        help="Crawl a live site via sitemap.xml (with homepage fallback) instead of a path.",
    ),
    unique: bool = typer.Option(
        True,
        "--unique/--all",
        help="Deduplicate URLs across files.",
    ),
) -> None:
    """List every URL link-coroner would autopsy.

    Either provide a filesystem PATH or pass ``--site https://...`` to
    discover URLs from a deployed site's sitemap.xml + robots.txt (with
    one-hop homepage fallback if no sitemap is available).
    """
    if site and path is not None:
        raise typer.BadParameter("pass either PATH or --site, not both.")
    if site is None and path is None:
        path = Path(".").resolve()

    if site:
        discovery = asyncio.run(discover_site(site))
        for url in discovery.urls:
            console.print(url)
        origin = (
            f"homepage fallback at {site}" if discovery.used_fallback else f"sitemap at {site}"
        )
        if discovery.robots.disallowed:
            console.print(
                f"[dim]robots.txt: {len(discovery.robots.disallowed)} Disallow rule(s) applied[/dim]"
            )
        console.print(
            f"\n[bold]🪦 link-coroner[/bold]: found [cyan]{len(discovery.urls)}[/cyan] URL(s) "
            f"from [cyan]{origin}[/cyan]."
        )
        raise typer.Exit(0)

    assert path is not None  # noqa: S101 — narrowing for type checkers
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
    path: Path | None = typer.Argument(
        None,
        exists=True,
        file_okay=True,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        help="File or directory to scan (omit when using --site).",
    ),
    site: str | None = typer.Option(
        None,
        "--site",
        help="Autopsy URLs discovered from a live site's sitemap.xml (+ robots.txt).",
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
    cache_db: Path | None = typer.Option(
        None,
        "--cache",
        help="Persist probe results to this SQLite cache (used by `link-coroner heatmap`).",
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

    if site and path is not None:
        raise typer.BadParameter("pass either PATH or --site, not both.")
    if site is None and path is None:
        path = Path(".").resolve()

    if site:
        discovery = asyncio.run(discover_site(site))
        urls = discovery.urls
        if discovery.robots.disallowed:
            console.print(
                f"[dim]robots.txt: {len(discovery.robots.disallowed)} Disallow rule(s) applied[/dim]"
            )
    else:
        assert path is not None  # noqa: S101
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

    if cache_db is not None:
        with ProbeCache(cache_db) as cache:
            cache.record_probe_results(results)

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


@app.command()
def digest(
    path: Path = typer.Argument(
        Path("."),
        exists=True,
        file_okay=True,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        help="File or directory to scan.",
    ),
    webhook_url: str = typer.Option(
        ...,
        "--webhook-url",
        envvar="LINK_CORONER_WEBHOOK_URL",
        help="Slack or Discord incoming-webhook URL.",
    ),
    provider: str = typer.Option(
        "auto",
        "--provider",
        case_sensitive=False,
        help="Webhook provider: auto | slack | discord.",
    ),
    state_file: Path = typer.Option(
        Path(".link-coroner-state.json"),
        "--state-file",
        help="Where to persist the set of known-dead URLs between runs.",
    ),
    concurrency: int = typer.Option(16, "--concurrency", "-c", min=1, max=256),
    per_host: int = typer.Option(4, "--per-host", min=1, max=64),
    timeout: float = typer.Option(10.0, "--timeout", min=0.1),
    resurrect: bool = typer.Option(
        True,
        "--resurrect/--no-resurrect",
        help="Include a Wayback snapshot link for each newly-deceased URL.",
    ),
    max_entries: int = typer.Option(
        20, "--max-entries", min=1, max=200, help="Max URLs listed per section."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Build and print the payload without sending it."
    ),
    post_if_empty: bool = typer.Option(
        False,
        "--post-if-empty/--skip-if-empty",
        help="Send a 'nothing new' message even when there are no changes.",
    ),
) -> None:
    """Scan PATH and POST a 'newly-deceased links' digest to a Slack/Discord webhook."""
    prov = provider.lower()
    if prov == "auto":
        prov = detect_provider(webhook_url)
    if prov not in {"slack", "discord"}:
        raise typer.BadParameter("--provider must be one of: auto, slack, discord")

    urls = _collect_urls(path)
    if not urls:
        console.print("[dim]No URLs found — skipping digest.[/dim]")
        raise typer.Exit(0)

    cfg = ProbeConfig(
        concurrency=concurrency,
        per_host_concurrency=per_host,
        timeout=timeout,
    )
    results = asyncio.run(probe_urls(urls, config=cfg))

    previous = load_state(state_file)
    snapshots: dict = {}
    new_dead_urls = [
        r.url for r in results if r.verdict is not Verdict.ALIVE and r.url not in previous
    ]
    if resurrect and new_dead_urls:
        snapshots = asyncio.run(resurrect_many(new_dead_urls, include_time_of_death=False))

    digest_obj = build_digest(results, previous_dead=previous, snapshots=snapshots)

    if digest_obj.is_empty and not post_if_empty:
        console.print("[green]No new obituaries since last run — skipping webhook.[/green]")
        # Still rewrite state so old entries that resolved get cleared out.
        current_dead = [r.url for r in results if r.verdict is not Verdict.ALIVE]
        save_state(state_file, current_dead)
        raise typer.Exit(0)

    payload = render_payload(digest_obj, prov, max_entries=max_entries)

    if dry_run:
        import json as _json

        typer.echo(_json.dumps(payload, indent=2))
        raise typer.Exit(0)

    resp = post_digest(webhook_url, payload, timeout=timeout)
    if 200 <= resp.status_code < 300:
        console.print(
            f"[green]Posted obituary digest[/green]: "
            f"{len(digest_obj.newly_deceased)} new, "
            f"{len(digest_obj.resurrected)} resurrected."
        )
        current_dead = [r.url for r in results if r.verdict is not Verdict.ALIVE]
        save_state(state_file, current_dead)
        raise typer.Exit(0)

    console.print(
        f"[red]Webhook POST failed[/red]: HTTP {resp.status_code} — {resp.body[:200]}"
    )
    raise typer.Exit(1)


@app.command("lsp")
def lsp_cmd(
    cache_db: Path | None = typer.Option(
        None,
        "--cache",
        help="Optional SQLite probe-history cache to share with `link-coroner heatmap`.",
    ),
) -> None:
    """Run the Language Server Protocol server over stdio.

    Wire this into your editor to underline dying links live. See the
    README's \"Editor integration\" section for VSCode + Neovim snippets.
    """
    from .lsp import run_stdio as run_lsp_stdio

    try:
        asyncio.run(run_lsp_stdio(cache_db=cache_db))
    except KeyboardInterrupt:  # pragma: no cover
        raise typer.Exit(0) from None


@app.command("mcp")
def mcp_cmd() -> None:
    """Run the MCP server over stdio so AI agents can autopsy URLs inline."""
    from .mcp_server import run_stdio

    try:
        asyncio.run(run_stdio())
    except KeyboardInterrupt:  # pragma: no cover
        raise typer.Exit(0) from None


@app.command("personas")
def personas_cmd() -> None:
    """List available narrator personas for the death-certificate report."""
    for p in list_personas():
        marker = " (default)" if p.name == "coroner" else ""
        console.print(f"[bold cyan]{p.name}[/bold cyan]{marker} — {p.description}")


def _parse_since(spec: str) -> int:
    """Parse a duration spec like ``90d``/``12w``/``24h`` into a unix timestamp."""
    import re
    import time

    spec = spec.strip().lower()
    match = re.fullmatch(r"(\d+)([hdw])", spec)
    if not match:
        raise typer.BadParameter("--since must look like '24h', '90d', or '12w'.")
    qty = int(match.group(1))
    unit = match.group(2)
    seconds = {"h": 3600, "d": 86400, "w": 604800}[unit] * qty
    return int(time.time()) - seconds


@app.command("heatmap")
def heatmap_cmd(
    cache_db: Path = typer.Option(
        Path(".link-coroner-cache.sqlite"),
        "--cache",
        "--db",
        help="SQLite probe-history cache (written by `autopsy --cache`).",
    ),
    output_format: str = typer.Option(
        "ansi", "--format", "-f", case_sensitive=False,
        help="Output format: ansi | svg | html.",
    ),
    since: str = typer.Option(
        "90d", "--since",
        help="How far back to aggregate (e.g. 24h, 90d, 12w). Default 90d.",
    ),
    output_file: Path | None = typer.Option(
        None, "--output", "-o",
        help="Write to this file instead of stdout (recommended for svg/html).",
    ),
    depth: int = typer.Option(
        2, "--path-depth", min=1, max=8,
        help="How many leading path segments to bucket files by (default 2).",
    ),
    no_color: bool = typer.Option(
        False, "--no-color", help="Disable ANSI color in --format ansi.",
    ),
) -> None:
    """Render a link-rot heatmap from cached probe history.

    Run ``link-coroner autopsy --cache .link-coroner-cache.sqlite`` on a
    schedule to feed this command. The default window is 90 days.
    """
    fmt = output_format.lower()
    if fmt not in {"ansi", "svg", "html"}:
        raise typer.BadParameter("--format must be one of: ansi, svg, html")
    if not cache_db.exists():
        console.print(
            f"[yellow]cache not found:[/yellow] {cache_db}\n"
            "Run `link-coroner autopsy --cache <path>` first to populate history."
        )
        raise typer.Exit(1)

    since_ts = _parse_since(since)
    with ProbeCache(cache_db) as cache:
        events = cache.all_events(since=since_ts)
    grid = build_grid(events, since_ts=since_ts, path_depth=depth)

    if fmt == "ansi":
        payload = render_ansi(grid, color=not no_color)
    elif fmt == "svg":
        payload = render_svg(grid)
    else:
        payload = render_html(grid)

    _emit(payload, output_file)
    raise typer.Exit(0)


@app.command("diff")
def diff_cmd(
    base: str = typer.Argument(..., help="Base git revision (e.g. main, HEAD~1, a commit SHA)."),
    head: str = typer.Argument(..., help="Head git revision to compare against base."),
    repo: Path = typer.Option(
        Path("."),
        "--repo",
        "-C",
        help="Path to the git repository (defaults to current directory).",
    ),
    output_format: str = typer.Option(
        "pretty",
        "--format",
        "-f",
        case_sensitive=False,
        help="Output format: pretty | json | markdown-comment.",
    ),
    output_file: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write the report to this file instead of stdout.",
    ),
    fail_on: str = typer.Option(
        "new-dead",
        "--fail-on",
        case_sensitive=False,
        help="Exit-code policy: new-dead (default) | any-dead | never.",
    ),
    concurrency: int = typer.Option(16, "--concurrency", "-c", min=1, max=256),
    timeout: float = typer.Option(10.0, "--timeout", min=0.1),
) -> None:
    """Diff link health between two git revisions of REPO."""
    fmt = output_format.lower()
    if fmt not in {"pretty", "json", "markdown-comment"}:
        typer.echo(
            "Error: --format must be one of: pretty, json, markdown-comment",
            err=True,
        )
        raise typer.Exit(2)
    policy = fail_on.lower()
    if policy not in {"new-dead", "any-dead", "never"}:
        typer.echo(
            "Error: --fail-on must be one of: new-dead, any-dead, never",
            err=True,
        )
        raise typer.Exit(2)

    if not (repo / ".git").exists() and not (repo / "HEAD").exists():
        typer.echo(f"Error: {repo} is not a git repository.", err=True)
        raise typer.Exit(2)

    def _scan(path: Path) -> list[str]:
        return _collect_urls(path)

    cfg = ProbeConfig(concurrency=concurrency, timeout=timeout)

    def _probe(urls):
        url_list = list(urls)
        if not url_list:
            return {}
        results = asyncio.run(probe_urls(url_list, config=cfg))
        return {r.url: r.verdict.value for r in results}

    result = diff_mod.run_diff(repo, base, head, scan=_scan, probe=_probe)

    if fmt == "json":
        payload = diff_mod.render_json(result)
    elif fmt == "markdown-comment":
        payload = diff_mod.render_markdown_comment(result)
    else:
        payload = diff_mod.render_pretty(result)

    _emit(payload, output_file)
    raise typer.Exit(diff_mod.exit_code_for(result, fail_on=policy))  # type: ignore[arg-type]


if __name__ == "__main__":  # pragma: no cover
    app()

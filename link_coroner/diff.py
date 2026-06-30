"""Time-machine diff (#28).

Scan two git revisions of a repository and report which links died,
which were resurrected, which are still rotting, and which are brand new.

The module is intentionally small so it can be unit-tested without
actually shelling out to git or hitting the network: ``compute_diff``
operates on plain dicts of ``{url -> verdict}`` and the orchestration
helper ``run_diff`` accepts injectable ``scanner`` / ``prober``
callables that the CLI wires up to the real implementation.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# Categories produced by :func:`compute_diff`.
Category = Literal[
    "NEW_DEAD",      # URL exists in head, was alive (or missing) in base, dead in head
    "RESURRECTED",   # URL exists in both, was dead in base, alive in head
    "STILL_DEAD",    # URL exists in both and is dead in both
    "NEW_ALIVE",     # URL is new in head and alive
    "REMOVED",       # URL existed in base but is gone in head (regardless of base state)
]

ALL_CATEGORIES: tuple[Category, ...] = (
    "NEW_DEAD",
    "RESURRECTED",
    "STILL_DEAD",
    "NEW_ALIVE",
    "REMOVED",
)

# ``"DEAD"`` and ``"UNREACHABLE"`` both count as "not alive" for the diff.
_DEAD_LIKE = {"DEAD", "UNREACHABLE"}


def _is_dead(verdict: str | None) -> bool:
    return verdict is not None and verdict.upper() in _DEAD_LIKE


@dataclass(slots=True)
class DiffResult:
    base_ref: str
    head_ref: str
    categories: dict[Category, list[str]] = field(default_factory=dict)

    def count(self, cat: Category) -> int:
        return len(self.categories.get(cat, []))

    @property
    def summary(self) -> dict[str, int]:
        return {cat: self.count(cat) for cat in ALL_CATEGORIES}

    def to_dict(self) -> dict[str, object]:
        return {
            "base": self.base_ref,
            "head": self.head_ref,
            "summary": self.summary,
            "categories": {cat: sorted(self.categories.get(cat, [])) for cat in ALL_CATEGORIES},
        }


def compute_diff(
    base: dict[str, str],
    head: dict[str, str],
    *,
    base_ref: str = "base",
    head_ref: str = "head",
) -> DiffResult:
    """Categorize URLs by joining two ``{url -> verdict}`` maps."""
    out: dict[Category, list[str]] = {cat: [] for cat in ALL_CATEGORIES}

    base_urls = set(base)
    head_urls = set(head)

    for url in head_urls - base_urls:
        if _is_dead(head[url]):
            out["NEW_DEAD"].append(url)
        else:
            out["NEW_ALIVE"].append(url)

    for url in base_urls & head_urls:
        b_dead = _is_dead(base[url])
        h_dead = _is_dead(head[url])
        if h_dead and not b_dead:
            out["NEW_DEAD"].append(url)
        elif h_dead and b_dead:
            out["STILL_DEAD"].append(url)
        elif b_dead and not h_dead:
            out["RESURRECTED"].append(url)
        # alive -> alive: not interesting, drop on the floor.

    for url in base_urls - head_urls:
        out["REMOVED"].append(url)

    for cat in out:
        out[cat].sort()

    return DiffResult(base_ref=base_ref, head_ref=head_ref, categories=out)


# --- git-worktree orchestration ---------------------------------------------

def _run_git(args: list[str], cwd: Path) -> str:
    res = subprocess.run(  # noqa: S603 - args are caller-controlled identifiers
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout


@contextmanager
def git_worktree(repo: Path, ref: str):
    """Yield a temp path containing ``ref`` checked out via ``git worktree add``.

    Cleans up the worktree (and its temp dir) on exit, even on failure.
    """
    repo = repo.resolve()
    tmp = Path(tempfile.mkdtemp(prefix="link-coroner-diff-"))
    work = tmp / "wt"
    try:
        _run_git(["worktree", "add", "--detach", str(work), ref], cwd=repo)
        yield work
    finally:
        # ``git worktree remove`` is the polite way; fall back to rm.
        try:
            _run_git(["worktree", "remove", "--force", str(work)], cwd=repo)
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


# Pluggable callables — keeps the orchestration testable.
ScanFn = Callable[[Path], list[str]]
ProbeFn = Callable[[Iterable[str]], dict[str, str]]


def run_diff(
    repo: Path,
    base_ref: str,
    head_ref: str,
    *,
    scan: ScanFn,
    probe: ProbeFn,
) -> DiffResult:
    """High-level orchestrator: checkout both refs, scan, probe, diff.

    ``scan(path) -> list[url]``: extract URLs from a worktree.
    ``probe(urls) -> {url: verdict}``: classify URLs as ALIVE/DEAD/...
    """
    with git_worktree(repo, base_ref) as base_path:
        base_urls = scan(base_path)
    with git_worktree(repo, head_ref) as head_path:
        head_urls = scan(head_path)

    all_urls = sorted(set(base_urls) | set(head_urls))
    verdicts = probe(all_urls)

    base_map = {u: verdicts[u] for u in base_urls if u in verdicts}
    head_map = {u: verdicts[u] for u in head_urls if u in verdicts}

    return compute_diff(base_map, head_map, base_ref=base_ref, head_ref=head_ref)


# --- renderers --------------------------------------------------------------

_CATEGORY_LABELS: dict[Category, str] = {
    "NEW_DEAD": "🪦 New dead",
    "STILL_DEAD": "💀 Still dead",
    "RESURRECTED": "🧟 Resurrected",
    "NEW_ALIVE": "✨ New alive",
    "REMOVED": "🗑️  Removed",
}


def render_pretty(diff: DiffResult) -> str:
    lines: list[str] = []
    lines.append(f"link-coroner diff {diff.base_ref}..{diff.head_ref}")
    lines.append("")
    for cat in ALL_CATEGORIES:
        urls = diff.categories.get(cat, [])
        lines.append(f"{_CATEGORY_LABELS[cat]} ({len(urls)})")
        for u in urls:
            lines.append(f"  - {u}")
        if urls:
            lines.append("")
    if not any(diff.categories.get(c) for c in ALL_CATEGORIES):
        lines.append("(no changes)")
    return "\n".join(lines).rstrip() + "\n"


def render_json(diff: DiffResult) -> str:
    return json.dumps(diff.to_dict(), indent=2, sort_keys=True) + "\n"


def render_markdown_comment(diff: DiffResult) -> str:
    """A compact, GitHub-friendly comment body."""
    s = diff.summary
    headline = (
        f"### 🪦 link-coroner diff `{diff.base_ref}` → `{diff.head_ref}`\n\n"
        f"**+{s['NEW_DEAD']} dead** · "
        f"{s['RESURRECTED']} resurrected · "
        f"{s['STILL_DEAD']} still dead · "
        f"{s['NEW_ALIVE']} new alive · "
        f"{s['REMOVED']} removed\n"
    )

    sections: list[str] = [headline]
    for cat in ("NEW_DEAD", "RESURRECTED", "STILL_DEAD"):
        urls = diff.categories.get(cat, [])
        if not urls:
            continue
        sections.append(f"\n<details><summary>{_CATEGORY_LABELS[cat]} ({len(urls)})</summary>\n")
        for u in urls:
            sections.append(f"\n- {u}")
        sections.append("\n\n</details>\n")
    return "".join(sections)


# --- exit-code policy -------------------------------------------------------

FailOn = Literal["new-dead", "any-dead", "never"]


def exit_code_for(diff: DiffResult, *, fail_on: FailOn = "new-dead") -> int:
    if fail_on == "never":
        return 0
    if fail_on == "any-dead":
        if diff.count("NEW_DEAD") or diff.count("STILL_DEAD"):
            return 1
        return 0
    # default: only NEW_DEAD breaks the build
    return 1 if diff.count("NEW_DEAD") else 0

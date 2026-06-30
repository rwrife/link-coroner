"""Tests for time-machine diff (issue #28)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from link_coroner.cli import app
from link_coroner.diff import (
    DiffResult,
    compute_diff,
    exit_code_for,
    git_worktree,
    render_json,
    render_markdown_comment,
    render_pretty,
    run_diff,
)

runner = CliRunner()


def test_compute_diff_categorizes_correctly() -> None:
    base = {
        "https://still-dead.test": "DEAD",
        "https://will-resurrect.test": "DEAD",
        "https://still-alive.test": "ALIVE",
        "https://will-die.test": "ALIVE",
        "https://removed.test": "ALIVE",
    }
    head = {
        "https://still-dead.test": "DEAD",
        "https://will-resurrect.test": "ALIVE",
        "https://still-alive.test": "ALIVE",
        "https://will-die.test": "DEAD",
        "https://new-dead.test": "DEAD",
        "https://new-alive.test": "ALIVE",
    }
    diff = compute_diff(base, head, base_ref="HEAD~1", head_ref="HEAD")

    assert sorted(diff.categories["NEW_DEAD"]) == [
        "https://new-dead.test",
        "https://will-die.test",
    ]
    assert diff.categories["RESURRECTED"] == ["https://will-resurrect.test"]
    assert diff.categories["STILL_DEAD"] == ["https://still-dead.test"]
    assert diff.categories["NEW_ALIVE"] == ["https://new-alive.test"]
    assert diff.categories["REMOVED"] == ["https://removed.test"]
    # alive-only-in-both shouldn't show up anywhere
    for cat, urls in diff.categories.items():
        assert "https://still-alive.test" not in urls, cat


def test_compute_diff_unreachable_counts_as_dead() -> None:
    diff = compute_diff(
        {"https://x.test": "ALIVE"},
        {"https://x.test": "UNREACHABLE"},
    )
    assert diff.categories["NEW_DEAD"] == ["https://x.test"]


def test_summary_and_to_dict_round_trip() -> None:
    diff = compute_diff(
        {"https://a.test": "ALIVE"},
        {"https://a.test": "DEAD"},
        base_ref="main",
        head_ref="feature",
    )
    s = diff.summary
    assert s == {
        "NEW_DEAD": 1,
        "RESURRECTED": 0,
        "STILL_DEAD": 0,
        "NEW_ALIVE": 0,
        "REMOVED": 0,
    }
    payload = diff.to_dict()
    assert payload["base"] == "main"
    assert payload["head"] == "feature"
    assert payload["summary"]["NEW_DEAD"] == 1
    assert payload["categories"]["NEW_DEAD"] == ["https://a.test"]


def test_render_pretty_includes_categories() -> None:
    diff = compute_diff(
        {"https://a.test": "ALIVE"},
        {"https://a.test": "DEAD", "https://b.test": "ALIVE"},
    )
    out = render_pretty(diff)
    assert "New dead (1)" in out
    assert "https://a.test" in out
    assert "New alive (1)" in out


def test_render_json_is_valid() -> None:
    diff = compute_diff({}, {"https://a.test": "DEAD"})
    data = json.loads(render_json(diff))
    assert data["summary"]["NEW_DEAD"] == 1
    assert data["categories"]["NEW_DEAD"] == ["https://a.test"]


def test_render_markdown_comment_has_headline_and_details() -> None:
    diff = compute_diff(
        {"https://r.test": "DEAD"},
        {"https://r.test": "ALIVE", "https://d.test": "DEAD"},
        base_ref="main",
        head_ref="pr",
    )
    md = render_markdown_comment(diff)
    assert "link-coroner diff" in md
    assert "+1 dead" in md
    assert "1 resurrected" in md
    assert "https://d.test" in md
    assert "<details>" in md


def test_exit_code_policy() -> None:
    new_dead = DiffResult("a", "b", {"NEW_DEAD": ["x"]})
    still_dead = DiffResult("a", "b", {"STILL_DEAD": ["x"]})
    clean = DiffResult("a", "b", {})

    assert exit_code_for(new_dead) == 1
    assert exit_code_for(clean) == 0
    assert exit_code_for(still_dead) == 0  # default = new-dead only
    assert exit_code_for(still_dead, fail_on="any-dead") == 1
    assert exit_code_for(new_dead, fail_on="never") == 0


# --- integration with a real (tiny) git repo --------------------------------


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "README.md").write_text(
        "see https://example.test/old and https://example.test/keep\n",
        encoding="utf-8",
    )
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "base"], repo)
    (repo / "README.md").write_text(
        "see https://example.test/keep and https://example.test/new\n",
        encoding="utf-8",
    )
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "head"], repo)
    return repo


def test_git_worktree_yields_checked_out_path(tiny_repo: Path) -> None:
    with git_worktree(tiny_repo, "HEAD~1") as wt:
        text = (wt / "README.md").read_text(encoding="utf-8")
        assert "example.test/old" in text
        assert "example.test/new" not in text
    # worktree cleaned up — path gone
    assert not wt.exists()


def test_run_diff_orchestrator_uses_injected_callables(tiny_repo: Path) -> None:
    seen_paths: list[Path] = []

    def scan(path: Path) -> list[str]:
        seen_paths.append(path)
        # extract by simple substring
        text = (path / "README.md").read_text(encoding="utf-8")
        return [
            tok.rstrip(".,")
            for tok in text.split()
            if tok.startswith("https://")
        ]

    def probe(urls):
        # everything ending in /old is dead, everything else alive
        out: dict[str, str] = {}
        for u in urls:
            out[u] = "DEAD" if u.endswith("/old") else "ALIVE"
        return out

    result = run_diff(tiny_repo, "HEAD~1", "HEAD", scan=scan, probe=probe)
    assert len(seen_paths) == 2
    # 'old' was in base (DEAD) and is gone in head -> REMOVED
    assert "https://example.test/old" in result.categories["REMOVED"]
    # 'new' is brand new and alive -> NEW_ALIVE
    assert "https://example.test/new" in result.categories["NEW_ALIVE"]
    # 'keep' alive->alive -> not categorized
    for cat, urls in result.categories.items():
        assert "https://example.test/keep" not in urls, cat


# --- CLI smoke --------------------------------------------------------------


def test_cli_diff_rejects_bad_format(tiny_repo: Path) -> None:
    res = runner.invoke(
        app, ["diff", "HEAD~1", "HEAD", "--repo", str(tiny_repo), "--format", "bogus"]
    )
    assert res.exit_code == 2
    assert "format" in (res.output + (res.stderr if hasattr(res, "stderr") else "")).lower() \
        or "format" in res.output.lower()


def test_cli_diff_rejects_non_git_dir(tmp_path: Path) -> None:
    res = runner.invoke(
        app, ["diff", "HEAD~1", "HEAD", "--repo", str(tmp_path)]
    )
    assert res.exit_code == 2

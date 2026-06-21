"""Tests for the mortician auto-PR module (issue #8)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from link_coroner.mortician import (
    MorticianPolicy,
    build_pr_body,
    filter_snapshots,
    open_pull_request,
)
from link_coroner.rewrite import RewriteChange, RewriteResult, rewrite_files
from link_coroner.wayback import WaybackSnapshot


def _snap(url: str, snapshot: str | None) -> WaybackSnapshot:
    return WaybackSnapshot(url=url, snapshot_url=snapshot, timestamp="20210101000000")


# ---------------------------------------------------------------------------
# Policy parsing & filtering
# ---------------------------------------------------------------------------


def test_policy_empty_allows_everything():
    p = MorticianPolicy.empty()
    assert p.allows("https://example.com/a")
    assert p.allows("https://foo.bar.baz/x")


def test_policy_skip_exact_url():
    p = MorticianPolicy(skip_urls=frozenset({"https://keep.me/here"}))
    assert not p.allows("https://keep.me/here")
    assert p.allows("https://keep.me/other")


def test_policy_skip_host_and_subdomains():
    p = MorticianPolicy(skip_hosts=frozenset({"example.com"}))
    assert not p.allows("https://example.com/x")
    assert not p.allows("https://www.example.com/x")
    assert not p.allows("https://api.v2.example.com/x")
    assert p.allows("https://notexample.com/x")
    assert p.allows("https://example.org/x")


def test_policy_from_file(tmp_path: Path):
    policy_path = tmp_path / "policy.txt"
    policy_path.write_text(
        """
        # comments and blanks ignored

        https://keep.me/here
        host: skip.example.com
        host:   another.example
        """.strip()
        + "\n",
        encoding="utf-8",
    )
    p = MorticianPolicy.from_file(policy_path)
    assert "https://keep.me/here" in p.skip_urls
    assert "skip.example.com" in p.skip_hosts
    assert "another.example" in p.skip_hosts
    assert not p.allows("https://keep.me/here")
    assert not p.allows("https://sub.skip.example.com/x")


def test_filter_snapshots_partitions_and_drops_empty():
    policy = MorticianPolicy(skip_hosts=frozenset({"blocked.example"}))
    snapshots = {
        "https://blocked.example/x": _snap("https://blocked.example/x", "https://wb/x"),
        "https://ok.example/a": _snap("https://ok.example/a", "https://wb/a"),
        "https://ok.example/b": _snap("https://ok.example/b", None),  # no snapshot
    }
    kept, skipped = filter_snapshots(snapshots, policy)
    assert "https://ok.example/a" in kept
    assert "https://ok.example/b" not in kept  # no snapshot
    assert "https://blocked.example/x" not in kept
    assert skipped == ["https://blocked.example/x"]


# ---------------------------------------------------------------------------
# PR body composition
# ---------------------------------------------------------------------------


def test_build_pr_body_contains_summary_and_sections(tmp_path: Path):
    result = RewriteResult(
        changes=[
            RewriteChange(
                path=tmp_path / "README.md",
                url="https://dead.example/x",
                replacement="https://web.archive.org/web/x",
                count=2,
            )
        ],
        files_modified=1,
        dry_run=False,
    )
    body = build_pr_body(
        result,
        skipped_by_policy=["https://blocked.example/y"],
        no_snapshot=["https://orphan.example/z"],
    )
    assert "Mortician auto-PR" in body
    assert "Files affected:** 1" in body
    assert "Replacements:** 1" in body
    assert "https://dead.example/x" in body
    assert "Skipped (per allowlist policy)" in body
    assert "https://blocked.example/y" in body
    assert "No Wayback snapshot available" in body
    assert "https://orphan.example/z" in body


# ---------------------------------------------------------------------------
# PR opening (subprocess injection)
# ---------------------------------------------------------------------------


def test_open_pull_request_invokes_expected_commands(tmp_path: Path):
    calls: list[list[str]] = []

    def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:2] == ["gh", "pr"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/x/y/pull/42\n", stderr=""
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    result = open_pull_request(
        tmp_path,
        branch="mortician/test",
        title="title",
        body="body",
        base="main",
        runner=fake_runner,
    )

    assert result.pushed is True
    assert result.pr_url == "https://github.com/x/y/pull/42"
    cmd_starts = [c[:2] for c in calls]
    assert ["git", "checkout"] in cmd_starts
    assert ["git", "add"] in cmd_starts
    assert ["git", "commit"] in cmd_starts
    assert ["git", "push"] in cmd_starts
    assert ["gh", "pr"] in cmd_starts
    # Verify the gh call carries the title and body args.
    gh_call = next(c for c in calls if c[:2] == ["gh", "pr"])
    assert "--title" in gh_call and "title" in gh_call
    assert "--body" in gh_call and "body" in gh_call
    assert "--base" in gh_call and "main" in gh_call
    assert "--head" in gh_call and "mortician/test" in gh_call


# ---------------------------------------------------------------------------
# Integration: rewrite + filter + body
# ---------------------------------------------------------------------------


def test_rewrite_uses_filtered_snapshots(tmp_path: Path):
    f = tmp_path / "README.md"
    f.write_text(
        "alive: https://blocked.example/x\nalso: https://ok.example/a\n",
        encoding="utf-8",
    )
    policy = MorticianPolicy(skip_hosts=frozenset({"blocked.example"}))
    snapshots = {
        "https://blocked.example/x": _snap("https://blocked.example/x", "https://wb/x"),
        "https://ok.example/a": _snap("https://ok.example/a", "https://wb/a"),
    }
    kept, skipped = filter_snapshots(snapshots, policy)
    res = rewrite_files(tmp_path, kept, dry_run=False, backup=False)
    text = f.read_text(encoding="utf-8")
    assert "https://blocked.example/x" in text  # untouched
    assert "https://wb/a" in text  # resurrected
    assert skipped == ["https://blocked.example/x"]
    assert res.files_modified == 1

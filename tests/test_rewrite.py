"""Tests for the in-place rewrite module (M5)."""

from __future__ import annotations

from pathlib import Path

from link_coroner.rewrite import rewrite_files
from link_coroner.wayback import WaybackSnapshot


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _snap(url: str, snapshot: str | None) -> WaybackSnapshot:
    return WaybackSnapshot(url=url, snapshot_url=snapshot, timestamp="20210101000000")


def test_rewrite_dry_run_does_not_touch_files(tmp_path: Path):
    f = tmp_path / "README.md"
    _write(f, "see [docs](https://gone.example/path) for more")
    snapshots = {
        "https://gone.example/path": _snap(
            "https://gone.example/path",
            "https://web.archive.org/web/20210101/https://gone.example/path",
        )
    }

    result = rewrite_files(tmp_path, snapshots, dry_run=True)

    assert result.dry_run is True
    assert result.files_modified == 1
    assert len(result.changes) == 1
    # File untouched.
    assert "gone.example/path" in f.read_text()
    assert "web.archive.org" not in f.read_text()


def test_rewrite_apply_patches_and_backs_up(tmp_path: Path):
    f = tmp_path / "notes.md"
    _write(f, "first https://gone.example/a then https://gone.example/a again")
    snapshots = {
        "https://gone.example/a": _snap(
            "https://gone.example/a",
            "https://web.archive.org/web/20210101/https://gone.example/a",
        )
    }

    result = rewrite_files(tmp_path, snapshots, dry_run=False, backup=True)

    assert result.dry_run is False
    assert result.files_modified == 1
    assert result.changes[0].count == 2
    new_text = f.read_text()
    assert "web.archive.org" in new_text
    assert "gone.example/a" not in new_text.replace("web.archive.org/web/20210101/https://gone.example/a", "")
    # Backup preserved the original.
    backup = f.with_suffix(".md.bak")
    assert backup.exists()
    assert "gone.example/a" in backup.read_text()


def test_rewrite_skips_snapshots_without_url(tmp_path: Path):
    f = tmp_path / "doc.md"
    _write(f, "https://gone.example/")
    snapshots = {"https://gone.example/": _snap("https://gone.example/", None)}

    result = rewrite_files(tmp_path, snapshots, dry_run=False)

    assert result.files_modified == 0
    assert result.changes == []
    assert "gone.example" in f.read_text()


def test_rewrite_no_op_when_url_absent(tmp_path: Path):
    f = tmp_path / "doc.md"
    _write(f, "nothing to see here")
    snapshots = {
        "https://gone.example/": _snap(
            "https://gone.example/",
            "https://web.archive.org/web/2021/https://gone.example/",
        )
    }

    result = rewrite_files(tmp_path, snapshots, dry_run=False)

    assert result.files_modified == 0
    assert result.changes == []

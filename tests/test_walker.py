from pathlib import Path

from link_coroner.scanner.walker import SKIP_DIRS, walk_paths


def test_walks_supported_files(tmp_path: Path):
    (tmp_path / "a.md").write_text("hi")
    (tmp_path / "b.txt").write_text("hi")
    (tmp_path / "c.bin").write_text("nope")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "d.py").write_text("hi")

    found = {p.name for p in walk_paths(tmp_path)}
    assert found == {"a.md", "b.txt", "d.py"}


def test_skips_ignored_dirs(tmp_path: Path):
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "junk.md").write_text("ignore me")
    (tmp_path / "ok.md").write_text("ok")

    found = {p.name for p in walk_paths(tmp_path)}
    assert found == {"ok.md"}


def test_single_file_passthrough(tmp_path: Path):
    f = tmp_path / "solo.md"
    f.write_text("hi")
    assert list(walk_paths(f)) == [f]


def test_single_unsupported_file(tmp_path: Path):
    f = tmp_path / "solo.bin"
    f.write_text("hi")
    assert list(walk_paths(f)) == []


def test_skip_dirs_includes_git():
    assert ".git" in SKIP_DIRS
    assert "node_modules" in SKIP_DIRS

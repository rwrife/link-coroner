from pathlib import Path

from typer.testing import CliRunner

from link_coroner import __version__
from link_coroner.cli import app

runner = CliRunner()


def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_scan_lists_urls(tmp_path: Path):
    (tmp_path / "readme.md").write_text(
        "check https://example.com and https://foo.dev/path"
    )
    result = runner.invoke(app, ["scan", str(tmp_path)])
    assert result.exit_code == 0
    assert "https://example.com" in result.stdout
    assert "https://foo.dev/path" in result.stdout
    assert "found" in result.stdout


def test_scan_dedupes_by_default(tmp_path: Path):
    (tmp_path / "a.md").write_text("https://dup.dev")
    (tmp_path / "b.md").write_text("https://dup.dev")
    result = runner.invoke(app, ["scan", str(tmp_path)])
    assert result.exit_code == 0
    # 1 unique URL reported
    assert "1" in result.stdout

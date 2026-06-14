"""Filesystem walker — yields files we know how to extract URLs from."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

# Files we extract URLs from. Source-code files are scanned for URLs inside
# comments / strings via the same heuristic regex extractor for now.
SUPPORTED_SUFFIXES: frozenset[str] = frozenset(
    {
        ".md",
        ".mdx",
        ".txt",
        ".rst",
        ".html",
        ".htm",
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
    }
)

# Directories we never want to descend into.
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".tox",
        ".next",
        ".cache",
    }
)


def walk_paths(root: Path) -> Iterator[Path]:
    """Yield supported files under ``root`` (or ``root`` itself if it's a file)."""
    root = Path(root)
    if root.is_file():
        if root.suffix.lower() in SUPPORTED_SUFFIXES:
            yield root
        return

    # Manual walk so we can prune SKIP_DIRS efficiently.
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            name = entry.name
            if entry.is_dir():
                if name in SKIP_DIRS or name.startswith("."):
                    # Skip hidden + ignored dirs (but root itself was allowed above).
                    continue
                stack.append(entry)
            elif entry.is_file() and entry.suffix.lower() in SUPPORTED_SUFFIXES:
                yield entry

"""Patch dead URLs in-place with their Wayback resurrection (M5).

Used by ``link-coroner rewrite``. Dry-run by default so users can't shoot
themselves in the foot. When ``backup=True`` (default), each touched
file gets a sibling ``<name>.bak`` written before overwriting.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .scanner.walker import walk_paths
from .wayback import WaybackSnapshot


@dataclass(slots=True)
class RewriteChange:
    path: Path
    url: str
    replacement: str
    count: int


@dataclass(slots=True)
class RewriteResult:
    changes: list[RewriteChange]
    files_modified: int
    dry_run: bool


def _replacements_for(
    snapshots: Mapping[str, WaybackSnapshot],
) -> dict[str, str]:
    """Build a {dead_url: snapshot_url} mapping, skipping entries without a snapshot."""
    out: dict[str, str] = {}
    for url, snap in snapshots.items():
        if snap and snap.snapshot_url:
            out[url] = snap.snapshot_url
    return out


def rewrite_files(
    root: Path,
    snapshots: Mapping[str, WaybackSnapshot],
    *,
    dry_run: bool = True,
    backup: bool = True,
) -> RewriteResult:
    """Patch every supported file under ``root``, replacing dead URLs with snapshots.

    Returns a structured :class:`RewriteResult` describing what was (or
    would be) changed. When ``dry_run`` is ``True`` (default), no files
    are touched.
    """
    repl = _replacements_for(snapshots)
    changes: list[RewriteChange] = []
    files_modified = 0

    if not repl:
        return RewriteResult(changes=[], files_modified=0, dry_run=dry_run)

    for file_path in walk_paths(Path(root)):
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        new_text = text
        file_changes: list[RewriteChange] = []
        for dead, alive in repl.items():
            count = new_text.count(dead)
            if count:
                new_text = new_text.replace(dead, alive)
                file_changes.append(
                    RewriteChange(
                        path=file_path,
                        url=dead,
                        replacement=alive,
                        count=count,
                    )
                )

        if file_changes and new_text != text:
            if not dry_run:
                if backup:
                    backup_path = file_path.with_suffix(file_path.suffix + ".bak")
                    try:
                        backup_path.write_text(text, encoding="utf-8")
                    except OSError:
                        # If we can't write a backup, skip this file to be safe.
                        continue
                file_path.write_text(new_text, encoding="utf-8")
            files_modified += 1
            changes.extend(file_changes)

    return RewriteResult(changes=changes, files_modified=files_modified, dry_run=dry_run)

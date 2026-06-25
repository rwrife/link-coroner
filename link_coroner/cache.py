"""SQLite-backed probe history cache.

Used by :mod:`link_coroner.heatmap` to render link-rot heatmaps and by
the ``autopsy --cache`` flag to persist probe events across runs.

Schema is versioned via ``PRAGMA user_version`` so future migrations
can extend the table without breaking existing caches.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .diagnosis import Cause, diagnose
from .forensics.probe import ProbeResult, Verdict

SCHEMA_VERSION = 1


@dataclass(slots=True, frozen=True)
class ProbeEvent:
    """A single recorded probe observation."""

    url: str
    host: str
    file_path: str | None
    verdict: str
    cause: str
    observed_at: int  # unix epoch seconds


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _migrate(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA user_version")
    version = cur.fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    if version < 1:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS probes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                host TEXT NOT NULL,
                file_path TEXT,
                verdict TEXT NOT NULL,
                cause TEXT NOT NULL,
                observed_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_probes_url ON probes(url);
            CREATE INDEX IF NOT EXISTS idx_probes_host ON probes(host);
            CREATE INDEX IF NOT EXISTS idx_probes_observed_at ON probes(observed_at);
            """
        )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


class ProbeCache:
    """Tiny SQLite wrapper for storing/reading probe history.

    Designed to be opened fresh per command; not thread-safe.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        _migrate(self._conn)

    # context-manager sugar -------------------------------------------------
    def __enter__(self) -> ProbeCache:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # writes ----------------------------------------------------------------
    def record_probe_results(
        self,
        results: Iterable[ProbeResult],
        *,
        observed_at: int | None = None,
        url_to_file: dict[str, str] | None = None,
    ) -> int:
        """Persist a batch of probe results. Returns rows inserted."""
        ts = int(observed_at if observed_at is not None else time.time())
        rows: list[tuple[str, str, str | None, str, str, int]] = []
        for result in results:
            cause = diagnose(result).value
            rows.append(
                (
                    result.url,
                    _host_of(result.url),
                    (url_to_file or {}).get(result.url),
                    result.verdict.value,
                    cause,
                    ts,
                )
            )
        if not rows:
            return 0
        with self._conn:
            self._conn.executemany(
                "INSERT INTO probes (url, host, file_path, verdict, cause, observed_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def record_events(self, events: Iterable[ProbeEvent]) -> int:
        rows = [
            (e.url, e.host, e.file_path, e.verdict, e.cause, e.observed_at) for e in events
        ]
        if not rows:
            return 0
        with self._conn:
            self._conn.executemany(
                "INSERT INTO probes (url, host, file_path, verdict, cause, observed_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    # reads -----------------------------------------------------------------
    def iter_events(
        self,
        *,
        since: int | None = None,
        until: int | None = None,
    ) -> Iterator[ProbeEvent]:
        sql = (
            "SELECT url, host, file_path, verdict, cause, observed_at FROM probes"
        )
        clauses: list[str] = []
        params: list[object] = []
        if since is not None:
            clauses.append("observed_at >= ?")
            params.append(int(since))
        if until is not None:
            clauses.append("observed_at <= ?")
            params.append(int(until))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY observed_at ASC, id ASC"
        for row in self._conn.execute(sql, params):
            yield ProbeEvent(
                url=row["url"],
                host=row["host"],
                file_path=row["file_path"],
                verdict=row["verdict"],
                cause=row["cause"],
                observed_at=int(row["observed_at"]),
            )

    def all_events(self, **kwargs: object) -> list[ProbeEvent]:
        return list(self.iter_events(**kwargs))  # type: ignore[arg-type]


def is_dead_verdict(verdict: str) -> bool:
    """Return True when a verdict string represents a deceased URL."""
    return verdict in {Verdict.DEAD.value, Verdict.UNREACHABLE.value}


__all__ = [
    "Cause",
    "ProbeCache",
    "ProbeEvent",
    "SCHEMA_VERSION",
    "is_dead_verdict",
]

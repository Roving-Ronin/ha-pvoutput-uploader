"""SQLite history log of 5 minute samples.

Every method is blocking and must run in the executor.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import sqlite3
import threading
from typing import Any

from .const import FIELD_KEYS

PENDING = 0
DONE = 1
EXPIRED = 2
FAILED = 3


@dataclass(slots=True)
class Sample:
    """One 5 minute row. ``ts`` is the UTC epoch of the slot end."""

    ts: int
    values: dict[str, float | None]
    uploaded: int = PENDING


class SampleStore:
    """History log kept next to the Home Assistant configuration."""

    def __init__(self, path: str) -> None:
        """Initialize the store."""
        self._path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        """Open the database and create or extend the tables."""
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS samples ("
            " ts INTEGER PRIMARY KEY,"
            " uploaded INTEGER NOT NULL DEFAULT 0)"
        )
        existing = {row[1] for row in conn.execute("PRAGMA table_info(samples)")}
        for key in FIELD_KEYS:
            if key not in existing:
                conn.execute(f"ALTER TABLE samples ADD COLUMN {key} REAL")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS samples_pending ON samples (uploaded, ts)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute("CREATE TABLE IF NOT EXISTS output_queue (day TEXT PRIMARY KEY)")
        queue_cols = {row[1] for row in conn.execute("PRAGMA table_info(output_queue)")}
        if "failed" not in queue_cols:
            conn.execute(
                "ALTER TABLE output_queue ADD COLUMN failed INTEGER NOT NULL DEFAULT 0"
            )
        if "error" not in queue_cols:
            conn.execute("ALTER TABLE output_queue ADD COLUMN error TEXT")
        # When PVOutput first did not confirm the status of a slot (slot end time).
        conn.execute(
            "CREATE TABLE IF NOT EXISTS unconfirmed"
            " (slot INTEGER PRIMARY KEY, since REAL NOT NULL)"
        )
        conn.commit()
        self._conn = conn

    def close(self) -> None:
        """Close the database."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> list[tuple]:
        assert self._conn is not None
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
            self._conn.commit()
            return rows

    def _executemany(self, sql: str, params: list[tuple]) -> None:
        assert self._conn is not None
        with self._lock:
            self._conn.executemany(sql, params)
            self._conn.commit()

    @staticmethod
    def _to_sample(row: tuple) -> Sample:
        return Sample(row[0], dict(zip(FIELD_KEYS, row[2:], strict=True)), row[1])

    _SELECT = f"SELECT ts, uploaded, {', '.join(FIELD_KEYS)} FROM samples"

    # Meta

    def get_meta(self, key: str) -> str | None:
        """Return a stored setting."""
        rows = self._execute("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0][0] if rows else None

    def set_meta(self, key: str, value: str | None) -> None:
        """Store or clear a setting."""
        if value is None:
            self._execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self._execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
            )

    # Samples

    def add_samples(
        self, samples: list[Sample], *, overwrite: bool = False
    ) -> None:
        """Insert samples, replacing existing rows only when asked."""
        if not samples:
            return
        verb = "INSERT OR REPLACE" if overwrite else "INSERT OR IGNORE"
        cols = ", ".join(FIELD_KEYS)
        marks = ", ".join("?" * (len(FIELD_KEYS) + 2))
        self._executemany(
            f"{verb} INTO samples (ts, uploaded, {cols}) VALUES ({marks})",
            [
                (s.ts, s.uploaded, *(s.values.get(k) for k in FIELD_KEYS))
                for s in samples
            ],
        )

    def existing_ts(self, start: int, end: int) -> list[int]:
        """Return the sorted timestamps stored in [start, end)."""
        rows = self._execute(
            "SELECT ts FROM samples WHERE ts >= ? AND ts < ? ORDER BY ts", (start, end)
        )
        return [r[0] for r in rows]

    def samples_between(self, start: int, end: int) -> list[Sample]:
        """Return samples with start <= ts < end, oldest first."""
        rows = self._execute(
            f"{self._SELECT} WHERE ts >= ? AND ts < ? ORDER BY ts", (start, end)
        )
        return [self._to_sample(r) for r in rows]

    def first_pending(self, min_ts: int, max_ts: int) -> int | None:
        """Return the oldest pending timestamp in [min_ts, max_ts]."""
        rows = self._execute(
            "SELECT MIN(ts) FROM samples WHERE uploaded = ? AND ts >= ? AND ts <= ?",
            (PENDING, min_ts, max_ts),
        )
        return rows[0][0] if rows else None

    def first_unsent(self) -> int | None:
        """Return the oldest timestamp not confirmed sent (pending, failed or expired)."""
        rows = self._execute("SELECT MIN(ts) FROM samples WHERE uploaded != ?", (DONE,))
        return rows[0][0] if rows else None

    def pending_ts(self, min_ts: int, max_ts: int, limit: int) -> list[int]:
        """Return pending timestamps in [min_ts, max_ts], oldest first."""
        rows = self._execute(
            "SELECT ts FROM samples WHERE uploaded = ? AND ts >= ? AND ts <= ?"
            " ORDER BY ts LIMIT ?",
            (PENDING, min_ts, max_ts, limit),
        )
        return [r[0] for r in rows]

    def expire_before(self, before: int) -> list[int]:
        """Mark pending rows older than ``before`` as expired, returning them."""
        rows = self._execute(
            "SELECT ts FROM samples WHERE uploaded = ? AND ts < ?", (PENDING, before)
        )
        self._execute(
            "UPDATE samples SET uploaded = ? WHERE uploaded = ? AND ts < ?",
            (EXPIRED, PENDING, before),
        )
        self._execute("DELETE FROM unconfirmed WHERE slot < ?", (before,))
        return [r[0] for r in rows]

    def mark_range(self, start: int, end: int, state: int) -> None:
        """Set the upload state of rows with start <= ts < end.

        Any retry window of those slots ends: they were sent, given up on, or
        queued again on purpose.
        """
        self._execute(
            "UPDATE samples SET uploaded = ? WHERE ts >= ? AND ts < ?",
            (state, start, end),
        )
        self._execute(
            "DELETE FROM unconfirmed WHERE slot >= ? AND slot < ?", (start, end)
        )

    def unconfirmed_since(self, slot: int, now: float) -> float:
        """Return when PVOutput first did not confirm ``slot``, recording ``now`` if new."""
        self._execute(
            "INSERT OR IGNORE INTO unconfirmed (slot, since) VALUES (?, ?)", (slot, now)
        )
        return self._execute("SELECT since FROM unconfirmed WHERE slot = ?", (slot,))[0][0]

    def purge(self, before: int) -> None:
        """Delete rows older than ``before``."""
        self._execute("DELETE FROM samples WHERE ts < ?", (before,))
        self._execute("DELETE FROM unconfirmed WHERE slot < ?", (before,))

    # Daily output queue

    def queue_outputs(self, days: Iterable[str]) -> None:
        """Queue daily outputs (days as YYYYMMDD); rejected days are queued again."""
        self._executemany(
            "INSERT INTO output_queue (day) VALUES (?)"
            " ON CONFLICT(day) DO UPDATE SET failed = 0, error = NULL",
            [(d,) for d in days],
        )

    def queued_outputs(self, limit: int) -> list[str]:
        """Return days waiting to be sent, oldest first."""
        rows = self._execute(
            "SELECT day FROM output_queue WHERE failed = 0 ORDER BY day LIMIT ?", (limit,)
        )
        return [r[0] for r in rows]

    def reject_output(self, day: str, error: str) -> None:
        """Keep a day PVOutput rejected, with the reason, out of the sending queue."""
        self._execute(
            "UPDATE output_queue SET failed = 1, error = ? WHERE day = ?", (error, day)
        )

    def rejected_outputs(self) -> list[tuple[str, str]]:
        """Return the rejected days and PVOutput's reasons, oldest first."""
        return [
            (r[0], r[1] or "")
            for r in self._execute(
                "SELECT day, error FROM output_queue WHERE failed = 1 ORDER BY day"
            )
        ]

    def dequeue_outputs(self, days: Iterable[str]) -> None:
        """Remove days from the output queue."""
        self._executemany(
            "DELETE FROM output_queue WHERE day = ?", [(d,) for d in days]
        )

    # Summary

    def summary(self) -> dict[str, Any]:
        """Return counts for the diagnostic sensors."""
        counts = dict(
            self._execute("SELECT uploaded, COUNT(*) FROM samples GROUP BY uploaded")
        )
        oldest, newest = self._execute("SELECT MIN(ts), MAX(ts) FROM samples")[0]
        queued, rejected = self._execute(
            "SELECT COUNT(*) - COALESCE(SUM(failed), 0), COALESCE(SUM(failed), 0)"
            " FROM output_queue"
        )[0]
        return {
            "total": sum(counts.values()),
            "pending": counts.get(PENDING, 0),
            "failed": counts.get(FAILED, 0),
            "expired": counts.get(EXPIRED, 0),
            "oldest": oldest,
            "newest": newest,
            "queued_outputs": queued,
            "failed_outputs": rejected,
        }

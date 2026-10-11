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
# Sent without values PVOutput rejected; counts as unsent for a reupload.
PARTIAL = 4
# Taken off the queue by the user; still unsent, so a reupload finds it.
CLEARED = 5


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
        if "no_battery" not in existing:
            # Set while a status was last sent without its battery values.
            conn.execute(
                "ALTER TABLE samples ADD COLUMN no_battery INTEGER NOT NULL DEFAULT 0"
            )
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
        # PVOutput history (archive): hourly values per field, and the days
        # fetched (detail 1: PVOutput had statuses, 0: it had none).
        conn.execute(
            "CREATE TABLE IF NOT EXISTS archive ("
            " hour INTEGER NOT NULL, field TEXT NOT NULL,"
            " mean REAL, min REAL, max REAL, energy REAL,"
            " PRIMARY KEY (field, hour))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS archive_days"
            " (day TEXT PRIMARY KEY, detail INTEGER NOT NULL)"
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

    def merge_samples(self, samples: list[Sample], *, resend: bool) -> int:
        """Add samples without overwriting recorded values.

        Missing rows are inserted with the sample's upload state. In existing rows
        only empty fields are filled; such a row is queued again when ``resend``.
        Returns how many rows were inserted or filled.
        """
        assert self._conn is not None
        cols = ", ".join(FIELD_KEYS)
        changed = 0
        with self._lock:
            for sample in samples:
                row = self._conn.execute(
                    f"SELECT {cols} FROM samples WHERE ts = ?", (sample.ts,)
                ).fetchone()
                if row is None:
                    marks = ", ".join("?" * (len(FIELD_KEYS) + 2))
                    self._conn.execute(
                        f"INSERT INTO samples (ts, uploaded, {cols}) VALUES ({marks})",
                        (
                            sample.ts,
                            sample.uploaded,
                            *(sample.values.get(k) for k in FIELD_KEYS),
                        ),
                    )
                    changed += 1
                    continue
                have = dict(zip(FIELD_KEYS, row, strict=True))
                fill = {
                    k: v
                    for k, v in sample.values.items()
                    if v is not None and k in have and have[k] is None
                }
                if not fill:
                    continue
                sets = ", ".join(f"{k} = ?" for k in fill)
                params: list[Any] = list(fill.values())
                if resend:
                    sets += ", uploaded = ?"
                    params.append(PENDING)
                self._conn.execute(
                    f"UPDATE samples SET {sets} WHERE ts = ?", (*params, sample.ts)
                )
                changed += 1
            self._conn.commit()
        return changed

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

    def set_no_battery(self, start: int, end: int, without: bool) -> None:
        """Record whether rows with start <= ts < end were sent without battery values."""
        self._execute(
            "UPDATE samples SET no_battery = ? WHERE ts >= ? AND ts < ?",
            (1 if without else 0, start, end),
        )

    def clear_pending(self, before: int) -> int:
        """Take pending rows older than ``before`` off the queue (cleared).

        Returns how many rows were cleared. Their retry windows end too.
        """
        count = self._execute(
            "SELECT COUNT(*) FROM samples WHERE uploaded = ? AND ts < ?",
            (PENDING, before),
        )[0][0]
        self._execute(
            "UPDATE samples SET uploaded = ? WHERE uploaded = ? AND ts < ?",
            (CLEARED, PENDING, before),
        )
        self._execute("DELETE FROM unconfirmed WHERE slot < ?", (before,))
        return count

    def clear_queued_outputs(self) -> int:
        """Drop daily outputs waiting to be sent (rejected ones stay listed)."""
        count = self._execute("SELECT COUNT(*) FROM output_queue WHERE failed = 0")[0][0]
        self._execute("DELETE FROM output_queue WHERE failed = 0")
        return count

    def purge(self, before: int) -> None:
        """Delete rows older than ``before``."""
        self._execute("DELETE FROM samples WHERE ts < ?", (before,))
        self._execute("DELETE FROM unconfirmed WHERE slot < ?", (before,))

    def latest_values(self) -> dict[str, tuple[int, float]]:
        """Return the latest recorded (timestamp, value) of each field."""
        latest = {}
        for key in FIELD_KEYS:
            rows = self._execute(
                f"SELECT ts, {key} FROM samples WHERE {key} IS NOT NULL"
                " ORDER BY ts DESC LIMIT 1"
            )
            if rows:
                latest[key] = (rows[0][0], rows[0][1])
        return latest

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

    # PVOutput history archive

    def archive_replace(
        self, start: int, end: int, rows: list[tuple[int, str, Any, Any, Any, Any]]
    ) -> None:
        """Replace the archive hours in [start, end) with ``rows``.

        Rows are (hour, field, mean, min, max, energy).
        """
        assert self._conn is not None
        with self._lock:
            self._conn.execute(
                "DELETE FROM archive WHERE hour >= ? AND hour < ?", (start, end)
            )
            self._conn.executemany(
                "INSERT OR REPLACE INTO archive (hour, field, mean, min, max, energy)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()

    def archive_mark_day(self, day: str, detail: bool) -> None:
        """Note that ``day`` (yyyy-mm-dd) was fetched, and whether it had detail."""
        self._execute(
            "INSERT OR REPLACE INTO archive_days (day, detail) VALUES (?, ?)",
            (day, int(detail)),
        )

    def archive_last_day(self) -> str | None:
        """Return the newest day fetched (yyyy-mm-dd)."""
        return self._execute("SELECT MAX(day) FROM archive_days")[0][0]

    def archive_fields(self) -> list[str]:
        """Return the fields the archive holds."""
        return [r[0] for r in self._execute("SELECT DISTINCT field FROM archive")]

    def archive_rows(
        self, field: str, since: int = 0, until: int | None = None
    ) -> list[tuple[int, Any, Any, Any, Any]]:
        """Return (hour, mean, min, max, energy) of a field, oldest first."""
        return [
            tuple(r)
            for r in self._execute(
                "SELECT hour, mean, min, max, energy FROM archive"
                " WHERE field = ? AND hour >= ? AND hour < ? ORDER BY hour",
                (field, since, until if until is not None else 2**62),
            )
        ]

    def archive_energy_before(self, field: str, hour: int) -> float:
        """Return the energy of a field archived before ``hour``."""
        return self._execute(
            "SELECT COALESCE(SUM(energy), 0) FROM archive WHERE field = ? AND hour < ?",
            (field, hour),
        )[0][0]

    def archive_summary(self) -> dict[str, Any]:
        """Return the days fetched, with and without detail, and the first day."""
        first, detail, empty = self._execute(
            "SELECT MIN(CASE WHEN detail = 1 THEN day END),"
            " COALESCE(SUM(detail), 0), COUNT(*) - COALESCE(SUM(detail), 0)"
            " FROM archive_days"
        )[0]
        return {"first": first, "days": detail, "empty_days": empty}

    def archive_clear(self) -> None:
        """Delete the archive (the site is removed)."""
        self._execute("DELETE FROM archive")
        self._execute("DELETE FROM archive_days")

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
            "partial": counts.get(PARTIAL, 0),
            "cleared": counts.get(CLEARED, 0),
            "without_battery": self._execute(
                "SELECT COUNT(*) FROM samples WHERE no_battery = 1"
            )[0][0],
            "expired": counts.get(EXPIRED, 0),
            "oldest": oldest,
            "newest": newest,
            "queued_outputs": queued,
            "failed_outputs": rejected,
        }

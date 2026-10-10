"""The SQLite history log."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from custom_components.pvoutput_uploader.const import FIELD_KEYS
from custom_components.pvoutput_uploader.store import (
    DONE,
    EXPIRED,
    FAILED,
    PENDING,
    Sample,
    SampleStore,
)


def make_store(tmp_path: Path) -> SampleStore:
    store = SampleStore(str(tmp_path / "log.db"))
    store.open()
    return store


def test_add_and_read(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.add_samples(
        [Sample(300, {"v1": 10.0, "v5": 21.5}), Sample(600, {"v2": 500.0}, DONE)]
    )
    rows = store.samples_between(0, 601)
    assert [r.ts for r in rows] == [300, 600]
    assert rows[0].values["v1"] == 10.0
    assert rows[0].values["v5"] == 21.5
    assert rows[0].values["b1"] is None
    assert set(rows[0].values) == set(FIELD_KEYS)
    assert rows[1].uploaded == DONE
    assert store.samples_between(300, 600) == rows[:1]  # end is exclusive
    store.close()


def test_insert_keeps_existing_unless_overwrite(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.add_samples([Sample(300, {"v1": 1.0}, DONE)])
    store.add_samples([Sample(300, {"v1": 2.0})])
    assert store.samples_between(0, 1000)[0].values["v1"] == 1.0
    store.add_samples([Sample(300, {"v1": 3.0})], overwrite=True)
    row = store.samples_between(0, 1000)[0]
    assert row.values["v1"] == 3.0
    assert row.uploaded == PENDING
    store.close()


def test_pending_mark_and_expire(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.add_samples([Sample(ts, {"v2": 1.0}) for ts in range(300, 3001, 300)])
    assert store.pending_ts(0, 3000, 3) == [300, 600, 900]
    assert store.pending_ts(600, 1200, 10) == [600, 900, 1200]
    assert store.first_pending(1000, 3000) == 1200

    store.mark_range(300, 901, DONE)
    assert store.pending_ts(0, 3000, 2) == [1200, 1500]

    assert store.expire_before(1500) == [1200]
    assert store.samples_between(1200, 1201)[0].uploaded == EXPIRED
    assert store.expire_before(1500) == []

    store.mark_range(1500, 1501, FAILED)
    assert store.first_unsent() == 1200  # expired counts as unsent
    summary = store.summary()
    assert summary["total"] == 10
    assert summary["pending"] == 5
    assert summary["failed"] == 1
    assert summary["expired"] == 1
    assert summary["oldest"] == 300
    assert summary["newest"] == 3000
    store.close()


def test_existing_ts_and_purge(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.add_samples([Sample(ts, {}) for ts in (300, 600, 900)])
    assert store.existing_ts(300, 900) == [300, 600]
    store.purge(600)
    assert store.existing_ts(0, 10_000) == [600, 900]
    store.close()


def test_meta(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.get_meta("x") is None
    store.set_meta("x", "1")
    store.set_meta("x", "2")
    assert store.get_meta("x") == "2"
    store.set_meta("x", None)
    assert store.get_meta("x") is None
    store.close()


def test_output_queue(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.queue_outputs(["20261003", "20261001", "20261002", "20261001"])
    assert store.queued_outputs(2) == ["20261001", "20261002"]
    store.dequeue_outputs(["20261001"])
    assert store.queued_outputs(10) == ["20261002", "20261003"]
    assert store.summary()["queued_outputs"] == 2
    store.close()


def test_survives_reopen(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.add_samples([Sample(300, {"v1": 5.0})])
    store.set_meta("last_built", "300")
    store.close()
    store = make_store(tmp_path)
    assert store.samples_between(0, 1000)[0].values["v1"] == 5.0
    assert store.get_meta("last_built") == "300"
    store.close()


def test_adds_missing_columns(tmp_path: Path) -> None:
    # A log written by an older version that knew fewer fields.
    path = tmp_path / "log.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE samples (ts INTEGER PRIMARY KEY,"
        " uploaded INTEGER NOT NULL DEFAULT 0, v1 REAL, v2 REAL)"
    )
    conn.execute("INSERT INTO samples (ts, uploaded, v1) VALUES (300, 1, 42.0)")
    conn.commit()
    conn.close()

    store = SampleStore(str(path))
    store.open()
    row = store.samples_between(0, 1000)[0]
    assert row.values["v1"] == 42.0
    assert row.values["b6"] is None
    assert row.uploaded == DONE
    store.close()


def test_rejected_outputs_stay_until_queued_again(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.queue_outputs(["20261001", "20261002"])
    store.reject_output("20261001", "Bad request 400: something")
    assert store.queued_outputs(10) == ["20261002"]
    assert store.rejected_outputs() == [("20261001", "Bad request 400: something")]
    summary = store.summary()
    assert (summary["queued_outputs"], summary["failed_outputs"]) == (1, 1)

    # Queuing it again (a reupload) retries it.
    store.queue_outputs(["20261001"])
    assert store.queued_outputs(10) == ["20261001", "20261002"]
    assert store.rejected_outputs() == []
    store.close()


def test_unconfirmed_since_is_kept_until_the_slot_is_settled(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.unconfirmed_since(600, 1000.0) == 1000.0
    assert store.unconfirmed_since(600, 5000.0) == 1000.0  # the first attempt counts
    store.close()

    store = make_store(tmp_path)  # survives a restart
    assert store.unconfirmed_since(600, 9000.0) == 1000.0
    store.mark_range(301, 601, DONE)  # sent, given up on, or reuploaded
    assert store.unconfirmed_since(600, 9000.0) == 9000.0

    store.unconfirmed_since(900, 9000.0)
    store.expire_before(1000)
    assert store.unconfirmed_since(900, 9500.0) == 9500.0
    store.close()

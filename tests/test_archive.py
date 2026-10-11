"""PVOutput history: hourly roll-up, daily and backfill downloads, statistics."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import time
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.pvoutput_uploader.api import (
    HistoryStatus,
    PVOutputRateLimited,
)
from custom_components.pvoutput_uploader.archive import day_window, rollup, statistic_id

from pvo_helpers import local_dt, local_ts
from test_uploader import field, make


@pytest.fixture
def now(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    freezer.move_to(local_dt(10, 10, 10, 20))
    return freezer


def day_of(hours: list[tuple[str, dict[str, float]]]) -> list[HistoryStatus]:
    return [HistoryStatus(t, values) for t, values in hours]


DAY = day_of(
    [
        # Closes the previous day: not counted.
        ("00:00", {"v1": 0.0, "v2": 999.0}),
        ("09:50", {"v1": 100.0, "v2": 500.0, "v5": 17.0}),
        # 10:00 ends the 09:00-10:00 hour.
        ("10:00", {"v1": 150.0, "v2": 700.0, "normalised": 0.1}),
        ("10:10", {"v1": 200.0, "v2": 300.0}),
    ]
)


async def test_rollup(hass: HomeAssistant) -> None:
    rows = {(r[0], r[1]): r[2:] for r in rollup(date(2026, 10, 10), DAY, {"v1", "v3"})}
    nine, ten = local_ts(10, 10, 9, 0), local_ts(10, 10, 10, 0)
    # Energy since midnight becomes the change over each hour.
    assert rows[(nine, "v1")] == (None, None, None, 150.0)
    assert rows[(ten, "v1")] == (None, None, None, 50.0)
    # Other values: mean, min and max of the hour's statuses.
    assert rows[(nine, "v2")] == (600.0, 500.0, 700.0, None)
    assert rows[(ten, "v2")] == (300.0, 300.0, 300.0, None)
    assert rows[(nine, "v5")] == (17.0, 17.0, 17.0, None)
    assert rows[(nine, "normalised")] == (0.1, 0.1, 0.1, None)
    # The 00:00 status is left out.
    assert (local_ts(10, 9, 23, 0), "v2") not in rows
    assert len(rows) == 6


async def hourly(hass: HomeAssistant, stat: str, start: int) -> list[dict[str, Any]]:
    await async_wait_recording_done(hass)
    found = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(start),
        None,
        {stat},
        "hour",
        None,
        {"sum", "mean", "min", "max"},
    )
    return found.get(stat, [])


async def test_daily_download(
    recorder_mock, hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    up.client.history["20261009"] = list(DAY)
    await up.archive.async_cycle()
    # First run: yesterday only.
    assert up.client.history_calls == ["20261009"]
    assert up.archive.summary == {"first": "2026-10-09", "days": 1, "empty_days": 0}

    # Once a day: nothing more today.
    await up.archive.async_cycle()
    assert up.client.history_calls == ["20261009"]

    # Every column is kept as statistics, whatever the fields are set to.
    start = day_window(date(2026, 10, 9))[0]
    v1 = await hourly(hass, statistic_id("1", "v1"), start)
    assert [round(r["sum"], 1) for r in v1] == [150.0, 200.0]
    v2 = await hourly(hass, statistic_id("1", "v2"), start)
    assert [(r["mean"], r["min"], r["max"]) for r in v2] == [
        (600.0, 500.0, 700.0),
        (300.0, 300.0, 300.0),
    ]
    assert await hourly(hass, statistic_id("1", "v5"), start)

    # The next day after 02:00: the day after.
    now.tick(24 * 3600)
    await up.archive.async_cycle()
    assert up.client.history_calls == ["20261009", "20261010"]


async def test_daily_download_switched_off(
    recorder_mock, hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    up.archive.enabled = False
    await up.archive.async_cycle()
    assert up.client.history_calls == []


async def test_history_download(
    recorder_mock, hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    up.archive.enabled = False
    for day in ("20261002", "20261003"):
        up.client.history[day] = list(DAY)
    await up.archive.async_queue(date(2026, 10, 1), date(2026, 10, 3))
    assert up.archive.days_left == 3

    await up.archive.async_cycle()
    assert up.client.history_calls == ["20261001", "20261002", "20261003"]
    assert up.archive.backfill is None
    assert up.archive.days_left == 0
    assert up.archive.result["detail"] == 2
    assert up.archive.result["empty"] == 1
    assert up.archive.summary["first"] == "2026-10-02"

    # Energy sums run on across the days.
    start = day_window(date(2026, 10, 2))[0]
    v1 = await hourly(hass, statistic_id("1", "v1"), start)
    assert [round(r["sum"], 1) for r in v1] == [150.0, 200.0, 350.0, 400.0]


async def test_history_download_resumes_after_the_limit(
    recorder_mock, hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    up.archive.enabled = False
    await up.archive.async_queue(date(2026, 10, 1), date(2026, 10, 3))
    calls = []

    def fail(kind: str, day: Any) -> Exception | None:
        calls.append(day)
        if day == "20261002" and len(calls) == 2:
            return PVOutputRateLimited("exceeded", time.time() + 600)
        return None

    up.client.fail = fail
    await up.archive.async_cycle()
    assert up.archive.backfill is not None
    assert up.archive.backfill["next"] == "2026-10-02"
    assert up.paused_until is not None

    # Kept across a restart, and carried on after the pause.
    again = await make(hass, tmp_path, [field("v2")])
    assert again.archive.backfill == up.archive.backfill
    now.tick(601)
    await up.archive.async_cycle()
    assert up.archive.backfill is None


async def test_first_detail(
    recorder_mock, hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    for n in range(5, 10):
        up.client.history[f"202610{n:02d}"] = list(DAY)
    assert await up.archive.async_first_detail(date(2026, 10, 1), date(2026, 10, 9)) == date(
        2026, 10, 5
    )
    # A few requests, not one per day.
    assert len(up.client.history_calls) <= 5
    # Nothing at all in range.
    assert await up.archive.async_first_detail(date(2026, 9, 1), date(2026, 9, 30)) is None
    # Probing stores nothing.
    assert up.archive.summary.get("days", 0) == 0


async def test_detail_replaces_and_empty_days_keep(
    recorder_mock, hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    up.archive.enabled = False
    up.client.history["20261002"] = list(DAY)
    await up.archive.async_queue(date(2026, 10, 2), date(2026, 10, 2))
    await up.archive.async_cycle()

    # PVOutput's data changed: the day is replaced.
    up.client.history["20261002"] = day_of([("10:00", {"v2": 100.0})])
    await up.archive.async_queue(date(2026, 10, 2), date(2026, 10, 2))
    await up.archive.async_cycle()
    rows = await up._run(up.store.archive_rows, "v2")
    assert [r[1] for r in rows] == [100.0]

    # PVOutput no longer has the day: what was downloaded stays.
    up.client.history.clear()
    await up.archive.async_queue(date(2026, 10, 2), date(2026, 10, 2))
    await up.archive.async_cycle()
    assert await up._run(up.store.archive_rows, "v2") == rows

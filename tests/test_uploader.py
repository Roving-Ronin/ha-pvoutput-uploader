"""Building statuses from the log and sending them."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pvoutput_uploader import uploader as uploader_module
from custom_components.pvoutput_uploader.api import (
    PVOutputBadRequest,
    PVOutputRateLimited,
)
from custom_components.pvoutput_uploader.const import (
    AGG_AVERAGE,
    AGG_ENERGY_TODAY,
    AGG_LAST,
    CONF_DETECTED_DONOR,
    CONF_DETECTED_INTERVAL,
    CONF_FIELDS,
    CONF_OUTPUT_FALLBACK,
    CONF_SYSTEM_ID,
    CONF_UPLOAD_INTERVAL,
    DOMAIN,
)
from custom_components.pvoutput_uploader.store import (
    DONE,
    EXPIRED,
    FAILED,
    PARTIAL,
    PENDING,
    Sample,
    SampleStore,
)
from custom_components.pvoutput_uploader.uploader import (
    META_LAST_BUILT,
    Uploader,
    _slot_end,
)

from pvo_helpers import local_dt, local_ts


class FakeClient:
    """Records what would be sent; ``fail`` may return an exception to raise."""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []
        self.singles: list[dict[str, str]] = []
        self.outputs: list[dict[str, str]] = []
        self.batch_outputs: list[list[str]] = []
        self.fail: Callable[[str, Any], Exception | None] | None = None
        self.rate_limit: int | None = None
        self.rate_remaining: int | None = None
        self.rate_reset: float | None = None
        # Times (hh:mm) addbatchstatus.jsp answers with status 0 for.
        self.not_added: set[str] = set()

    def _check(self, kind: str, payload: Any) -> None:
        if self.fail and (err := self.fail(kind, payload)):
            raise err

    async def add_batch_status(self, lines: list[str]) -> str:
        self._check("batch", lines)
        self.batches.append(lines)
        # Like PVOutput: date,time,status per status; 1 added, 0 nothing added.
        return ";".join(
            f"{d},{t},{0 if t in self.not_added else 1}"
            for d, t, *_ in (line.split(",") for line in lines)
        )

    async def add_status(self, params: dict[str, str]) -> str:
        self._check("single", params)
        self.singles.append(params)
        return "OK 200: Added Status"

    async def add_output(self, params: dict[str, str]) -> str:
        self._check("output", params)
        self.outputs.append(params)
        return ""

    async def add_batch_output(self, lines: list[str]) -> str:
        self._check("batch_output", lines)
        self.batch_outputs.append(lines)
        return ""


def field(key: str, entity: str | None = "sensor.x", **extra: Any) -> dict[str, Any]:
    return {"field": key, "entity_id": entity, "active": True} | extra


async def make(
    hass: HomeAssistant,
    tmp_path: Path,
    fields: list[dict[str, Any]],
    donor: bool = False,
    interval: int = 5,
    **options: Any,
) -> Uploader:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "api_key": "key",
            CONF_SYSTEM_ID: "1",
            CONF_DETECTED_INTERVAL: interval,
            CONF_DETECTED_DONOR: donor,
        },
        options={CONF_FIELDS: fields} | options,
    )
    await hass.async_add_executor_job(partial(tmp_path.mkdir, parents=True, exist_ok=True))
    store = SampleStore(str(tmp_path / "log.db"))
    await hass.async_add_executor_job(store.open)
    up = Uploader(hass, entry, FakeClient(), store)  # type: ignore[arg-type]
    await up.async_load()
    return up


async def add(up: Uploader, *samples: Sample, last_built: int | None = None) -> None:
    await up._run(up.store.add_samples, list(samples))
    if last_built is not None:
        await up._run(up.store.set_meta, META_LAST_BUILT, str(last_built))


async def states(up: Uploader, start: int, end: int) -> dict[int, int]:
    rows = await up._run(up.store.samples_between, start, end)
    return {r.ts: r.uploaded for r in rows}


@pytest.fixture
def now(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    freezer.move_to(local_dt(10, 10, 10, 20))
    return freezer


async def test_slot_end(hass: HomeAssistant) -> None:
    assert _slot_end(local_ts(10, 10, 10, 5), 900) == local_ts(10, 10, 10, 15)
    assert _slot_end(local_ts(10, 10, 10, 15), 900) == local_ts(10, 10, 10, 15)
    assert _slot_end(local_ts(10, 10, 23, 55), 900) == local_ts(10, 11, 0, 0)
    assert _slot_end(local_ts(10, 11, 0, 0), 300) == local_ts(10, 11, 0, 0)


async def test_day_bounds_cover_dst_change(hass: HomeAssistant, tmp_path: Path) -> None:
    await hass.config.async_set_time_zone("US/Pacific")
    up = await make(hass, tmp_path, [])
    start, end = up._day_bounds(date(2026, 11, 1))  # clocks go back: 25 hours
    assert end - start == 25 * 3600
    assert start == local_ts(11, 1) - 300 + 1


async def test_batch_with_energy_since_midnight(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(
        hass,
        tmp_path,
        [
            field("v1", aggregate=AGG_ENERGY_TODAY),
            field("v2", aggregate=AGG_AVERAGE),
            field("v5", aggregate=AGG_AVERAGE),
        ],
    )
    await add(
        up,
        # Already sent, and yesterday: must not count towards today's energy.
        Sample(local_ts(10, 9, 23, 55), {"v1": 1000.0}, DONE),
        # The slot ending at 00:00 belongs to the new day.
        Sample(local_ts(10, 10, 0, 0), {"v1": 5.0, "v2": 100.0, "v5": 20.04}),
        Sample(local_ts(10, 10, 10, 5), {"v1": 10.0, "v2": 200.0}),
        Sample(local_ts(10, 10, 10, 10), {"v1": 20.0, "v5": 21.0}),
        last_built=local_ts(10, 10, 10, 10),
    )
    await up._async_upload_statuses()
    assert up.client.batches == [
        [
            "20261010,00:00,5,100,,,20",
            "20261010,10:05,15,200",
            "20261010,10:10,35,,,,21",
        ]
    ]
    assert set((await states(up, 0, 2**40)).values()) == {DONE}
    assert up.last_upload is not None

    # Nothing left: no further request.
    await up._async_upload_statuses()
    assert len(up.client.batches) == 1


async def test_fifteen_minute_interval(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(
        hass,
        tmp_path,
        [field("v1", aggregate=AGG_ENERGY_TODAY), field("v2"), field("v4", aggregate=AGG_LAST)],
        **{CONF_UPLOAD_INTERVAL: "15"},
    )
    await add(
        up,
        Sample(local_ts(10, 10, 10, 5), {"v1": 1.0, "v2": 100.0, "v4": 1.0}),
        Sample(local_ts(10, 10, 10, 10), {"v1": 2.0, "v2": 200.0, "v4": 2.0}),
        Sample(local_ts(10, 10, 10, 15), {"v1": 3.0, "v2": 300.0, "v4": None}),
        # The 10:30 slot is not complete yet.
        Sample(local_ts(10, 10, 10, 20), {"v1": 4.0, "v2": 400.0}),
        last_built=local_ts(10, 10, 10, 20),
    )
    await up._async_upload_statuses()
    # v4 is unavailable at the end of the slot: no last value is sent.
    assert up.client.batches == [["20261010,10:15,6,200"]]
    st = await states(up, 0, 2**40)
    assert st[local_ts(10, 10, 10, 15)] == DONE
    assert st[local_ts(10, 10, 10, 20)] == PENDING


async def test_passive_and_donor_fields_are_not_sent(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    fields = [
        field("v2"),
        field("v5", active=False),
        field("v12"),  # extended: donor only
    ]
    row = Sample(local_ts(10, 10, 10, 5), {"v2": 100.0, "v5": 20.0, "v12": 45.0})

    up = await make(hass, tmp_path / "a", fields, donor=False)
    await add(up, row, last_built=row.ts)
    await up._async_upload_statuses()
    assert up.client.batches == [["20261010,10:05,,100"]]

    up = await make(hass, tmp_path / "b", fields, donor=True)
    await add(up, row, last_built=row.ts)
    await up._async_upload_statuses()
    assert up.client.batches == [
        [",".join(["20261010", "10:05", "", "100", *[""] * 9, "45"])]
    ]


async def test_battery_fields_go_one_by_one(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(
        hass,
        tmp_path,
        [
            field("v5"),
            field("b1"),
            field("b2", aggregate=AGG_LAST),
            field("b3", entity=None, constant=5544),
        ],
        donor=True,
    )
    await add(
        up,
        Sample(local_ts(10, 10, 10, 5), {"v5": 20.0, "b1": -250.4, "b2": 81.25}),
        # Only the constant would be sent: no status.
        Sample(local_ts(10, 10, 10, 10), {}),
        Sample(local_ts(10, 10, 10, 15), {"b1": 100.0}),
        last_built=local_ts(10, 10, 10, 15),
    )
    await up._async_upload_statuses()
    assert up.client.batches == []
    assert up.client.singles == [
        {"d": "20261010", "t": "10:05", "v5": "20", "b1": "-250", "b2": "81.2", "b3": "5544"},
        {"d": "20261010", "t": "10:15", "b1": "100", "b3": "5544"},
    ]
    assert set((await states(up, 0, 2**40)).values()) == {DONE}


async def test_rate_limit_pauses_and_resumes(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    ts = local_ts(10, 10, 10, 5)
    await add(up, Sample(ts, {"v2": 100.0}), last_built=ts)
    reset = local_ts(10, 10, 11, 0)
    up.client.fail = lambda kind, payload: PVOutputRateLimited("Exceeded", reset)

    await up._async_upload_statuses()
    assert up.paused_until == reset
    assert (await states(up, 0, 2**40))[ts] == PENDING
    assert await up._run(up.store.get_meta, "paused_until") == str(reset)

    # The pause survives a restart.
    again = Uploader(hass, up.entry, up.client, up.store)
    await again.async_load()
    assert again.paused_until == reset

    up.client.fail = None
    await up._async_upload_statuses()
    assert up.client.batches == []

    now.move_to(local_dt(10, 10, 11, 1))
    await up._async_upload_statuses()
    assert up.client.batches == [["20261010,10:05,,100"]]
    assert up.paused_until is None


async def test_rate_limit_reserve(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    ts = local_ts(10, 10, 10, 5)
    await add(up, Sample(ts, {"v2": 100.0}), last_built=ts)
    up.client.rate_remaining = 2
    up.client.rate_reset = local_ts(10, 10, 11, 0)
    await up._async_upload_statuses()
    assert up.client.batches == []

    now.move_to(local_dt(10, 10, 11, 1))
    await up._async_upload_statuses()
    assert len(up.client.batches) == 1


async def test_rejected_status_is_isolated(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    times = [local_ts(10, 10, 10, m) for m in (5, 10, 15, 20)]
    await add(up, *(Sample(t, {"v2": 100.0}) for t in times), last_built=times[-1])

    def fail(kind: str, lines: list[str]) -> Exception | None:
        if any(",10:10," in line for line in lines):
            return PVOutputBadRequest("Bad request 400: Moon Powered")
        return None

    up.client.fail = fail
    await up._async_upload_statuses()
    st = await states(up, 0, 2**40)
    assert st[times[1]] == FAILED
    assert [st[t] for t in (times[0], times[2], times[3])] == [DONE, DONE, DONE]


async def test_missing_base_status_is_retried(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v5")])
    ts = local_ts(10, 10, 10, 15)
    await add(up, Sample(ts, {"v5": 20.0}), last_built=ts)
    up.client.fail = lambda kind, payload: PVOutputBadRequest(
        "Bad request 400: Missing energy and power values"
    )
    await up._async_upload_statuses()
    assert (await states(up, 0, 2**40))[ts] == PENDING

    now.move_to(local_dt(10, 10, 13, 0))
    await up._async_upload_statuses()
    assert (await states(up, 0, 2**40))[ts] == FAILED


async def test_old_statuses_expire_into_outputs(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")], **{CONF_OUTPUT_FALLBACK: True})
    old = local_ts(9, 20, 12, 0)  # beyond the 14 day window
    await add(up, Sample(old, {"v2": 100.0}), last_built=local_ts(10, 10, 10, 15))
    await up._async_upload_statuses()
    assert (await states(up, 0, 2**40))[old] == EXPIRED
    assert await up._run(up.store.queued_outputs, 10) == ["20260920"]
    assert up.client.batches == []


async def test_old_statuses_expire_without_outputs(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    old = local_ts(9, 20, 12, 0)
    await add(up, Sample(old, {"v2": 100.0}), last_built=local_ts(10, 10, 10, 15))
    await up._async_upload_statuses()
    assert (await states(up, 0, 2**40))[old] == EXPIRED
    assert await up._run(up.store.queued_outputs, 10) == []


async def test_donor_window_is_longer(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")], donor=True)
    old = local_ts(9, 20, 12, 0)
    await add(up, Sample(old, {"v2": 100.0}), last_built=local_ts(10, 10, 10, 15))
    await up._async_upload_statuses()
    assert up.client.batches == [["20260920,12:00,,100"]]


async def test_daily_output(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(
        hass,
        tmp_path,
        [
            field("v1", aggregate=AGG_ENERGY_TODAY),
            field("v2"),
            field("v3", aggregate=AGG_ENERGY_TODAY),
            field("v5"),
        ],
    )
    await add(
        up,
        Sample(local_ts(10, 1, 12, 0), {"v1": 100.0, "v2": 1000.0, "v3": 50.0, "v5": 15.0}, DONE),
        Sample(local_ts(10, 1, 12, 5), {"v1": 200.0, "v2": 3000.0, "v3": 60.0, "v5": 25.0}, DONE),
    )
    await up._run(up.store.queue_outputs, ["20261001", "20260915"])
    await up._async_upload_outputs()
    # 15 September has no data and is dropped.
    assert up.client.outputs == [
        {"d": "20261001", "g": "300", "c": "110", "pp": "3000", "pt": "12:05", "tm": "15", "tx": "25"}
    ]
    assert await up._run(up.store.queued_outputs, 10) == []


async def test_daily_outputs_batched_for_donors(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v1", aggregate=AGG_ENERGY_TODAY)], donor=True)
    await add(
        up,
        Sample(local_ts(10, 1, 12, 0), {"v1": 100.0}, DONE),
        Sample(local_ts(10, 2, 12, 0), {"v1": 200.0}, DONE),
    )
    await up._run(up.store.queue_outputs, ["20261001", "20261002"])
    await up._async_upload_outputs()
    assert up.client.batch_outputs == [["20261001,100", "20261002,200"]]


async def test_reupload(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    recent = local_ts(10, 9, 10, 0)
    old = local_ts(9, 1, 10, 0)
    await add(up, Sample(recent, {"v2": 1.0}, DONE), Sample(old, {"v2": 1.0}, DONE))

    result = await up.async_reupload(local_dt(9, 1), date(2026, 10, 10), "auto")
    # Window: 27 September to 10 October. Before it: 1-26 September as outputs.
    assert result == {"status_days": 14, "output_days": 26}
    st = await states(up, 0, 2**40)
    assert st[recent] == PENDING
    assert st[old] == DONE
    queued = await up._run(up.store.queued_outputs, 100)
    assert queued[0] == "20260901"
    assert queued[-1] == "20260926"


async def test_reupload_outputs_only_skips_today(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    result = await up.async_reupload(local_dt(10, 8), date(2026, 10, 12), "outputs")
    assert result["status_days"] == 0
    assert await up._run(up.store.queued_outputs, 100) == ["20261008", "20261009"]


async def test_import_from_recorder(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v1", aggregate=AGG_ENERGY_TODAY)])
    await add(up, last_built=local_ts(10, 10, 10, 15))
    state_row = local_ts(10, 10, 10, 5)

    async def from_states(hass_, sources, start, end):
        return [Sample(state_row, {"v1": 7.0})] if start < state_row <= end else []

    async def from_statistics(hass_, sources, start, end):
        return [
            (local_ts(10, 10, 9, 0), {"v1": 120.0}),
            # After the end of the import: skipped.
            (local_ts(10, 10, 11, 0), {"v1": 999.0}),
        ]

    with (
        patch.object(uploader_module, "async_build_from_states", side_effect=from_states),
        patch.object(
            uploader_module, "async_build_from_statistics", side_effect=from_statistics
        ),
    ):
        result = await up.async_import(
            local_dt(10, 10, 8, 0), local_dt(10, 10, 12, 0), overwrite=False, upload=False
        )

    assert result == {"from_states": 1, "from_statistics": 12}
    rows = await up._run(up.store.samples_between, 0, 2**40)
    assert [r.ts for r in rows] == [
        *range(local_ts(10, 10, 8, 5), local_ts(10, 10, 9, 0) + 1, 300),
        state_row,
    ]
    assert rows[0].values["v1"] == pytest.approx(10.0)
    assert {r.uploaded for r in rows} == {DONE}


async def test_reupload_from_a_time(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    times = [local_ts(10, 9, 10, m) for m in (0, 5, 10)]
    await add(up, *(Sample(t, {"v2": 1.0}, DONE) for t in times))
    result = await up.async_reupload(local_dt(10, 9, 10, 5), date(2026, 10, 10), "statuses")
    assert result == {"status_days": 2, "output_days": 0}
    st = await states(up, 0, 2**40)
    assert [st[t] for t in times] == [DONE, PENDING, PENDING]


async def test_first_unsent(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    assert await up.async_first_unsent() is None
    await add(
        up,
        Sample(local_ts(10, 9, 10, 0), {"v2": 1.0}, DONE),
        Sample(local_ts(10, 9, 10, 5), {"v2": 1.0}, FAILED),
        Sample(local_ts(10, 9, 10, 10), {"v2": 1.0}, PENDING),
    )
    assert await up.async_first_unsent() == local_ts(10, 9, 10, 5)


async def test_only_confirmed_statuses_count_as_sent(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    times = [local_ts(10, 10, 10, m) for m in (5, 10, 15)]
    await add(up, *(Sample(t, {"v2": 100.0}) for t in times), last_built=times[-1])
    up.client.not_added = {"10:10"}

    await up._async_upload_statuses()
    st = await states(up, 0, 2**40)
    assert [st[t] for t in times] == [DONE, PENDING, DONE]
    # Not resent at once in the same cycle.
    assert len(up.client.batches) == 1

    # Retried in later cycles, then given up on.
    await up._async_upload_statuses()
    assert up.client.batches[-1] == ["20261010,10:10,,100"]
    assert (await states(up, 0, 2**40))[times[1]] == PENDING
    now.move_to(local_dt(10, 10, 12, 30))
    await up._async_upload_statuses()
    assert (await states(up, 0, 2**40))[times[1]] == FAILED


async def test_unavailable_values_are_not_sent_as_current(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(
        hass,
        tmp_path,
        [field("v1", aggregate=AGG_ENERGY_TODAY), field("v2"), field("v4", aggregate=AGG_LAST)],
        **{CONF_UPLOAD_INTERVAL: "15"},
    )
    gap: dict[str, float | None] = {"v1": None, "v2": None, "v4": None}
    await add(
        up,
        Sample(local_ts(10, 10, 10, 5), {"v1": 1.0, "v2": 100.0, "v4": 100.0}),
        Sample(local_ts(10, 10, 10, 10), gap),
        Sample(local_ts(10, 10, 10, 15), gap),
        # Only power reports in the next slot; the energy counter is unavailable.
        Sample(local_ts(10, 10, 10, 20), {"v1": None, "v2": 200.0, "v4": None}),
        Sample(local_ts(10, 10, 10, 25), gap),
        Sample(local_ts(10, 10, 10, 30), gap),
        last_built=local_ts(10, 10, 10, 30),
    )
    await up._async_upload_statuses()
    assert up.client.batches == [
        [
            # v4 read 100 but was unavailable at 10:15: not sent as current.
            "20261010,10:15,1,100",
            # No counter reading in this slot: today's total is not repeated.
            "20261010,10:30,,200",
        ]
    ]


async def test_old_statuses_get_the_full_retry_window(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    old = local_ts(10, 9, 10, 5)  # yesterday, e.g. imported or reuploaded
    await add(up, Sample(old, {"v2": 100.0}), last_built=local_ts(10, 10, 10, 15))
    up.client.not_added = {"10:05"}

    await up._async_upload_statuses()
    assert (await states(up, 0, 2**40))[old] == PENDING

    now.move_to(local_dt(10, 10, 11, 20))
    await up._async_upload_statuses()
    assert (await states(up, 0, 2**40))[old] == PENDING

    now.move_to(local_dt(10, 10, 12, 30))
    await up._async_upload_statuses()
    assert (await states(up, 0, 2**40))[old] == FAILED


async def test_retry_window_survives_restart(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    ts = local_ts(10, 10, 10, 5)
    await add(up, Sample(ts, {"v2": 100.0}), last_built=local_ts(10, 10, 10, 15))
    up.client.not_added = {"10:05"}
    await up._async_upload_statuses()  # first unconfirmed attempt at 10:20

    # Home Assistant restarts (or the options are saved) at 11:30.
    now.move_to(local_dt(10, 10, 11, 30))
    again = Uploader(hass, up.entry, up.client, up.store)
    await again.async_load()
    await again._async_upload_statuses()
    assert (await states(again, 0, 2**40))[ts] == PENDING

    # Two hours after the first attempt, not after the restart.
    now.move_to(local_dt(10, 10, 12, 25))
    await again._async_upload_statuses()
    assert (await states(again, 0, 2**40))[ts] == FAILED


async def test_rejected_daily_output_is_kept(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v1", aggregate=AGG_ENERGY_TODAY)])
    await add(up, Sample(local_ts(10, 1, 12, 0), {"v1": 100.0}, DONE))
    await up._run(up.store.queue_outputs, ["20261001"])
    up.client.fail = lambda kind, payload: PVOutputBadRequest("Bad request 400: Invalid")

    await up._async_upload_outputs()
    assert await up._run(up.store.queued_outputs, 10) == []
    assert await up._run(up.store.rejected_outputs) == [("20261001", "Bad request 400: Invalid")]
    assert up.last_upload is None
    assert "20261001" in (up.last_error or "")

    # A reupload queues it again, and it is sent once PVOutput takes it.
    up.client.fail = None
    await up.async_reupload(local_dt(10, 1), date(2026, 10, 1), "outputs")
    await up._async_upload_outputs()
    assert [p["d"] for p in up.client.outputs] == ["20261001"]
    assert await up._run(up.store.rejected_outputs) == []
    assert up.last_upload is not None


async def test_one_bad_day_in_a_batch_of_outputs(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v1", aggregate=AGG_ENERGY_TODAY)], donor=True)
    await add(
        up,
        Sample(local_ts(10, 1, 12, 0), {"v1": 100.0}, DONE),
        Sample(local_ts(10, 2, 12, 0), {"v1": 200.0}, DONE),
    )
    await up._run(up.store.queue_outputs, ["20261001", "20261002"])

    def fail(kind: str, payload: Any) -> Exception | None:
        text = ";".join(payload) if isinstance(payload, list) else payload["d"]
        return PVOutputBadRequest("Bad request 400: Invalid") if "20261002" in text else None

    up.client.fail = fail
    await up._async_upload_outputs()
    assert [p["d"] for p in up.client.outputs] == ["20261001"]
    assert await up._run(up.store.queued_outputs, 10) == []
    assert [d for d, _ in await up._run(up.store.rejected_outputs)] == ["20261002"]


def _counting(fail: Callable[[list[str]], Exception | None]) -> tuple[list[int], Any]:
    attempts = [0]

    def check(kind: str, lines: list[str]) -> Exception | None:
        attempts[0] += 1
        return fail(lines)

    return attempts, check


async def test_rejected_value_is_left_out_once(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2"), field("v5")])
    ts = local_ts(10, 10, 10, 5)
    await add(up, Sample(ts, {"v2": 99999.0, "v5": 20.0}), last_built=ts)
    attempts, up.client.fail = _counting(
        lambda lines: PVOutputBadRequest(
            "Bad request 400: Power value 99999 too high for system size 13320"
        )
        if any(",99999" in line for line in lines)
        else None
    )
    await up._async_upload_statuses()
    # The temperature still reaches PVOutput, without the rejected power.
    assert up.client.batches == [["20261010,10:05,,,,,20"]]
    assert attempts[0] == 2
    assert (await states(up, 0, 2**40))[ts] == PARTIAL
    # Partly sent counts as unsent, for a reupload once the value is fixed.
    assert await up.async_first_unsent() == ts


async def test_unknown_rejection_is_not_retried(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2"), field("v5")])
    ts = local_ts(10, 10, 10, 5)
    await add(up, Sample(ts, {"v2": 500.0, "v5": 20.0}), last_built=ts)
    attempts, up.client.fail = _counting(
        lambda lines: PVOutputBadRequest("Bad request 400: Something else")
    )
    await up._async_upload_statuses()
    assert attempts[0] == 1
    assert (await states(up, 0, 2**40))[ts] == FAILED


async def test_rejected_again_without_the_value(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v1", aggregate=AGG_ENERGY_TODAY), field("v5")])
    ts = local_ts(10, 10, 2, 0)
    await add(up, Sample(ts, {"v1": 50.0, "v5": 20.0}), last_built=ts)
    attempts, up.client.fail = _counting(
        lambda lines: PVOutputBadRequest("Bad request 400: Moon Powered")
    )
    await up._async_upload_statuses()
    assert attempts[0] == 2  # the full status, then once without v1
    assert up.client.batches == []
    assert (await states(up, 0, 2**40))[ts] == FAILED


async def test_no_retry_when_only_fixed_values_would_be_left(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2"), field("v6", entity=None, constant=230)])
    ts = local_ts(10, 10, 10, 5)
    await add(up, Sample(ts, {"v2": 99999.0}), last_built=ts)
    attempts, up.client.fail = _counting(
        lambda lines: PVOutputBadRequest("Bad request 400: Power value 99999 too high")
    )
    await up._async_upload_statuses()
    assert attempts[0] == 1
    assert (await states(up, 0, 2**40))[ts] == FAILED

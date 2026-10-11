"""Setting up the integration, its entities and actions."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pvoutput_uploader.api import (
    PVOutputAuthError,
    PVOutputConnectionError,
    StatusReading,
    SystemInfo,
)
from custom_components.pvoutput_uploader.config_flow import DEFAULT_OPTIONS
from custom_components.pvoutput_uploader.const import (
    CONF_DETECTED_DONOR,
    CONF_DETECTED_INTERVAL,
    CONF_SYSTEM_ID,
    CONF_SYSTEM_NAME,
    DOMAIN,
)

GET_SYSTEM = "custom_components.pvoutput_uploader.api.PVOutputClient.get_system"
GET_HISTORY = "custom_components.pvoutput_uploader.api.PVOutputClient.get_status_history"


@pytest.fixture(autouse=True)
def no_history():
    """PVOutput has no history unless a test says otherwise (no real requests)."""
    with patch(GET_HISTORY, AsyncMock(return_value=[])) as mock:
        yield mock


@pytest.fixture
def entry(hass: HomeAssistant, tmp_path: Path) -> MockConfigEntry:
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1234",
        title="My System",
        data={
            "api_key": "key",
            CONF_SYSTEM_ID: "1234",
            CONF_SYSTEM_NAME: "My System",
            CONF_DETECTED_INTERVAL: 5,
            CONF_DETECTED_DONOR: False,
        },
        options=dict(DEFAULT_OPTIONS),
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_setup_and_unload(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry, tmp_path: Path
) -> None:
    with patch(GET_SYSTEM, AsyncMock(return_value=SystemInfo("My System", 10, True))):
        await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    # What the account reports is kept for later starts.
    assert entry.data[CONF_DETECTED_INTERVAL] == 10
    assert entry.data[CONF_DETECTED_DONOR] is True
    assert (tmp_path / "pvoutput_uploader_1234.db").exists()

    assert hass.states.get("switch.my_system_uploading").state == "on"
    assert hass.states.get("sensor.my_system_account_type").state == "donor"
    assert hass.states.get("sensor.my_system_samples_waiting").state == "0"

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.my_system_uploading"}, blocking=True
    )
    assert entry.runtime_data.uploader.upload_enabled is False
    assert hass.states.get("switch.my_system_uploading").state == "off"

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_offline_uses_saved_settings(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    with patch(GET_SYSTEM, AsyncMock(side_effect=PVOutputConnectionError("down"))):
        await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_DETECTED_INTERVAL] == 5
    assert hass.states.get("sensor.my_system_account_type").state == "standard"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_refused_key(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    with patch(GET_SYSTEM, AsyncMock(side_effect=PVOutputAuthError("bad key"))):
        await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_actions(recorder_mock, hass: HomeAssistant, entry: MockConfigEntry) -> None:
    with patch(GET_SYSTEM, AsyncMock(return_value=SystemInfo("My System", 5, False))):
        await _setup(hass, entry)
    today = dt_util.now().date()

    result = await hass.services.async_call(
        DOMAIN,
        "reupload",
        {"start": (today - timedelta(days=3)).isoformat()},
        blocking=True,
        return_response=True,
    )
    assert result == {"status_days": 4, "output_days": 0}

    result = await hass.services.async_call(
        DOMAIN,
        "reupload",
        {"start": (today - timedelta(days=20)).isoformat(), "method": "outputs"},
        blocking=True,
        return_response=True,
    )
    assert result == {"status_days": 0, "output_days": 21}
    # Today is never queued: its output is not complete.
    assert len(await hass.async_add_executor_job(
        entry.runtime_data.uploader.store.queued_outputs, 100
    )) == 20

    result = await hass.services.async_call(
        DOMAIN,
        "import_from_recorder",
        {"start": (dt_util.now() - timedelta(hours=2)).isoformat()},
        blocking=True,
        return_response=True,
    )
    assert result == {"from_states": 0, "from_statistics": 0}

    # Nothing unsent yet.
    result = await hass.services.async_call(
        DOMAIN, "reupload", {"since_failure": True}, blocking=True, return_response=True
    )
    assert result == {"status_days": 0, "output_days": 0}

    # A date and time works as start too.
    result = await hass.services.async_call(
        DOMAIN,
        "reupload",
        {"start": f"{(today - timedelta(days=1)).isoformat()} 12:00:00"},
        blocking=True,
        return_response=True,
    )
    assert result == {"status_days": 2, "output_days": 0}

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "reupload", {}, blocking=True, return_response=True
        )

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "reupload",
            {"start": today.isoformat(), "end": (today - timedelta(days=1)).isoformat()},
            blocking=True,
            return_response=True,
        )

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "reupload",
            {"start": today.isoformat(), "config_entry_id": "nope"},
            blocking=True,
            return_response=True,
        )

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_history_from_the_menu(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    with patch(GET_SYSTEM, AsyncMock(return_value=SystemInfo("My System", 5, False))):
        await _setup(hass, entry)
    today = dt_util.now().date()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "reupload"}
    )
    assert result["step_id"] == "reupload"
    # No battery fields: no "leave out battery" box.
    assert "without_battery" not in [str(k) for k in result["data_schema"].schema]
    # No passive fields: no list of them.
    assert "passive_fields" not in [str(k) for k in result["data_schema"].schema]
    placeholders = result["description_placeholders"]
    assert placeholders["status_days"] == "14"
    assert placeholders["first_unsent"].startswith("none")

    async def submit(data: dict) -> dict:
        return await hass.config_entries.options.async_configure(
            result["flow_id"], {"method": "auto", "end": f"{today.isoformat()} 23:59:00"} | data
        )

    # Nothing recorded yet, so nothing unsent.
    result = await submit({"from": "since_failure"})
    assert result["errors"] == {"base": "nothing_unsent"}
    result = await submit({"from": "from_time"})
    assert result["errors"] == {"start": "start_required"}
    result = await submit(
        {"from": "from_time", "start": f"{today.isoformat()} 00:00:00",
         "end": f"{(today - timedelta(days=1)).isoformat()} 23:59:00"}
    )
    assert result["errors"] == {"base": "end_before_start"}
    result = await submit(
        {"from": "from_time", "start": f"{(today - timedelta(days=2)).isoformat()} 06:00:00"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reupload_queued"
    assert result["description_placeholders"] == {"status_days": "3", "output_days": "0"}

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "import_history"}
    )
    start = (dt_util.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"start": start, "overwrite": False, "upload": False}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "import_done"
    assert result["description_placeholders"] == {"from_states": "0", "from_statistics": "0"}

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_field_sensors(recorder_mock, hass: HomeAssistant, entry: MockConfigEntry) -> None:
    hass.states.async_set(
        "sensor.outside", "20", {"unit_of_measurement": "°C", "friendly_name": "Outside"}
    )
    fields = [
        {
            "field": "v5", "entity_id": "sensor.outside", "constant": None,
            "name": "Ambient", "active": True, "aggregate": "average",
        },
        {
            "field": "b3", "entity_id": None, "constant": 5544.0,
            "name": "Battery Size", "active": True, "aggregate": "last",
        },
    ]
    hass.config_entries.async_update_entry(
        entry, options=dict(entry.options) | {"fields": fields}
    )
    with patch(GET_SYSTEM, AsyncMock(return_value=SystemInfo("My System", 5, False))):
        await _setup(hass, entry)

    v5 = hass.states.get("sensor.my_system_v5_ambient")
    assert v5.state == "Outside °C"
    assert v5.attributes["pvoutput_unit"] == "°C"
    assert v5.attributes["pvoutput_field"] == "v5"
    assert v5.attributes["pvoutput_field_name"] == "Temperature"
    assert v5.attributes["source"] == "sensor"
    assert v5.attributes["source_entity"] == "sensor.outside"
    assert v5.attributes["source_name"] == "Outside"
    assert v5.attributes["value_per_interval"] == "average"
    assert v5.attributes["configured_to_send"] is True

    b3 = hass.states.get("sensor.my_system_b3_battery_size")
    assert b3.state == "5544 Wh"
    assert b3.attributes["pvoutput_unit"] == "Wh"
    assert b3.attributes["source"] == "fixed value"
    # A battery field on a standard account is kept but not sent.
    assert b3.attributes["configured_to_send"] is False
    assert b3.attributes["not_configured_to_send_because"] == "needs a PVOutput donor account"

    # Removing a field removes its sensor.
    with patch(GET_SYSTEM, AsyncMock(return_value=SystemInfo("My System", 5, False))):
        hass.config_entries.async_update_entry(
            entry, options=dict(entry.options) | {"fields": fields[:1]}
        )
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.my_system_b3_battery_size") is None
    assert hass.states.get("sensor.my_system_v5_ambient") is not None

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reupload_has_no_battery_box(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Battery values are batched, so leaving them out gains nothing."""
    b1 = {
        "field": "b1", "entity_id": "sensor.battery_power", "constant": None,
        "name": "Battery Power", "active": True, "aggregate": "average",
    }
    hass.config_entries.async_update_entry(
        entry, options=dict(entry.options) | {"fields": [b1]}
    )
    with patch(GET_SYSTEM, AsyncMock(return_value=SystemInfo("My System", 5, True))):
        await _setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "reupload"}
    )
    assert "without_battery" not in [str(k) for k in result["data_schema"].schema]

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reupload_offers_passive_fields(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    passive = {
        "field": "v5", "entity_id": "sensor.outside", "constant": None,
        "name": "Outside", "active": False, "aggregate": "average",
    }
    hass.config_entries.async_update_entry(
        entry, options=dict(entry.options) | {"fields": [passive]}
    )
    with patch(GET_SYSTEM, AsyncMock(return_value=SystemInfo("My System", 5, False))):
        await _setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "reupload"}
    )
    assert "passive_fields" in [str(k) for k in result["data_schema"].schema]

    # The import form offers them too.
    other = await hass.config_entries.options.async_init(entry.entry_id)
    other = await hass.config_entries.options.async_configure(
        other["flow_id"], {"next_step_id": "import_history"}
    )
    assert "passive_fields" in [str(k) for k in other["data_schema"].schema]

    uploader = entry.runtime_data.uploader
    today = dt_util.now().date().isoformat()
    with patch.object(
        uploader,
        "async_reupload",
        AsyncMock(return_value={"status_days": 1, "output_days": 0}),
    ) as reupload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "from": "from_time",
                "start": f"{today} 00:00:00",
                "end": f"{today} 23:59:00",
                "method": "statuses",
                "passive_fields": ["v5"],
            },
        )
    assert result["reason"] == "reupload_queued"
    assert reupload.call_args.kwargs["passive_fields"] == ["v5"]

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_clear_queue_button(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    with patch(GET_SYSTEM, AsyncMock(return_value=SystemInfo("My System", 5, False))):
        await _setup(hass, entry)
    assert hass.states.get("button.my_system_clear_queued_history") is not None
    uploader = entry.runtime_data.uploader
    with patch.object(
        uploader,
        "async_clear_queue",
        AsyncMock(return_value={"samples": 0, "daily_outputs": 0}),
    ) as clear:
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.my_system_clear_queued_history"},
            blocking=True,
        )
    clear.assert_awaited_once()
    assert await hass.config_entries.async_unload(entry.entry_id)


GET_STATUS = "custom_components.pvoutput_uploader.api.PVOutputClient.get_status"


def _modes(entry: MockConfigEntry) -> dict[str, str]:
    return {
        m["field"]: "download" if m.get("direction") == "download"
        else "send" if m["active"] else "record_only"
        for m in entry.options["fields"]
    }


async def test_field_mode_selects(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    fields = [
        {
            "field": "v5", "entity_id": "sensor.outside", "constant": None,
            "name": "Ambient", "active": True, "aggregate": "average",
        },
        {
            "field": "v6", "entity_id": "sensor.voltage", "constant": None,
            "name": "Grid", "active": False, "aggregate": "average",
        },
        {
            "field": "v2", "entity_id": None, "constant": None, "name": "Power",
            "active": True, "aggregate": "average", "direction": "download",
        },
    ]
    hass.config_entries.async_update_entry(
        entry, options=dict(entry.options) | {"fields": fields}
    )
    reading = StatusReading("20261011", "12:00", {"v2": 1500.0, "v5": None}, 0.25)
    with (
        patch(GET_SYSTEM, AsyncMock(return_value=SystemInfo("My System", 5, False))),
        patch(GET_STATUS, AsyncMock(return_value=reading)) as get_status,
    ):
        await _setup(hass, entry)
        v5 = hass.states.get("select.my_system_v5_ambient")
        assert v5.state == "send"
        assert v5.attributes["options"] == ["send", "record_only", "download"]
        assert hass.states.get("select.my_system_v6_grid").state == "record_only"
        # A field with no source in Home Assistant can only download.
        v2 = hass.states.get("select.my_system_v2_power")
        assert v2.state == "download"
        assert v2.attributes["options"] == ["download"]
        # The field sensors are not taken for the selects, or the reverse.
        assert hass.states.get("sensor.my_system_v5_ambient") is not None

        # Download sensors, read from PVOutput (v7-v12 only for donors).
        get_status.assert_called_with(extended=False)
        power = hass.states.get("sensor.my_system_v2_power_from_pvoutput")
        assert float(power.state) == 1500.0
        assert power.attributes["unit_of_measurement"] == "W"
        assert power.attributes["pvoutput_status"] == "20261011 12:00"
        assert float(hass.states.get("sensor.my_system_normalised_output_from_pvoutput").state) == 0.25

        await hass.services.async_call(
            "select", "select_option",
            {"entity_id": "select.my_system_v5_ambient", "option": "download"},
            blocking=True,
        )
        await hass.async_block_till_done(wait_background_tasks=True)
        assert _modes(entry) == {"v5": "download", "v6": "record_only", "v2": "download"}
        # The upload source stays, for switching back.
        assert entry.options["fields"][0]["entity_id"] == "sensor.outside"
        assert hass.states.get("select.my_system_v5_ambient").state == "download"
        # PVOutput had no temperature: unknown, not 0.
        assert hass.states.get("sensor.my_system_v5_ambient_from_pvoutput").state == "unknown"

        await hass.services.async_call(
            "select", "select_option",
            {"entity_id": "select.my_system_v5_ambient", "option": "send"},
            blocking=True,
        )
        await hass.async_block_till_done(wait_background_tasks=True)
        assert _modes(entry)["v5"] == "send"
        assert hass.states.get("sensor.my_system_v5_ambient_from_pvoutput") is None

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_field_mode_select_checks_the_saved_field(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    fields = [
        {
            "field": "v2", "entity_id": None, "constant": None, "name": "Power",
            "active": True, "aggregate": "average", "direction": "download",
        },
    ]
    hass.config_entries.async_update_entry(
        entry, options=dict(entry.options) | {"fields": fields}
    )
    reading = StatusReading("20261011", "12:00", {"v2": 1500.0}, 0.25)
    with (
        patch(GET_SYSTEM, AsyncMock(return_value=SystemInfo("My System", 5, False))),
        patch(GET_STATUS, AsyncMock(return_value=reading)),
    ):
        await _setup(hass, entry)
        select = hass.data["entity_components"]["select"].get_entity(
            "select.my_system_v2_power"
        )
        # Without a source, Send is refused even when called directly.
        with pytest.raises(ServiceValidationError):
            await select.async_select_option("send")
        assert _modes(entry) == {"v2": "download"}

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_extended_download_needs_donor(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    fields = [
        {
            "field": "v7", "entity_id": None, "constant": None, "name": "Grid",
            "active": True, "aggregate": "average", "direction": "download",
        },
    ]
    hass.config_entries.async_update_entry(
        entry, options=dict(entry.options) | {"fields": fields}
    )
    reading = StatusReading("20261011", "12:00", {"v2": 1500.0}, 0.25)
    with (
        patch(GET_SYSTEM, AsyncMock(return_value=SystemInfo("My System", 5, False))),
        patch(GET_STATUS, AsyncMock(return_value=reading)),
    ):
        await _setup(hass, entry)
        v7 = hass.states.get("sensor.my_system_v7_grid_from_pvoutput")
        assert v7.state == "unknown"
        assert v7.attributes["not_downloaded_because"] == "needs a PVOutput donor account"

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_last_upload_only_when_something_uploads(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    download = {
        "field": "v2", "entity_id": None, "constant": None, "name": "Power",
        "active": True, "aggregate": "average", "direction": "download",
    }
    upload = {
        "field": "v5", "entity_id": "sensor.outside", "constant": None,
        "name": "Ambient", "active": False, "aggregate": "average",
    }
    hass.config_entries.async_update_entry(
        entry, options=dict(entry.options) | {"fields": [download]}
    )
    reading = StatusReading("20261011", "12:00", {"v2": 1500.0}, 0.25)
    with (
        patch(GET_SYSTEM, AsyncMock(return_value=SystemInfo("My System", 5, False))),
        patch(GET_STATUS, AsyncMock(return_value=reading)),
    ):
        await _setup(hass, entry)
        # Download only: no Last upload, but Last download.
        assert hass.states.get("sensor.my_system_last_upload") is None
        assert hass.states.get("sensor.my_system_last_download") is not None

        # A field that can be uploaded (Record only counts: reuploads send it).
        hass.config_entries.async_update_entry(
            entry, options=dict(entry.options) | {"fields": [download, upload]}
        )
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get("sensor.my_system_last_upload") is not None

        # Upload only: no Last download.
        hass.config_entries.async_update_entry(
            entry, options=dict(entry.options) | {"fields": [upload]}
        )
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get("sensor.my_system_last_download") is None
        assert hass.states.get("sensor.my_system_last_upload") is not None

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_pvoutput_history(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry, no_history
) -> None:
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
    )

    from custom_components.pvoutput_uploader.api import HistoryStatus
    from custom_components.pvoutput_uploader.archive import (
        DAILY_HOUR,
        day_window,
        statistic_id,
    )

    now = dt_util.now()
    latest = now.date() - timedelta(days=1 if now.hour >= DAILY_HOUR else 2)
    statuses = [
        HistoryStatus("10:00", {"v2": 500.0, "v1": 300.0}),
        HistoryStatus("10:10", {"v2": 700.0, "v1": 400.0}),
    ]
    no_history.side_effect = lambda day, extended: (
        statuses if day == latest.strftime("%Y%m%d") else []
    )
    fields = [
        {
            "field": "v2", "entity_id": None, "constant": None, "name": "Power",
            "active": True, "aggregate": "average", "direction": "download",
        },
    ]
    hass.config_entries.async_update_entry(
        entry, options=dict(entry.options) | {"fields": fields}
    )
    reading = StatusReading("20261011", "12:00", {"v2": 1500.0}, 0.25)

    async def hourly(stat: str) -> list[dict]:
        await async_wait_recording_done(hass)
        found = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            dt_util.utc_from_timestamp(day_window(latest)[0]),
            None,
            {stat},
            "hour",
            None,
            {"mean", "sum"},
        )
        return found.get(stat, [])

    with (
        patch(GET_SYSTEM, AsyncMock(return_value=SystemInfo("My System", 5, False))),
        patch(GET_STATUS, AsyncMock(return_value=reading)),
    ):
        await _setup(hass, entry)
        assert hass.states.get("switch.my_system_downloading").state == "on"
        assert hass.states.get("switch.my_system_daily_history_download").state == "on"
        assert hass.states.get("sensor.my_system_history_download_days_left").state == "0"

        # Yesterday is downloaded, every column as statistics.
        assert [r["mean"] for r in await hourly(statistic_id("1234", "v2"))] == [500.0, 700.0]
        # No statuses before 10:00: the 300 Wh is shared by the hours from midnight.
        v1 = await hourly(statistic_id("1234", "v1"))
        assert [round(r["sum"], 1) for r in v1[-2:]] == [300.0, 400.0]
        assert round(v1[0]["sum"], 1) == 30.0
        # The download sensor gets it for the time before it existed.
        sensor = "sensor.my_system_v2_power_from_pvoutput"
        assert [r["mean"] for r in await hourly(sensor)] == [500.0, 700.0]

        # Removing the site removes its PVOutput history statistics.
        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
        assert await hourly(statistic_id("1234", "v2")) == []


async def test_download_history_from_the_menu(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry, no_history
) -> None:
    from custom_components.pvoutput_uploader.api import HistoryStatus

    status = [HistoryStatus("10:00", {"v2": 500.0})]
    # PVOutput has detail from 1 October 2026 on.
    no_history.side_effect = lambda day, extended: status if day >= "20261001" else []
    with patch(GET_SYSTEM, AsyncMock(return_value=SystemInfo("My System", 5, False))):
        await _setup(hass, entry)
        archive = entry.runtime_data.uploader.archive

        async def ask(start: str, end: str) -> dict:
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"next_step_id": "download_history"}
            )
            assert result["step_id"] == "download_history"
            return await hass.config_entries.options.async_configure(
                result["flow_id"], {"start": start, "end": end}
            )

        # All of it has detail: queued straight away.
        result = await ask("2026-10-02", "2026-10-05")
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "history_download_queued"
        assert result["description_placeholders"]["days"] == "4"
        await hass.async_block_till_done(wait_background_tasks=True)

        # The start has none: say from when it has, then download that part.
        result = await ask("2026-09-20", "2026-10-03")
        assert result["step_id"] == "download_history_partial"
        assert result["description_placeholders"]["first"] == "2026-10-01"
        assert result["description_placeholders"]["missing"] == "11"
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
        assert result["reason"] == "history_download_queued"
        assert result["description_placeholders"]["start"] == "2026-10-01"
        assert archive.backfill is None or archive.backfill["end"] == "2026-10-03"

        # Nothing at all.
        result = await ask("2026-08-01", "2026-08-31")
        assert result["reason"] == "no_history_detail"

        # The end before the start.
        result = await ask("2026-10-05", "2026-10-01")
        assert result["errors"] == {"base": "end_before_start"}

    assert await hass.config_entries.async_unload(entry.entry_id)

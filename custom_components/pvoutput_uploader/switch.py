"""The Uploading, Downloading and Daily history download switches.

Fields are set with their select.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_SYSTEM_ID
from .coordinator import PVOutputConfigEntry, PVOutputCoordinator
from .entity import PVOutputEntity

ACTIVE_PREFIX = "active_"
DOWNLOADING = "downloading"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PVOutputConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switches."""
    coordinator = entry.runtime_data
    sid = entry.data[CONF_SYSTEM_ID]
    switches: list[FlagSwitch] = [
        UploadSwitch(coordinator, "uploading"),
        DailyHistorySwitch(coordinator, "daily_history"),
    ]
    # Downloading only while a field is in Download mode.
    downloading = bool(coordinator.uploader.download_mappings)
    if downloading:
        switches.append(DownloadSwitch(coordinator, DOWNLOADING))
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.domain != "switch":
            continue
        # Before 0.1.7 each field had a Send switch; its select replaces it.
        if reg_entry.unique_id.startswith(f"{sid}_{ACTIVE_PREFIX}") or (
            reg_entry.unique_id == f"{sid}_{DOWNLOADING}" and not downloading
        ):
            registry.async_remove(reg_entry.entity_id)
    async_add_entities(switches)


class FlagSwitch(PVOutputEntity, SwitchEntity, RestoreEntity):
    """A switch for an on/off flag of the uploader, kept across restarts."""

    def __init__(self, coordinator: PVOutputCoordinator, key: str) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, key)

    def _get(self) -> bool:
        raise NotImplementedError

    def _set(self, value: bool) -> None:
        raise NotImplementedError

    async def async_added_to_hass(self) -> None:
        """Restore the last state."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._set(last.state != STATE_OFF)

    @property
    def available(self) -> bool:
        """Always available."""
        return True

    @property
    def is_on(self) -> bool:
        """Return whether the flag is on."""
        return self._get()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on, and run a cycle to act on it."""
        self._set(True)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off."""
        self._set(False)
        self.async_write_ha_state()


class UploadSwitch(FlagSwitch):
    """Turns sending to PVOutput on and off. The history log keeps recording."""

    def _get(self) -> bool:
        return self.coordinator.uploader.upload_enabled

    def _set(self, value: bool) -> None:
        self.coordinator.uploader.upload_enabled = value


class DownloadSwitch(FlagSwitch):
    """Turns the live reads of fields in Download mode on and off."""

    def _get(self) -> bool:
        return self.coordinator.uploader.download_enabled

    def _set(self, value: bool) -> None:
        self.coordinator.uploader.download_enabled = value


class DailyHistorySwitch(FlagSwitch):
    """Turns the daily download of yesterday's PVOutput history on and off."""

    def _get(self) -> bool:
        return self.coordinator.uploader.archive.enabled

    def _set(self, value: bool) -> None:
        self.coordinator.uploader.archive.enabled = value

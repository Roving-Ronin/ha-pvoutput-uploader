"""The Uploading switch (fields are set with their select)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_SYSTEM_ID
from .coordinator import PVOutputConfigEntry
from .entity import PVOutputEntity

ACTIVE_PREFIX = "active_"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PVOutputConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switches."""
    coordinator = entry.runtime_data
    # Before 0.1.7 each field had a Send switch; its select replaces it.
    prefix = f"{entry.data[CONF_SYSTEM_ID]}_{ACTIVE_PREFIX}"
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.domain == "switch" and reg_entry.unique_id.startswith(prefix):
            registry.async_remove(reg_entry.entity_id)
    async_add_entities([UploadSwitch(coordinator, "uploading")])


class UploadSwitch(PVOutputEntity, SwitchEntity, RestoreEntity):
    """Turns sending to PVOutput on and off."""

    async def async_added_to_hass(self) -> None:
        """Restore the last state."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self.coordinator.uploader.upload_enabled = last.state != STATE_OFF

    @property
    def available(self) -> bool:
        """Always available."""
        return True

    @property
    def is_on(self) -> bool:
        """Return whether uploading is on."""
        return self.coordinator.uploader.upload_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Resume uploading."""
        self.coordinator.uploader.upload_enabled = True
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop uploading. The history log keeps recording."""
        self.coordinator.uploader.upload_enabled = False
        self.async_write_ha_state()


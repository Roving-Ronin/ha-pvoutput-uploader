"""Switch that pauses uploading while recording carries on."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import PVOutputConfigEntry
from .entity import PVOutputEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PVOutputConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switch."""
    async_add_entities([UploadSwitch(entry.runtime_data, "uploading")])


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

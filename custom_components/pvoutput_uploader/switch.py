"""Switches: uploading on/off, and one per field to make it active or passive."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_FIELDS, CONF_SYSTEM_ID, MAP_ACTIVE, MAP_FIELD
from .coordinator import PVOutputConfigEntry, PVOutputCoordinator
from .entity import PVOutputEntity
from .uploader import FieldMapping

ACTIVE_PREFIX = "active_"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PVOutputConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switches."""
    coordinator = entry.runtime_data
    fields = [
        FieldActiveSwitch(coordinator, mapping) for mapping in coordinator.uploader.mappings
    ]
    # Drop the switches of fields that were removed.
    wanted = {switch.unique_id for switch in fields}
    prefix = f"{entry.data[CONF_SYSTEM_ID]}_{ACTIVE_PREFIX}"
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            reg_entry.domain == "switch"
            and reg_entry.unique_id.startswith(prefix)
            and reg_entry.unique_id not in wanted
        ):
            registry.async_remove(reg_entry.entity_id)
    async_add_entities([UploadSwitch(coordinator, "uploading"), *fields])


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


class FieldActiveSwitch(PVOutputEntity, SwitchEntity):
    """Makes a field active (sent) or passive (recorded only).

    The same as the Active tick under Configure → Edit field. Changing it saves
    the configuration, and the site reloads to apply it.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PVOutputCoordinator, mapping: FieldMapping) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, f"{ACTIVE_PREFIX}{mapping.field}")
        self._attr_translation_key = None
        self._attr_name = f"Send {mapping.field} {mapping.name}"
        self.mapping = mapping

    @property
    def available(self) -> bool:
        """The configuration is always known."""
        return True

    @property
    def is_on(self) -> bool:
        """Return whether the field is active."""
        return self.mapping.active

    @property
    def icon(self) -> str:
        """Show active or passive."""
        return "mdi:upload" if self.mapping.active else "mdi:upload-off"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Make the field active."""
        await self._set_active(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Make the field passive: still recorded, not sent."""
        await self._set_active(False)

    async def _set_active(self, active: bool) -> None:
        entry = self.coordinator.config_entry
        options = deepcopy(dict(entry.options))
        for mapping in options.get(CONF_FIELDS, []):
            if mapping[MAP_FIELD] == self.mapping.field:
                mapping[MAP_ACTIVE] = active
        # Saving reloads the site, which recreates this switch with the new state.
        self.hass.config_entries.async_update_entry(entry, options=options)

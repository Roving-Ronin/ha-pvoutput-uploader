"""One select per field: Send, Record only, or Download."""

from __future__ import annotations

from copy import deepcopy

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_FIELDS,
    CONF_SYSTEM_ID,
    DOMAIN,
    FIELDS_BY_KEY,
    MAP_FIELD,
    MODE_DOWNLOAD,
    MODE_RECORD,
    MODE_SEND,
    apply_mode,
    downloadable,
)
from .coordinator import PVOutputConfigEntry, PVOutputCoordinator
from .entity import PVOutputEntity
from .uploader import FieldMapping

DIRECTION_PREFIX = "direction_"


def field_modes(mapping: FieldMapping, donor: bool) -> list[str]:
    """Return what a field can be set to.

    Send and Record only need an HA sensor or fixed value; Download needs a
    field PVOutput returns (not the battery fields; v7-v12 for donors only).
    A field never offers what it cannot do: a download-only field gets its
    source under Configure first. The current mode is always listed.
    """
    modes = [MODE_SEND, MODE_RECORD] if mapping.has_source else []
    if mapping.download or (
        downloadable(mapping.field) and (donor or not FIELDS_BY_KEY[mapping.field].donor)
    ):
        modes.append(MODE_DOWNLOAD)
    return modes


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PVOutputConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the field selects."""
    coordinator = entry.runtime_data
    selects = [
        FieldModeSelect(coordinator, mapping) for mapping in coordinator.uploader.mappings
    ]
    # Drop the selects of fields that were removed.
    wanted = {select.unique_id for select in selects}
    prefix = f"{entry.data[CONF_SYSTEM_ID]}_{DIRECTION_PREFIX}"
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            reg_entry.domain == "select"
            and reg_entry.unique_id.startswith(prefix)
            and reg_entry.unique_id not in wanted
        ):
            registry.async_remove(reg_entry.entity_id)
    async_add_entities(selects)


class FieldModeSelect(PVOutputEntity, SelectEntity):
    """Send, Record only (recorded, not sent) or Download (read from PVOutput).

    Changing it saves the field's direction and active flag; the site reloads
    to apply it. The upload source and settings are kept while downloading, so
    switching back restores them.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PVOutputCoordinator, mapping: FieldMapping) -> None:
        """Initialize the select."""
        super().__init__(coordinator, f"{DIRECTION_PREFIX}{mapping.field}")
        self._attr_translation_key = "field_mode"
        self._attr_name = f"{mapping.field} {mapping.name}"
        self._attr_options = field_modes(mapping, coordinator.uploader.donor)
        self.mapping = mapping

    @property
    def available(self) -> bool:
        """The configuration is always known."""
        return True

    @property
    def current_option(self) -> str:
        """Return the field's mode."""
        return self.mapping.mode

    @property
    def icon(self) -> str:
        """Show the mode."""
        return {
            MODE_SEND: "mdi:upload",
            MODE_RECORD: "mdi:upload-off",
            MODE_DOWNLOAD: "mdi:download",
        }[self.mapping.mode]

    async def async_select_option(self, option: str) -> None:
        """Change the field's mode."""
        entry = self.coordinator.config_entry
        options = deepcopy(dict(entry.options))
        for field in options.get(CONF_FIELDS, []):
            if field[MAP_FIELD] != self.mapping.field:
                continue
            # Checked against the field as saved now and the current account,
            # not only the options listed when the select was created.
            allowed = field_modes(FieldMapping.from_dict(field), self.coordinator.uploader.donor)
            if option not in allowed:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="mode_not_allowed",
                    translation_placeholders={
                        "field": self.mapping.field,
                        "mode": option,
                        "allowed": ", ".join(allowed),
                    },
                )
            apply_mode(field, option)
        # Saving reloads the site, which recreates this select with the new mode.
        self.hass.config_entries.async_update_entry(entry, options=options)

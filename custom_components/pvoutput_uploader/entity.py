"""Base entity for PVOutput Uploader."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_SYSTEM_ID, CONF_SYSTEM_NAME, DOMAIN
from .coordinator import PVOutputCoordinator


class PVOutputEntity(CoordinatorEntity[PVOutputCoordinator]):
    """Entity tied to one PVOutput system."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PVOutputCoordinator, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        data = coordinator.config_entry.data
        system_id = data[CONF_SYSTEM_ID]
        self._attr_translation_key = key
        self._attr_unique_id = f"{system_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, system_id)},
            name=data.get(CONF_SYSTEM_NAME) or f"PVOutput {system_id}",
            manufacturer="PVOutput",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=f"https://pvoutput.org/list.jsp?sid={system_id}",
        )

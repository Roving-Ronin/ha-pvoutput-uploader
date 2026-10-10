"""Status sensors for PVOutput Uploader."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import PVOutputConfigEntry
from .entity import PVOutputEntity
from .uploader import Uploader


def _ts(value: float | None) -> datetime | None:
    return dt_util.utc_from_timestamp(value) if value else None


@dataclass(frozen=True, kw_only=True)
class PVOutputSensorDescription(SensorEntityDescription):
    """Describes a status sensor."""

    value_fn: Callable[[Uploader], Any]


SENSORS = (
    PVOutputSensorDescription(
        key="last_upload",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda u: u.last_upload,
    ),
    PVOutputSensorDescription(
        key="pending_samples",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda u: u.summary.get("pending"),
    ),
    PVOutputSensorDescription(
        key="queued_outputs",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda u: u.summary.get("queued_outputs"),
    ),
    PVOutputSensorDescription(
        key="failed_samples",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: u.summary.get("failed"),
    ),
    PVOutputSensorDescription(
        key="failed_outputs",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: u.summary.get("failed_outputs"),
    ),
    PVOutputSensorDescription(
        key="history_samples",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: u.summary.get("total"),
    ),
    PVOutputSensorDescription(
        key="history_oldest",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: _ts(u.summary.get("oldest")),
    ),
    PVOutputSensorDescription(
        key="rate_limit_remaining",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: u.client.rate_remaining,
    ),
    PVOutputSensorDescription(
        key="paused_until",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: _ts(u.paused_until)
        if u.paused_until and u.paused_until > dt_util.utcnow().timestamp()
        else None,
    ),
    PVOutputSensorDescription(
        key="account_type",
        device_class=SensorDeviceClass.ENUM,
        options=["donor", "standard"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: "donor" if u.donor else "standard",
    ),
    PVOutputSensorDescription(
        key="last_error",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: (u.last_error or "")[:255] or None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PVOutputConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the status sensors."""
    async_add_entities(
        PVOutputSensor(entry.runtime_data, description) for description in SENSORS
    )


class PVOutputSensor(PVOutputEntity, SensorEntity):
    """A status value of the uploader."""

    entity_description: PVOutputSensorDescription

    def __init__(self, coordinator, description: PVOutputSensorDescription) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """The uploader keeps its own state, so the sensors stay available."""
        return True

    @property
    def native_value(self) -> Any:
        """Return the value."""
        return self.entity_description.value_fn(self.coordinator.uploader)

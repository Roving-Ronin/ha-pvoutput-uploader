"""Sensors for PVOutput Uploader: one per configured field, plus status."""

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
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    AGG_ENERGY_TODAY,
    BATTERY_STATE,
    BATTERY_STATE_CODES,
    CONF_SYSTEM_ID,
    FIELDS_BY_KEY,
    KIND_ENERGY,
    KIND_POWER,
    KIND_TEMPERATURE,
    KIND_VOLTAGE,
    upload_unit,
)
from .coordinator import PVOutputConfigEntry, PVOutputCoordinator
from .entity import PVOutputEntity
from .uploader import FieldMapping, Uploader


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
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: u.last_upload,
    ),
    PVOutputSensorDescription(
        key="pending_samples",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: u.summary.get("pending"),
    ),
    PVOutputSensorDescription(
        key="queued_outputs",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: u.summary.get("queued_outputs"),
    ),
    PVOutputSensorDescription(
        key="failed_samples",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: u.summary.get("failed"),
    ),
    PVOutputSensorDescription(
        key="partial_samples",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: u.summary.get("partial"),
    ),
    PVOutputSensorDescription(
        key="cleared_samples",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: u.summary.get("cleared"),
    ),
    PVOutputSensorDescription(
        key="without_battery_samples",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: u.summary.get("without_battery"),
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
        # A recorder problem first: it stays until reading works again.
        value_fn=lambda u: (u.record_error or u.last_error or "")[:255] or None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PVOutputConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the field and status sensors."""
    coordinator = entry.runtime_data
    uploader = coordinator.uploader
    fields: list[SensorEntity] = [
        FieldSensor(coordinator, mapping) for mapping in uploader.mappings
    ]
    downloads: list[SensorEntity] = [
        DownloadSensor(coordinator, mapping) for mapping in uploader.download_mappings
    ]
    if downloads:
        downloads.append(PVOutputSensor(coordinator, EFFICIENCY))
    # Drop the sensors of removed fields, and download sensors no longer needed.
    wanted = {sensor.unique_id for sensor in (*fields, *downloads)}
    sid = entry.data[CONF_SYSTEM_ID]
    managed = (f"{sid}_field_", f"{sid}_download_", f"{sid}_normalised_output")
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            reg_entry.domain == "sensor"
            and reg_entry.unique_id.startswith(managed)
            and reg_entry.unique_id not in wanted
        ):
            registry.async_remove(reg_entry.entity_id)
    async_add_entities(
        [
            *fields,
            *downloads,
            *(PVOutputSensor(coordinator, description) for description in SENSORS),
        ]
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


# What a field's 5 minute values are recorded in.
RECORDED_UNITS = {
    KIND_ENERGY: "Wh per 5 minutes",
    KIND_POWER: UnitOfPower.WATT,
    KIND_TEMPERATURE: UnitOfTemperature.CELSIUS,
    KIND_VOLTAGE: UnitOfElectricPotential.VOLT,
}


def _number(value: float) -> str:
    return f"{value:g}"


class FieldSensor(PVOutputEntity, SensorEntity):
    """A configured PVOutput field: what is sent, and where it comes from.

    The state is the source sensor's friendly name, or the fixed value.
    """

    def __init__(self, coordinator: PVOutputCoordinator, mapping: FieldMapping) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, f"field_{mapping.field}")
        self._attr_translation_key = None
        self._attr_name = f"{mapping.field} {mapping.name}"
        self.mapping = mapping

    @property
    def available(self) -> bool:
        """The configuration is always known."""
        return True

    @property
    def _configured_to_send(self) -> bool:
        """Active and allowed by the account type.

        Not whether uploading is switched on, nor whether PVOutput accepted it.
        """
        return self.mapping in self.coordinator.uploader.active_mappings

    @property
    def icon(self) -> str:
        """Show download, or whether the field is configured to be sent."""
        if self.mapping.download:
            return "mdi:download"
        return "mdi:upload" if self._configured_to_send else "mdi:upload-off"

    @property
    def _upload_unit(self) -> str | None:
        m = self.mapping
        state = self.hass.states.get(m.entity_id) if m.entity_id else None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) if state else None
        return upload_unit(m.field, m.aggregate, unit or m.unit)

    @property
    def native_value(self) -> str:
        """Return a readable source name or fixed value, with the upload unit.

        Download fields show PVOutput: that is where their value comes from.
        """
        if self.mapping.download:
            return "PVOutput"
        value = self._source_label
        unit = self._upload_unit
        return f"{value[:255 - len(unit) - 1]} {unit}" if unit else value[:255]

    @property
    def _source_label(self) -> str:
        """The selected source friendly name, or the fixed number."""
        if entity_id := self.mapping.entity_id:
            if (state := self.hass.states.get(entity_id)) is not None:
                return state.name[:255]
            registry_entry = er.async_get(self.hass).async_get(entity_id)
            if registry_entry is not None:
                name = registry_entry.name or registry_entry.original_name
                if name:
                    return name[:255]
            return entity_id.split(".", 1)[-1].replace("_", " ")[:255]
        value = self.mapping.constant or 0
        if self.mapping.field == BATTERY_STATE and int(value) in BATTERY_STATE_CODES:
            return f"{int(value)} {BATTERY_STATE_CODES[int(value)]}"
        return _number(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Describe how the field is sent."""
        m = self.mapping
        field = FIELDS_BY_KEY[m.field]
        uploader = self.coordinator.uploader
        attrs: dict[str, Any] = {
            "pvoutput_field": m.field,
            "pvoutput_field_name": field.name,
            "source": "sensor" if m.entity_id else "fixed value" if m.has_source else "none",
            "direction": m.direction,
            "mode": m.mode,
            "active": m.active,
            "configured_to_send": self._configured_to_send,
            "pvoutput_unit": self._upload_unit,
        }
        if m.active and not m.download and not self._configured_to_send:
            if field.donor and not uploader.donor:
                reason = "needs a PVOutput donor account"
            else:
                reason = "needs an active b1 (battery power): PVOutput ignores battery fields without it"
            attrs["not_configured_to_send_because"] = reason
        if m.entity_id:
            attrs["source_entity"] = m.entity_id
            state = self.hass.states.get(m.entity_id)
            attrs["source_unit"] = (
                state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) if state else None
            ) or m.unit
            if (state := self.hass.states.get(m.entity_id)) is not None:
                attrs["source_name"] = state.name
            attrs["value_per_interval"] = m.aggregate
            if (latest := uploader.latest.get(m.field)) is not None:
                ts, value = latest
                attrs["last_recorded"] = dt_util.utc_from_timestamp(ts).isoformat()
                attrs["last_recorded_value"] = round(value, 3)
                kind = KIND_ENERGY if m.aggregate == AGG_ENERGY_TODAY else field.kind
                if (unit := RECORDED_UNITS.get(kind)) is not None:
                    attrs["last_recorded_unit"] = unit
        return attrs


DEVICE_CLASSES = {
    KIND_ENERGY: SensorDeviceClass.ENERGY,
    KIND_POWER: SensorDeviceClass.POWER,
    KIND_TEMPERATURE: SensorDeviceClass.TEMPERATURE,
    KIND_VOLTAGE: SensorDeviceClass.VOLTAGE,
}


class DownloadSensor(PVOutputEntity, SensorEntity):
    """A field's value as PVOutput holds it now (download direction).

    Read once per status interval with getstatus.jsp. PVOutput's "NaN" (no
    value) shows as unknown, not 0. Energy fields count since local midnight,
    so they are total_increasing and work in the Energy dashboard.
    """

    _attr_icon = "mdi:download"

    def __init__(self, coordinator: PVOutputCoordinator, mapping: FieldMapping) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, f"download_{mapping.field}")
        self._attr_translation_key = None
        self._attr_name = f"{mapping.field} {mapping.name} from PVOutput"
        self.mapping = mapping
        field = FIELDS_BY_KEY[mapping.field]
        kind = KIND_ENERGY if mapping.aggregate == AGG_ENERGY_TODAY else field.kind
        self._attr_native_unit_of_measurement = upload_unit(
            mapping.field, mapping.aggregate, mapping.unit
        )
        self._attr_device_class = DEVICE_CLASSES.get(kind)
        self._attr_state_class = (
            SensorStateClass.TOTAL_INCREASING
            if kind == KIND_ENERGY
            else SensorStateClass.MEASUREMENT
        )

    @property
    def available(self) -> bool:
        """Unknown rather than unavailable while PVOutput has no value."""
        return True

    @property
    def native_value(self) -> float | None:
        """Return the value PVOutput holds."""
        reading = self.coordinator.uploader.reading
        return reading.values.get(self.mapping.field) if reading else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Tell which PVOutput field and status the value comes from."""
        attrs: dict[str, Any] = {"pvoutput_field": self.mapping.field}
        if (reading := self.coordinator.uploader.reading) is not None:
            attrs["pvoutput_status"] = f"{reading.date} {reading.time}"
        return attrs


EFFICIENCY = PVOutputSensorDescription(
    key="normalised_output",
    native_unit_of_measurement="kWh/kW",
    state_class=SensorStateClass.MEASUREMENT,
    value_fn=lambda u: u.reading.efficiency if u.reading else None,
)

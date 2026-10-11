"""Services of the PVOutput Uploader integration."""

from __future__ import annotations

from datetime import date, datetime

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import PVOutputConfigEntry
from .uploader import REUPLOAD_AUTO, REUPLOAD_OUTPUTS, REUPLOAD_STATUSES

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_START = "start"
ATTR_END = "end"
ATTR_METHOD = "method"
ATTR_OVERWRITE = "overwrite"
ATTR_UPLOAD = "upload"
ATTR_SINCE_FAILURE = "since_failure"
ATTR_WITHOUT_BATTERY = "without_battery"

SERVICE_REUPLOAD = "reupload"
SERVICE_IMPORT = "import_from_recorder"

REUPLOAD_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_START): vol.Any(cv.date, cv.datetime),
        vol.Optional(ATTR_SINCE_FAILURE, default=False): cv.boolean,
        vol.Optional(ATTR_WITHOUT_BATTERY, default=False): cv.boolean,
        vol.Optional(ATTR_END): vol.Any(cv.date, cv.datetime),
        vol.Optional(ATTR_METHOD, default=REUPLOAD_AUTO): vol.In(
            [REUPLOAD_AUTO, REUPLOAD_STATUSES, REUPLOAD_OUTPUTS]
        ),
    }
)
IMPORT_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_START): cv.datetime,
        vol.Optional(ATTR_END): cv.datetime,
        vol.Optional(ATTR_OVERWRITE, default=False): cv.boolean,
        vol.Optional(ATTR_UPLOAD, default=False): cv.boolean,
    }
)


def _entry(hass: HomeAssistant, call: ServiceCall) -> PVOutputConfigEntry:
    entries = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED
    ]
    if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
        entries = [e for e in entries if e.entry_id == entry_id]
    if len(entries) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="select_entry"
        )
    return entries[0]


def _as_local(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt_util.get_default_time_zone())


def _start_of(value: date) -> datetime:
    """Return a date as local midnight, or a datetime as local time."""
    if isinstance(value, datetime):
        return _as_local(value)
    return dt_util.start_of_local_day(value)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration services."""

    async def reupload(call: ServiceCall) -> ServiceResponse:
        entry = _entry(hass, call)
        coordinator = entry.runtime_data
        uploader = coordinator.uploader
        # A day (to its end) or a date and time.
        end: date | datetime = call.data.get(ATTR_END) or dt_util.now().date()
        if isinstance(end, datetime):
            end = _as_local(end)
        if call.data[ATTR_SINCE_FAILURE]:
            if (first := await uploader.async_first_unsent()) is None:
                return {"status_days": 0, "output_days": 0}
            start = dt_util.as_local(dt_util.utc_from_timestamp(first))
        elif ATTR_START in call.data:
            start = _start_of(call.data[ATTR_START])
        else:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="start_required"
            )
        if (end < start) if isinstance(end, datetime) else (end < start.date()):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="end_before_start"
            )
        result = await uploader.async_reupload(
            start, end, call.data[ATTR_METHOD], without_battery=call.data[ATTR_WITHOUT_BATTERY]
        )
        await coordinator.async_request_refresh()
        return result

    async def import_from_recorder(call: ServiceCall) -> ServiceResponse:
        entry = _entry(hass, call)
        start = _as_local(call.data[ATTR_START])
        end = _as_local(call.data.get(ATTR_END) or dt_util.now())
        if end <= start:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="end_before_start"
            )
        coordinator = entry.runtime_data
        result = await coordinator.uploader.async_import(
            start, end, call.data[ATTR_OVERWRITE], call.data[ATTR_UPLOAD]
        )
        await coordinator.async_request_refresh()
        return result

    hass.services.async_register(
        DOMAIN,
        SERVICE_REUPLOAD,
        reupload,
        schema=REUPLOAD_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT,
        import_from_recorder,
        schema=IMPORT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

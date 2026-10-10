"""The PVOutput Uploader integration."""

from __future__ import annotations

import logging

from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import PVOutputAuthError, PVOutputClient, PVOutputError
from .const import (
    CONF_DETECTED_DONOR,
    CONF_DETECTED_INTERVAL,
    CONF_SYSTEM_ID,
    CONF_SYSTEM_NAME,
    DOMAIN,
)
from .coordinator import PVOutputConfigEntry, PVOutputCoordinator
from .services import async_setup_services
from .store import SampleStore
from .uploader import Uploader

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.SWITCH]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PVOutputConfigEntry) -> bool:
    """Set up PVOutput Uploader from a config entry."""
    client = PVOutputClient(
        async_get_clientsession(hass), entry.data[CONF_API_KEY], entry.data[CONF_SYSTEM_ID]
    )
    try:
        system = await client.get_system()
    except PVOutputAuthError as err:
        raise ConfigEntryError(f"PVOutput refused the API key: {err}") from err
    except PVOutputError as err:
        # Keep recording; the last known account settings are used meanwhile.
        _LOGGER.warning("PVOutput not reachable, using saved system settings: %s", err)
    else:
        detected = {
            CONF_SYSTEM_NAME: system.name,
            CONF_DETECTED_INTERVAL: system.interval,
            CONF_DETECTED_DONOR: system.donor,
        }
        if any(entry.data.get(k) != v for k, v in detected.items()):
            hass.config_entries.async_update_entry(entry, data=entry.data | detected)

    store = SampleStore(hass.config.path(f"pvoutput_uploader_{entry.data[CONF_SYSTEM_ID]}.db"))
    await hass.async_add_executor_job(store.open)

    uploader = Uploader(hass, entry, client, store)
    await uploader.async_load()
    coordinator = PVOutputCoordinator(hass, entry, uploader)
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), f"{DOMAIN} first cycle"
    )
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: PVOutputConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PVOutputConfigEntry) -> bool:
    """Unload a config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    uploader = entry.runtime_data.uploader
    async with uploader.lock:
        await hass.async_add_executor_job(uploader.store.close)
    return True

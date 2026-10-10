"""Runs the uploader once a minute and shares its state with the entities."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .uploader import Uploader

_LOGGER = logging.getLogger(__name__)

type PVOutputConfigEntry = ConfigEntry[PVOutputCoordinator]


class PVOutputCoordinator(DataUpdateCoordinator[None]):
    """Drives the uploader cycle."""

    config_entry: PVOutputConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: PVOutputConfigEntry, uploader: Uploader
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=60),
        )
        self.uploader = uploader

    async def _async_update_data(self) -> None:
        """Record and upload. Failures are kept on the uploader, not raised."""
        await self.uploader.async_cycle()

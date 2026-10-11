"""Button that clears the waiting history from the upload queue."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PVOutputConfigEntry
from .entity import PVOutputEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PVOutputConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the button."""
    async_add_entities([ClearQueueButton(entry.runtime_data, "clear_queue")])


class ClearQueueButton(PVOutputEntity, ButtonEntity):
    """Takes waiting history off the queue; current statuses keep going.

    Nothing is deleted: cleared statuses stay in the history log as unsent and
    can be sent again with Reupload history.

    No entity category: it sits with the Uploading switch under Controls, apart
    from the per-field switches under Configuration.
    """

    @property
    def available(self) -> bool:
        """Always available."""
        return True

    async def async_press(self) -> None:
        """Clear the queue."""
        await self.coordinator.uploader.async_clear_queue()
        await self.coordinator.async_request_refresh()

"""AirSend PIN lock: unlocking opens a one-shot authorization for a secure device."""
from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_INTERNAL_URL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN
from .device import Device
from .vault_store import (
    SIGNAL_SECURE_CHANGED,
    async_authorize_or_raise,
    get_vault_manager,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one lock per secure device."""
    internal_url = entry.data.get(CONF_INTERNAL_URL, "")
    entities = []
    for name, options in entry.data.get("devices", {}).items():
        device = Device(name, options, internal_url)
        if device.is_secure:
            entities.append(AirSendSecureLock(hass, device))
    async_add_entities(entities)


class AirSendSecureLock(LockEntity):
    """Locked = no valid PIN window. Unlocking requires the vault PIN.

    The window is one-shot: it closes after the first command or after a few
    seconds, whichever comes first, and the entity locks again by itself.
    """

    _attr_should_poll = False
    _attr_code_format = r"^\d{6,8}$"

    def __init__(self, hass: HomeAssistant, device: Device) -> None:
        self._hass = hass
        self._device = device
        self._vault = get_vault_manager(hass)
        self._attr_unique_id = f"{DOMAIN}_{device.unique_channel_name}_lock"
        self._attr_name = f"{device.name} PIN"

    @property
    def device_info(self) -> DeviceInfo:
        return self._device.device_info

    @property
    def is_locked(self) -> bool:
        return not self._vault.is_authorized(self._device.name)

    async def async_added_to_hass(self) -> None:
        @callback
        def _changed(device_id: str) -> None:
            if device_id == self._device.name:
                self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(self._hass, SIGNAL_SECURE_CHANGED, _changed)
        )

    async def async_unlock(self, **kwargs: Any) -> None:
        await async_authorize_or_raise(
            self._vault, self._device.name, kwargs.get("code")
        )

    async def async_lock(self, **kwargs: Any) -> None:
        self._vault.revoke(self._device.name)

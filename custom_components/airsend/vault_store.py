"""Home Assistant glue around the AirSend vault: storage, PIN windows, signals."""
from __future__ import annotations

import asyncio
import logging
import os

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .vault import (
    InvalidPin,
    PepperMissing,
    PinLocked,
    UnknownDevice,
    Vault,
    load_pepper,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "airsend.vault"
STORAGE_VERSION = 1
PEPPER_FILE = "airsend.pepper"
# One-shot authorization: after a valid PIN the decrypted secrets are kept in
# memory for at most this long and for a single command.
AUTH_WINDOW_SECONDS = 30
SIGNAL_SECURE_CHANGED = "airsend_secure_changed"
EVENT_VAULT = "airsend_vault_event"  # audit trail on the HA event bus
NOTIFY_AFTER_FAILURES = 3


class VaultManager:
    """Owns the vault, its persistence and the short PIN-authorization windows."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True)
        self._pepper_path = hass.config.path(".storage", PEPPER_FILE)
        self._vault: Vault | None = None
        self._loaded = False
        self._lock = asyncio.Lock()
        self._auth: dict[str, tuple[dict, CALLBACK_TYPE]] = {}

    # -- lifecycle -------------------------------------------------------
    async def async_load(self) -> None:
        if self._loaded:
            return
        data = await self._store.async_load()
        if data:
            try:
                pepper = await self.hass.async_add_executor_job(
                    load_pepper, self._pepper_path, False
                )
                self._vault = Vault(data, pepper)
            except PepperMissing:
                _LOGGER.error(
                    "AirSend vault exists but %s is missing: secure devices cannot "
                    "be unlocked. Remove and re-add the integration to reset.",
                    PEPPER_FILE,
                )
        self._loaded = True

    @property
    def has_vault(self) -> bool:
        return self._vault is not None

    def has_device(self, device_id: str) -> bool:
        return self._vault is not None and self._vault.has_device(device_id)

    def seconds_locked(self) -> int:
        return self._vault.seconds_locked() if self._vault else 0

    async def _async_save(self) -> None:
        if self._vault is not None:
            await self._store.async_save(self._vault.to_dict())
            self._vault.dirty = False

    async def async_create(self, pin: str) -> None:
        """Create a new vault protected by ``pin`` (raises WeakPin)."""
        pepper = await self.hass.async_add_executor_job(
            load_pepper, self._pepper_path, True
        )
        self._vault = await self.hass.async_add_executor_job(Vault.create, pin, pepper)
        await self._async_save()

    async def async_remove(self) -> None:
        """Wipe the vault and the pepper (integration removed)."""
        self.revoke_all()
        self._vault = None
        await self._store.async_remove()
        try:
            await self.hass.async_add_executor_job(os.remove, self._pepper_path)
        except FileNotFoundError:
            pass

    # -- audit -----------------------------------------------------------
    @callback
    def audit(self, kind: str, device: str | None = None) -> None:
        """Fire an ``airsend_vault_event`` (never contains PIN or secrets)."""
        self.hass.bus.async_fire(EVENT_VAULT, {"type": kind, "device": device})

    def _notify(self, title: str, message: str, key: str) -> None:
        from homeassistant.components import persistent_notification

        persistent_notification.async_create(
            self.hass, message, title=title, notification_id=f"{STORAGE_KEY}_{key}"
        )

    # -- PIN-protected operations ---------------------------------------
    async def _async_unlock(self, pin: str, device: str | None = None) -> bytes:
        """Unlock the DEK; persists failure counters even when it raises."""
        assert self._vault is not None
        async with self._lock:
            try:
                dek = await self.hass.async_add_executor_job(self._vault.unlock, pin)
            except InvalidPin:
                self.audit("pin_failed", device)
                fails = self._vault.to_dict().get("fails", 0)
                if fails >= NOTIFY_AFTER_FAILURES:
                    self._notify(
                        "AirSend vault",
                        f"{fails} wrong PIN attempts on the AirSend vault"
                        + (f" (device: {device})." if device else "."),
                        "failures",
                    )
                raise
            except PinLocked:
                self.audit("pin_locked", device)
                raise
            finally:
                if self._vault.dirty:
                    await self._async_save()
            return dek

    async def async_seal(self, pin: str, secrets_by_device: dict) -> None:
        """Seal secrets for several devices (PIN required)."""
        dek = await self._async_unlock(pin)
        for device_id, secrets in secrets_by_device.items():
            self._vault.seal_device(dek, device_id, secrets)
            self.audit("sealed", device_id)
        await self._async_save()

    async def async_remove_device(self, device_id: str) -> None:
        """Forget a device's sealed secrets (no PIN needed: it only destroys data)."""
        if self._vault is not None and self._vault.has_device(device_id):
            self.revoke(device_id)
            self._vault.remove_device(device_id)
            await self._async_save()
            self.audit("removed", device_id)

    async def async_change_pin(self, old_pin: str, new_pin: str) -> None:
        assert self._vault is not None
        async with self._lock:
            try:
                await self.hass.async_add_executor_job(
                    self._vault.change_pin, old_pin, new_pin
                )
                self.audit("pin_changed")
            finally:
                if self._vault.dirty:
                    await self._async_save()

    async def async_authorize(self, device_id: str, pin: str) -> None:
        """Validate the PIN and open a one-shot window for ``device_id``."""
        if self._vault is None:
            raise UnknownDevice(device_id)
        dek = await self._async_unlock(pin, device_id)
        secrets = self._vault.open_device(dek, device_id)
        self.revoke(device_id)
        self.audit("unlocked", device_id)

        @callback
        def _expire(_now) -> None:
            self._auth.pop(device_id, None)
            async_dispatcher_send(self.hass, SIGNAL_SECURE_CHANGED, device_id)

        cancel = async_call_later(self.hass, AUTH_WINDOW_SECONDS, _expire)
        self._auth[device_id] = (secrets, cancel)
        async_dispatcher_send(self.hass, SIGNAL_SECURE_CHANGED, device_id)

    # -- authorization window -------------------------------------------
    @callback
    def is_authorized(self, device_id: str) -> bool:
        return device_id in self._auth

    @callback
    def consume(self, device_id: str) -> dict | None:
        """Return the secrets once (single use) and close the window."""
        entry = self._auth.pop(device_id, None)
        if entry is None:
            return None
        secrets, cancel = entry
        cancel()
        async_dispatcher_send(self.hass, SIGNAL_SECURE_CHANGED, device_id)
        self.audit("command_authorized", device_id)
        return secrets

    @callback
    def revoke(self, device_id: str) -> None:
        entry = self._auth.pop(device_id, None)
        if entry is not None:
            entry[1]()
            async_dispatcher_send(self.hass, SIGNAL_SECURE_CHANGED, device_id)

    @callback
    def revoke_all(self) -> None:
        for device_id in list(self._auth):
            self.revoke(device_id)


async def async_authorize_or_raise(
    manager: VaultManager, device_id: str, pin: str | None
) -> None:
    """Authorize ``device_id`` with ``pin`` or raise a user-facing error."""
    from . import DOMAIN

    def _error(key: str, **placeholders: str) -> ServiceValidationError:
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders=placeholders,
        )

    if not pin:
        raise _error("pin_required")
    try:
        await manager.async_authorize(device_id, str(pin))
    except InvalidPin:
        raise _error("invalid_pin") from None
    except PinLocked as err:
        raise _error("pin_locked", minutes=str(max(1, err.retry_after // 60))) from None
    except UnknownDevice:
        raise _error("secure_not_sealed", device=device_id) from None


def get_vault_manager(hass: HomeAssistant) -> VaultManager:
    """Return the singleton manager (one config entry per instance)."""
    from . import DOMAIN

    domain_data = hass.data.setdefault(DOMAIN, {})
    manager = domain_data.get("_vault")
    if manager is None:
        manager = VaultManager(hass)
        domain_data["_vault"] = manager
    return manager

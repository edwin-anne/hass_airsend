"""The AirSend component."""
import os
import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.typing import ConfigType
from homeassistant.const import CONF_INTERNAL_URL

from homeassistant.components.hassio import get_addons_info

DOMAIN = "airsend"
AS_PLATFORMS = ["cover", "switch", "button", "light", "sensor", "binary_sensor", "lock"]
SECURE_SERVICES = {
    "secure_open": "open_cover",
    "secure_close": "close_cover",
    "secure_stop": "stop_cover",
}

_LOGGER = logging.getLogger(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Legacy YAML setup — no longer used for device loading."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up AirSend from a config entry."""
    from .coordinator import AirSendCoordinator
    from .device import Device, set_secrets_provider
    from .vault_store import get_vault_manager

    hass.data.setdefault(DOMAIN, {})

    vault = get_vault_manager(hass)
    await vault.async_load()
    set_secrets_provider(vault.consume)
    _async_register_services(hass)

    detected_url = await get_internal_url(hass)

    if detected_url:
        internal_url = detected_url

        if detected_url != entry.data.get(CONF_INTERNAL_URL):
            hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_INTERNAL_URL: detected_url,
                },
            )
    else:
        internal_url = entry.data.get(CONF_INTERNAL_URL, "")

    devices_config = entry.data.get("devices", {})

    coordinators = {}
    for name, options in devices_config.items():
        device = Device(name, options, internal_url)
        coordinator = AirSendCoordinator(hass, device)
        coordinators[name] = coordinator

    hass.data[DOMAIN][entry.entry_id] = {
        "entry": {
            **entry.data,
            CONF_INTERNAL_URL: internal_url,
        },
        "coordinators": coordinators,
    }

    await hass.config_entries.async_forward_entry_setups(entry, AS_PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    from .device import set_secrets_provider
    from .vault_store import get_vault_manager

    unload_ok = await hass.config_entries.async_unload_platforms(entry, AS_PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        get_vault_manager(hass).revoke_all()
        set_secrets_provider(None)
        for service in SECURE_SERVICES:
            hass.services.async_remove(DOMAIN, service)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Wipe the vault (and its pepper) when the integration is removed."""
    from .vault_store import get_vault_manager

    await get_vault_manager(hass).async_remove()
    hass.data.get(DOMAIN, {}).pop("_vault", None)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register airsend.secure_open / secure_close / secure_stop."""
    from .vault_store import async_authorize_or_raise, get_vault_manager

    schema = vol.Schema(
        {vol.Required("entity_id"): cv.entity_id, vol.Required("pin"): cv.string}
    )

    def _make_handler(cover_service: str):
        async def _handler(call: ServiceCall) -> None:
            entity_id = call.data["entity_id"]
            state = hass.states.get(entity_id)
            device = state.attributes.get("secure_device") if state else None
            if not device:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="not_secure_entity",
                    translation_placeholders={"entity": entity_id},
                )
            vault = get_vault_manager(hass)
            await async_authorize_or_raise(vault, device, call.data["pin"])
            try:
                await hass.services.async_call(
                    "cover", cover_service, {"entity_id": entity_id}, blocking=True
                )
            finally:
                vault.revoke(device)

        return _handler

    for service, cover_service in SECURE_SERVICES.items():
        if not hass.services.has_service(DOMAIN, service):
            hass.services.async_register(
                DOMAIN, service, _make_handler(cover_service), schema=schema
            )


def load_airsend_yaml(hass: HomeAssistant) -> dict:
    """Load airsend.yaml resolving !secret tags via secrets.yaml."""
    import yaml as _yaml

    config_dir = hass.config.config_dir
    path = os.path.join(config_dir, "airsend.yaml")

    if not os.path.exists(path):
        _LOGGER.error("airsend.yaml not found at %s", path)
        return {}

    secrets = {}
    secrets_path = os.path.join(config_dir, "secrets.yaml")
    if os.path.exists(secrets_path):
        try:
            with open(secrets_path, "r", encoding="utf-8") as f:
                secrets = _yaml.safe_load(f) or {}
        except Exception as e:
            _LOGGER.warning("Could not load secrets.yaml: %s", e)

    def secret_constructor(loader, node):
        key = loader.construct_scalar(node)
        value = secrets.get(key)
        if value is None:
            _LOGGER.warning("Secret '%s' not found in secrets.yaml", key)
            return ""
        return value

    loader_class = type("SecretLoader", (_yaml.SafeLoader,), {})
    loader_class.add_constructor("!secret", secret_constructor)

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _yaml.load(f, Loader=loader_class)  # noqa: S506
        _LOGGER.debug("airsend.yaml loaded with %d keys", len(data) if data else 0)
        return data or {}
    except Exception as e:
        _LOGGER.error("Failed to load airsend.yaml: %s", e)
        return {}


async def get_internal_url(hass: HomeAssistant) -> str:
    """Auto-detect internal URL of the AirSend addon."""
    try:
        addons_info = get_addons_info(hass)
        for name, options in addons_info.items():
            if "_airsend" in name:
                ip = options.get("ip_address")
                if ip:
                    return "http://" + str(ip) + ":33863/"
    except Exception:
        pass
    return ""

"""Config flow for AirSend integration."""
import os
import logging
import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_INTERNAL_URL
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import DOMAIN, load_airsend_yaml, get_internal_url
from .vault import InvalidPin, PinLocked, WeakPin, split_secure_devices
from .vault_store import get_vault_manager

_PIN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

_LOGGER = logging.getLogger(DOMAIN)


async def test_connection(url: str) -> str | None:
    """Test connection to AirSend addon. Returns None if OK, error key otherwise."""
    if not url:
        return "no_url"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                # L'addon répond même avec 401/404, l'important est qu'il réponde
                if response.status < 500:
                    return None
                return "cannot_connect"
    except aiohttp.ClientConnectorError:
        return "cannot_connect"
    except TimeoutError:
        return "timeout"
    except Exception:
        return "cannot_connect"


class AirSendConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the AirSend config flow."""

    VERSION = 1

    _pending: dict | None = None

    async def _async_prepare_devices(self, devices: dict, url: str, entry=None):
        """Strip secrets of secure devices; ask for the PIN when they must be sealed.

        Returns (result, error). ``result`` is a flow result when the PIN step is
        needed, ``None`` when the entry can be written straight away.
        """
        public, secrets = split_secure_devices(devices)
        vault = get_vault_manager(self.hass)
        await vault.async_load()
        secure_names = [n for n, o in devices.items() if isinstance(o, dict) and o.get("secure")]
        missing = [n for n in secure_names if n not in secrets and not vault.has_device(n)]
        if missing:
            return None, "secure_no_secrets", public
        self._pending = {"url": url, "devices": public, "secrets": secrets, "entry": entry}
        if secrets:
            return await self.async_step_pin(), None, public
        return None, None, public

    async def async_step_pin(self, user_input=None):
        """Create the vault (or unlock it) and seal the secrets of secure devices."""
        errors = {}
        vault = get_vault_manager(self.hass)
        await vault.async_load()
        creating = not vault.has_vault
        pending = self._pending

        if user_input is not None:
            pin = user_input["pin"]
            try:
                if creating:
                    if pin != user_input.get("pin_confirm"):
                        errors["base"] = "pin_mismatch"
                    else:
                        await vault.async_create(pin)
                if not errors:
                    await vault.async_seal(pin, pending["secrets"])
                    return await self._async_finish()
            except WeakPin:
                errors["base"] = "weak_pin"
            except InvalidPin:
                errors["base"] = "invalid_pin"
            except PinLocked as err:
                errors["base"] = "pin_locked"
                _LOGGER.warning("AirSend vault locked for %ss", err.retry_after)

        schema = {vol.Required("pin"): _PIN_SELECTOR}
        if creating:
            schema[vol.Required("pin_confirm")] = _PIN_SELECTOR
        return self.async_show_form(
            step_id="pin",
            data_schema=vol.Schema(schema),
            description_placeholders={"devices": ", ".join(pending["secrets"])},
            errors=errors,
        )

    async def _async_finish(self):
        """Write the config entry (secrets of secure devices already sealed)."""
        pending = self._pending
        data = {CONF_INTERNAL_URL: pending["url"], "devices": pending["devices"]}
        entry = pending["entry"]
        if entry is None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="AirSend", data=data)
        self.hass.config_entries.async_update_entry(entry, data=data)
        await self.hass.config_entries.async_reload(entry.entry_id)
        return self.async_abort(reason="reconfigure_successful")

    async def async_step_user(self, user_input=None):
        """First step: detect URL, test connection, load devices."""
        errors = {}

        internal_url = await get_internal_url(self.hass)

        if user_input is not None:
            internal_url = user_input.get(CONF_INTERNAL_URL, internal_url).strip()
            if internal_url and not internal_url.endswith("/"):
                internal_url += "/"

            # Test de connexion
            connection_error = await test_connection(internal_url)
            if connection_error:
                errors["base"] = connection_error
            else:
                # Chargement du yaml
                yaml_data = await self.hass.async_add_executor_job(
                    load_airsend_yaml, self.hass
                )
                devices = yaml_data.get("devices", {})

                if not devices:
                    errors["base"] = "no_devices"
                else:
                    result, error, public = await self._async_prepare_devices(
                        devices, internal_url
                    )
                    if error:
                        errors["base"] = error
                    elif result is not None:
                        return result
                    else:
                        return await self._async_finish()

        yaml_path = self.hass.config.path("airsend.yaml")
        yaml_exists = await self.hass.async_add_executor_job(os.path.exists, yaml_path)
        if not yaml_exists and not errors:
            errors["base"] = "yaml_not_found"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_INTERNAL_URL, default=internal_url): str,
                }
            ),
            description_placeholders={"yaml_path": yaml_path},
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input=None):
        """Allow reconfiguration (reload yaml + update URL)."""
        errors = {}
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        current_url = entry.data.get(CONF_INTERNAL_URL, "")

        if user_input is not None:
            internal_url = user_input.get(CONF_INTERNAL_URL, current_url).strip()
            if internal_url and not internal_url.endswith("/"):
                internal_url += "/"

            # Test de connexion
            connection_error = await test_connection(internal_url)
            if connection_error:
                errors["base"] = connection_error
            else:
                yaml_data = await self.hass.async_add_executor_job(
                    load_airsend_yaml, self.hass
                )
                devices = yaml_data.get("devices", {})

                if not devices:
                    errors["base"] = "no_devices"
                else:
                    result, error, public = await self._async_prepare_devices(
                        devices, internal_url, entry
                    )
                    if error:
                        errors["base"] = error
                    elif result is not None:
                        return result
                    else:
                        return await self._async_finish()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_INTERNAL_URL, default=current_url): str,
                }
            ),
            errors=errors,
        )

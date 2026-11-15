"""Options flow: native pairing wizard, PIN change and device removal."""
from __future__ import annotations

import asyncio
import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_INTERNAL_URL
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import DOMAIN
from .enroll import (
    PROG_NOTE,
    AirSendApi,
    AirSendApiError,
    describe_protocol,
)
from .vault import InvalidPin, PinLocked, WeakPin
from .vault_store import get_vault_manager

_LOGGER = logging.getLogger(DOMAIN)

_PIN = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
DEVICE_TYPES = {"4098": "cover", "4097": "switch", "4096": "button", "4100": "light"}
CAPTURE_WINDOW_SECONDS = 60


def _select(options: list[tuple[str, str]], translation_key: str | None = None):
    return SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(value=v, label=l) for v, l in options],
            mode=SelectSelectorMode.DROPDOWN,
            translation_key=translation_key,
        )
    )


class AirSendOptionsFlow(config_entries.OptionsFlow):
    """Manage native AirSend devices and the vault from the UI."""

    def __init__(self) -> None:
        self._spurl = ""
        self._mode = "capture"
        self._channel: dict | None = None
        self._protocol: int | None = None
        self._task: asyncio.Task | None = None
        self._pending_device: dict | None = None
        self._capture_error: str | None = None

    # -- helpers ---------------------------------------------------------
    def _api(self) -> AirSendApi:
        return AirSendApi(
            async_get_clientsession(self.hass),
            self.config_entry.data.get(CONF_INTERNAL_URL, ""),
            self._spurl,
        )

    @property
    def _devices(self) -> dict:
        return dict(self.config_entry.data.get("devices", {}))

    def _default_spurl(self) -> str:
        for options in self._devices.values():
            if isinstance(options, dict) and options.get("spurl"):
                return options["spurl"]
        return ""

    async def _async_save_devices(self, devices: dict):
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, "devices": devices},
        )
        await self.hass.config_entries.async_reload(self.config_entry.entry_id)
        return self.async_create_entry(data={})

    # -- menu ------------------------------------------------------------
    async def async_step_init(self, user_input=None):
        vault = get_vault_manager(self.hass)
        await vault.async_load()
        menu = ["pair", "remove_device"]
        if vault.has_vault:
            menu.append("change_pin")
        return self.async_show_menu(step_id="init", menu_options=menu)

    # -- pairing: connection + mode -------------------------------------
    async def async_step_pair(self, user_input=None):
        errors = {}
        if user_input is not None:
            self._spurl = user_input["spurl"].strip()
            self._mode = user_input["mode"]
            api = self._api()
            try:
                await api.status()
                await api.events()  # authenticated call: validates the spurl
            except AirSendApiError as err:
                errors["base"] = err.code
            else:
                if self._mode == "virtual":
                    return await self.async_step_virtual_protocol()
                return await self.async_step_capture_ready()
        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema(
                {
                    vol.Required("spurl", default=self._default_spurl()): _SECRET,
                    vol.Required("mode", default=self._mode): _select(
                        [("capture", "capture"), ("virtual", "virtual")], "pair_mode"
                    ),
                }
            ),
            errors=errors,
        )

    # -- mode A: capture an existing remote -----------------------------
    async def async_step_capture_ready(self, user_input=None):
        if user_input is not None:
            return await self.async_step_capture()
        return self.async_show_form(
            step_id="capture_ready",
            data_schema=vol.Schema({}),
            description_placeholders={"seconds": str(CAPTURE_WINDOW_SECONDS)},
            errors={"base": self._capture_error} if self._capture_error else {},
        )

    async def async_step_capture(self, user_input=None):
        if self._task is None:
            self._capture_error = None
            self._task = self.hass.async_create_task(
                self._api().capture(window=CAPTURE_WINDOW_SECONDS)
            )
        if not self._task.done():
            return self.async_show_progress(
                step_id="capture", progress_action="listen", progress_task=self._task
            )
        return self.async_show_progress_done(next_step_id="capture_done")

    async def async_step_capture_done(self, user_input=None):
        task, self._task = self._task, None
        try:
            candidates = task.result()
        except AirSendApiError as err:
            self._capture_error = err.code
            return await self.async_step_capture_ready()
        if not candidates:
            self._capture_error = "no_frame"
            return await self.async_step_capture_ready()
        if len(candidates) > 1:
            self._capture_error = "multiple_frames"
            return await self.async_step_capture_ready()
        self._channel = candidates[0]
        return await self.async_step_device()

    # -- mode B: virtual transmitter ------------------------------------
    async def async_step_virtual_protocol(self, user_input=None):
        errors = {}
        try:
            channels = await self._api().list_channels()
        except AirSendApiError as err:
            return self.async_abort(reason=err.code)
        if user_input is not None:
            self._protocol = int(user_input["protocol"])
            self._channel = None
            return await self.async_step_virtual_prog()
        options = [(str(c["id"]), describe_protocol(c)) for c in channels]
        options.sort(key=lambda o: o[1].lower())
        return self.async_show_form(
            step_id="virtual_protocol",
            data_schema=vol.Schema({vol.Required("protocol"): _select(options)}),
            errors=errors,
        )

    async def async_step_virtual_prog(self, user_input=None):
        errors = {}
        if user_input is not None:
            api = self._api()
            try:
                if self._channel is None:
                    self._channel = await api.build_channel(self._protocol)
                await api.send_note(self._channel, PROG_NOTE)
            except AirSendApiError as err:
                errors["base"] = err.code
            else:
                return self.async_show_menu(
                    step_id="virtual_confirm",
                    menu_options=["device", "virtual_prog"],
                )
        return self.async_show_form(
            step_id="virtual_prog", data_schema=vol.Schema({}), errors=errors
        )

    # -- device details + save ------------------------------------------
    async def async_step_device(self, user_input=None):
        errors = {}
        if user_input is not None:
            name = user_input["name"].strip()
            if not name or name in self._devices:
                errors["name"] = "name_invalid"
            else:
                self._pending_device = {
                    "name": name,
                    "type": int(user_input["type"]),
                    "invert": bool(user_input.get("invert", False)),
                    "secure": bool(user_input.get("secure", True)),
                }
                if self._pending_device["secure"]:
                    return await self.async_step_seal()
                return await self._async_store_device(secrets_sealed=False)
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required("name"): str,
                    vol.Required("type", default="4098"): _select(
                        [(k, v) for k, v in DEVICE_TYPES.items()], "device_type"
                    ),
                    vol.Required("invert", default=False): bool,
                    vol.Required("secure", default=True): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_seal(self, user_input=None):
        """Create the vault or unlock it, then seal the new device's secrets."""
        errors = {}
        vault = get_vault_manager(self.hass)
        await vault.async_load()
        creating = not vault.has_vault
        if user_input is not None:
            pin = user_input["pin"]
            try:
                if creating:
                    if pin != user_input.get("pin_confirm"):
                        errors["base"] = "pin_mismatch"
                    else:
                        await vault.async_create(pin)
                if not errors:
                    await vault.async_seal(
                        pin,
                        {
                            self._pending_device["name"]: {
                                "channel": self._channel,
                                "spurl": self._spurl,
                            }
                        },
                    )
                    return await self._async_store_device(secrets_sealed=True)
            except WeakPin:
                errors["base"] = "weak_pin"
            except InvalidPin:
                errors["base"] = "invalid_pin"
            except PinLocked:
                errors["base"] = "pin_locked"
        schema = {vol.Required("pin"): _PIN}
        if creating:
            schema[vol.Required("pin_confirm")] = _PIN
        return self.async_show_form(
            step_id="seal",
            data_schema=vol.Schema(schema),
            description_placeholders={"device": self._pending_device["name"]},
            errors=errors,
        )

    async def _async_store_device(self, secrets_sealed: bool):
        pending = self._pending_device
        options: dict = {"type": pending["type"], "native": True}
        if pending["invert"] and pending["type"] in (4098, 4099):
            options["invert"] = True
        if secrets_sealed:
            options["secure"] = True
        else:
            options["channel"] = self._channel
            options["spurl"] = self._spurl
        devices = self._devices
        devices[pending["name"]] = options
        self._channel = None
        self._spurl = ""
        return await self._async_save_devices(devices)

    # -- remove ----------------------------------------------------------
    async def async_step_remove_device(self, user_input=None):
        removable = [
            n for n, o in self._devices.items()
            if isinstance(o, dict) and (o.get("native") or o.get("secure"))
        ]
        if not removable:
            return self.async_abort(reason="nothing_to_remove")
        if user_input is not None:
            name = user_input["name"]
            devices = self._devices
            devices.pop(name, None)
            await get_vault_manager(self.hass).async_remove_device(name)
            return await self._async_save_devices(devices)
        return self.async_show_form(
            step_id="remove_device",
            data_schema=vol.Schema(
                {vol.Required("name"): _select([(n, n) for n in removable])}
            ),
        )

    # -- change PIN --------------------------------------------------------
    async def async_step_change_pin(self, user_input=None):
        errors = {}
        vault = get_vault_manager(self.hass)
        if user_input is not None:
            if user_input["new_pin"] != user_input["new_pin_confirm"]:
                errors["base"] = "pin_mismatch"
            else:
                try:
                    await vault.async_change_pin(user_input["old_pin"], user_input["new_pin"])
                    return self.async_create_entry(data={})
                except WeakPin:
                    errors["base"] = "weak_pin"
                except InvalidPin:
                    errors["base"] = "invalid_pin"
                except PinLocked:
                    errors["base"] = "pin_locked"
        return self.async_show_form(
            step_id="change_pin",
            data_schema=vol.Schema(
                {
                    vol.Required("old_pin"): _PIN,
                    vol.Required("new_pin"): _PIN,
                    vol.Required("new_pin_confirm"): _PIN,
                }
            ),
            errors=errors,
        )

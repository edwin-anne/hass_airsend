"""Options flow: native pairing wizard, PIN change and device removal."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import traceback

import voluptuous as vol

from aiohttp import web

from homeassistant import config_entries
from homeassistant.core import callback
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
    describe_candidate,
    normalize_protocol,
)
from .vault import InvalidPin, PinLocked, WeakPin
from .vault_store import get_vault_manager

_LOGGER = logging.getLogger(DOMAIN)

_PIN = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
DEVICE_TYPES = {"4098": "cover", "4097": "switch", "4096": "button", "4100": "light"}
CAPTURE_WINDOW_SECONDS = 60


def _select(options: list[tuple[str, str]], translation_key: str | None = None):
    config: dict = {
        "options": [SelectOptionDict(value=v, label=l) for v, l in options],
        "mode": SelectSelectorMode.DROPDOWN,
    }
    # HA's schema requires a string here: the key must be absent, never None.
    if translation_key:
        config["translation_key"] = translation_key
    return SelectSelector(SelectSelectorConfig(**config))


class AirSendOptionsFlow(config_entries.OptionsFlow):
    """Manage native AirSend devices and the vault from the UI."""

    def __init__(self) -> None:
        self._spurl = ""
        self._mode = "capture"
        self._channel: dict | None = None
        self._model: dict | None = None
        self._task: asyncio.Task | None = None
        self._pending_device: dict | None = None
        self._capture_error: str | None = None
        self._capture_api: AirSendApi | None = None
        self._candidates: list[dict] = []
        self._candidate_labels: dict[str, str] = {}
        self._diagnostic = ""
        self._webhook_id: str | None = None

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

    @callback
    def async_remove(self) -> None:
        """Flow closed: stop listening and drop the temporary webhook."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._unregister_webhook()

    def _unregister_webhook(self) -> None:
        if self._webhook_id:
            from homeassistant.components import webhook

            webhook.async_unregister(self.hass, self._webhook_id)
            self._webhook_id = None

    def _register_webhook(self, inbox: list) -> str:
        """Register a temporary webhook used as the bind callback.

        Returns the callback URL, or the dummy callback when no internal URL is
        available. The webhook id is random and removed when the capture ends.
        """
        from homeassistant.components import webhook
        from homeassistant.helpers.network import NoURLAvailableError, get_url

        async def _handle(hass, webhook_id, request):
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                return web.Response(status=400)
            events = body.get("events") if isinstance(body, dict) else None
            if isinstance(events, list):
                inbox.extend(e for e in events if isinstance(e, dict))
            return web.Response(status=200)

        try:
            base = get_url(self.hass, allow_external=False, allow_cloud=False)
        except NoURLAvailableError:
            return "http://127.0.0.1/"
        webhook_id = secrets.token_hex(16)
        webhook.async_register(
            self.hass, DOMAIN, "AirSend pairing", webhook_id, _handle, local_only=True
        )
        self._webhook_id = webhook_id
        return base.rstrip("/") + webhook.async_generate_path(webhook_id)

    async def _async_rebind_box(self) -> None:
        """Our bind/unbind may have replaced the box listener: restore it."""
        data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})
        for coordinator in data.get("coordinators", {}).values():
            try:
                if coordinator.device.is_airsend:
                    await coordinator.async_request_refresh()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Could not refresh box listener", exc_info=True)

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
            except Exception as err:  # noqa: BLE001
                errors["base"] = self._unexpected(err)
            else:
                return await self.async_step_capture_ready()
        return self.async_show_form(
            step_id="pair",
            description_placeholders={"diagnostic": self._diagnostic},
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
    async def async_step_capture_ready_model(self, user_input=None):
        return await self.async_step_capture_ready(user_input)

    async def async_step_capture_ready(self, user_input=None):
        if user_input is not None:
            return await self.async_step_capture()
        return self.async_show_form(
            step_id="capture_ready_model" if self._mode == "virtual" else "capture_ready",
            data_schema=vol.Schema({}),
            description_placeholders={
                "seconds": str(CAPTURE_WINDOW_SECONDS),
                "diagnostic": self._diagnostic,
            },
            errors={"base": self._capture_error} if self._capture_error else {},
        )

    async def async_step_capture(self, user_input=None):
        if self._task is None:
            self._capture_error = None
            self._diagnostic = ""
            inbox: list = []
            callback_url = self._register_webhook(inbox)
            self._capture_api = self._api()
            self._task = self.hass.async_create_task(
                self._capture_api.capture(
                    window=CAPTURE_WINDOW_SECONDS, callback=callback_url, inbox=inbox
                )
            )
        if not self._task.done():
            return self.async_show_progress(
                step_id="capture", progress_action="listen", progress_task=self._task
            )
        return self.async_show_progress_done(next_step_id="capture_done")

    def _unexpected(self, err: Exception) -> str:
        """Log an unexpected error; keep a diagnostic independent of translations.

        The diagnostic holds the exception name/message and where it happened
        (file:line in this integration only). A persistent notification shows
        the same text in the UI. No channel, spurl or PIN is ever included.
        """
        _LOGGER.exception("AirSend pairing step failed")
        package_dir = os.path.dirname(os.path.abspath(__file__))
        frames = [
            f for f in traceback.extract_tb(err.__traceback__)
            if os.path.abspath(f.filename).startswith(package_dir)
        ]
        where = ""
        if frames:
            last = frames[-1]
            where = f" @ {os.path.basename(last.filename)}:{last.lineno} in {last.name}"
        self._diagnostic = f"{type(err).__name__}: {err}{where}"[:300]
        trace = "\n".join(
            f"- {os.path.basename(f.filename)}:{f.lineno} in {f.name}" for f in frames
        )
        try:
            from homeassistant.components import persistent_notification

            persistent_notification.async_create(
                self.hass,
                f"{self._diagnostic}\n\n{trace}\n\n"
                "Please include this text in your bug report.",
                title="AirSend pairing error",
                notification_id="airsend_pairing_error",
            )
        except Exception:  # noqa: BLE001 - diagnostics must never raise
            _LOGGER.debug("Could not create the pairing error notification", exc_info=True)
        return "unknown"

    async def _async_resume_after_capture(self):
        """Idempotent re-entry: the frontend may call capture_done more than once."""
        if self._candidates:
            return await self.async_step_capture_choose()
        if self._model is not None:
            return await self.async_step_virtual_prog()
        if self._channel is not None:
            return await self.async_step_device()
        return await self.async_step_capture_ready()

    async def async_step_capture_done(self, user_input=None):
        if self._task is None:
            return await self._async_resume_after_capture()
        task, self._task = self._task, None
        try:
            self._unregister_webhook()
            await self._async_rebind_box()
            self._diagnostic = self._capture_api.diagnostic if self._capture_api else ""
            try:
                candidates = task.result()
            except AirSendApiError as err:
                self._capture_error = err.code
                return await self.async_step_capture_ready()
            if not candidates:
                self._capture_error = "no_frame"
                return await self.async_step_capture_ready()
            if self._capture_api and self._capture_api.used_fallback:
                _LOGGER.warning(
                    "AirSend capture used the relaxed frame filter (%s)", self._diagnostic
                )
            if len(candidates) > 1:
                return await self._async_prepare_choice(candidates)
            return await self._async_use_candidate(candidates[0])
        except Exception as err:  # noqa: BLE001 - never a silent HTTP 500
            self._capture_error = self._unexpected(err)
            return await self.async_step_capture_ready()

    async def _async_prepare_choice(self, candidates: list[dict]):
        """Several remotes were heard: let the user pick (best guess first)."""
        names: dict = {}
        try:
            for info in await self._api().list_channels():
                proto = normalize_protocol(info)
                names[proto["id"]] = proto["name"]
        except AirSendApiError:
            pass
        hits = self._capture_api.candidate_hits if self._capture_api else []
        self._candidates = candidates
        self._candidate_labels = {
            str(i): describe_candidate(
                c, hits[i] if i < len(hits) else 1, names.get(c.get("id"))
            )
            for i, c in enumerate(candidates)
        }
        return await self.async_step_capture_choose()

    async def async_step_capture_choose(self, user_input=None):
        if user_input is not None:
            choice = user_input["candidate"]
            if choice == "retry":
                self._candidates = []
                return await self.async_step_capture_ready()
            try:
                channel = self._candidates[int(choice)]
            except (ValueError, IndexError):
                return await self.async_step_capture_ready()
            self._candidates = []
            return await self._async_use_candidate(channel)
        options = list(self._candidate_labels.items()) + [("retry", "Recommencer / Retry")]
        return self.async_show_form(
            step_id="capture_choose",
            data_schema=vol.Schema(
                {
                    vol.Required("candidate", default="0"): _select(options)
                }
            ),
        )

    async def _async_use_candidate(self, channel: dict):
        if self._mode == "virtual":
            # The captured remote is only a MODEL for /channels/build: never stored.
            self._model = channel
            self._channel = None
            info = await self._api().protocol_info(self._model["id"])
            if info is not None and info["cloneable"] is False:
                self._model = None
                return self.async_abort(reason="not_cloneable")
            return await self.async_step_virtual_prog()
        self._channel = channel
        return await self.async_step_device()

    # -- mode B: virtual transmitter ------------------------------------
    async def async_step_virtual_prog(self, user_input=None):
        errors = {}
        if user_input is not None:
            api = self._api()
            try:
                if self._channel is None:
                    self._channel = await api.build_channel(self._model)
                    self._model = None  # the model remote is discarded
                await api.send_note(self._channel, PROG_NOTE)
            except AirSendApiError as err:
                errors["base"] = err.code
            except Exception as err:  # noqa: BLE001
                errors["base"] = self._unexpected(err)
            else:
                return self.async_show_menu(
                    step_id="virtual_confirm",
                    menu_options=["device", "virtual_prog"],
                )
        return self.async_show_form(
            step_id="virtual_prog",
            data_schema=vol.Schema({}),
            description_placeholders={"diagnostic": self._diagnostic},
            errors=errors,
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
            except Exception as err:  # noqa: BLE001
                errors["base"] = self._unexpected(err)
        schema = {vol.Required("pin"): _PIN}
        if creating:
            schema[vol.Required("pin_confirm")] = _PIN
        return self.async_show_form(
            step_id="seal",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "device": self._pending_device["name"],
                "diagnostic": self._diagnostic,
            },
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

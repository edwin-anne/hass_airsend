"""Client for the AirSendWebService endpoints used by the pairing wizard.

Source of truth: ``doc/AirSendWebService.yaml`` shipped with AirSendWebService.
No Home Assistant import here, so it can be tested with a fake server.

Unverified on real hardware (see README): the exact body accepted by
``POST /channels/build`` and the exact shape of ``GET /airsend/events`` items.
The parsing below is deliberately tolerant and never logs secret values.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

CHANNEL_KEYS = ("id", "source", "counter", "mac", "token", "seed")
GOT_TYPES = (3, "GOT")
RELIABILITY_MIN = 0x6  # exclusive, same window as the add-on / Jeedom plugins
RELIABILITY_MAX = 0x47  # exclusive
PROG_NOTE = {"method": 1, "type": 0, "value": "PROG"}
UNPROG_NOTE = {"method": 1, "type": 0, "value": "UNPROG"}


class AirSendApiError(Exception):
    """The web service answered with an error or is unreachable."""

    def __init__(self, code: str, status: int | None = None) -> None:
        super().__init__(code if status is None else f"{code} ({status})")
        self.code = code
        self.status = status


def normalize_channel(channel: dict) -> dict:
    """Keep only the documented Channel fields."""
    return {k: channel[k] for k in CHANNEL_KEYS if channel.get(k) is not None}


def describe_protocol(info: dict) -> str:
    """Human label for a ChannelInfo item."""
    parts = [str(info.get("name") or info.get("id"))]
    if info.get("band"):
        parts.append(f"{info['band']} MHz")
    return " - ".join(parts)


def pick_candidates(events: list[dict]) -> list[dict]:
    """Return the distinct channels seen in valid received-frame (GOT) events."""
    found: dict[tuple, dict] = {}
    for event in events or []:
        if not isinstance(event, dict) or event.get("type") not in GOT_TYPES:
            continue
        channel = event.get("channel")
        if not isinstance(channel, dict) or "id" not in channel:
            continue
        reliability = event.get("reliability", channel.get("reliability"))
        if isinstance(reliability, (int, float)) and not (
            RELIABILITY_MIN < reliability < RELIABILITY_MAX
        ):
            continue
        clean = normalize_channel(channel)
        key = tuple(clean.get(k) for k in ("id", "source", "mac", "seed"))
        found.setdefault(key, clean)
    return list(found.values())


class AirSendApi:
    """Thin async client (one instance per wizard run)."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str, spurl: str | None = None) -> None:
        self._session = session
        self._base = base_url if base_url.endswith("/") else base_url + "/"
        self._spurl = spurl
        self._ssl = False if self._base.startswith("https://") else None

    async def _request(self, method: str, path: str, body: Any = None, auth: bool = True,
                       timeout: float = 8) -> Any:
        headers = {"User-Agent": "hass_airsend"}
        if auth and self._spurl:
            headers["Authorization"] = "Bearer " + self._spurl
        try:
            async with self._session.request(
                method,
                self._base + path,
                json=body,
                headers=headers,
                ssl=self._ssl,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status == 401:
                    raise AirSendApiError("invalid_spurl", 401)
                if response.status == 405:
                    raise AirSendApiError("invalid_input", 405)
                if response.status != 200:
                    raise AirSendApiError("server_error", response.status)
                try:
                    return await response.json(content_type=None)
                except Exception:  # empty / non-JSON body is fine for some calls
                    return None
        except asyncio.TimeoutError:
            raise AirSendApiError("timeout") from None
        except aiohttp.ClientError:
            raise AirSendApiError("cannot_connect") from None

    async def status(self) -> Any:
        return await self._request("GET", "service/status", auth=False)

    async def list_channels(self) -> list[dict]:
        data = await self._request("GET", "channels", auth=False)
        if not isinstance(data, list):
            raise AirSendApiError("bad_response")
        return [c for c in data if isinstance(c, dict) and "id" in c]

    async def build_channel(self, channel_id: int) -> dict:
        """Create a new (virtual transmitter) channel from a protocol id."""
        data = await self._request("POST", "channels/build", {"id": int(channel_id)}, auth=False)
        if not isinstance(data, dict) or "id" not in data:
            raise AirSendApiError("bad_response")
        channel = normalize_channel(data)
        if "source" not in channel:
            raise AirSendApiError("bad_response")
        return channel

    async def bind(self) -> None:
        """Listen indefinitely; the caller bounds the window and calls unbind()."""
        await self._request("POST", "airsend/bind", {"duration": 0, "callback": None})

    async def unbind(self) -> None:
        await self._request("GET", "airsend/unbind")

    async def events(self) -> list[dict]:
        data = await self._request("GET", "airsend/events")
        return data if isinstance(data, list) else []

    async def send_note(self, channel: dict, note: dict) -> None:
        await self._request(
            "POST",
            "airsend/transfer",
            {"wait": True, "channel": channel, "thingnotes": {"notes": [note]}},
            timeout=12,
        )

    async def capture(self, window: float = 60, settle: float = 2.0, poll: float = 1.0) -> list[dict]:
        """Listen for a remote for ``window`` seconds and return distinct channels.

        Stops ``settle`` seconds after the first valid frame so a single button
        press is enough. Always unbinds, even on error.
        """
        await self.bind()
        deadline = time.monotonic() + window
        first_seen: float | None = None
        candidates: list[dict] = []
        try:
            while time.monotonic() < deadline:
                candidates = pick_candidates(await self.events())
                if candidates and first_seen is None:
                    first_seen = time.monotonic()
                if first_seen is not None and time.monotonic() - first_seen >= settle:
                    break
                await asyncio.sleep(poll)
        finally:
            try:
                await self.unbind()
            except AirSendApiError:
                _LOGGER.debug("unbind failed after capture")
        return candidates

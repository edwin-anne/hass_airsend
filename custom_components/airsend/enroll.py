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


def normalize_protocol(info: dict) -> dict:
    """Normalize a ChannelInfo item across both API generations.

    Old: ``counter`` (0 none/1 need/2 required/3 conditional) and ``clone``
    (0 none/1 unavailable/2 temporary/3 full). New: ``hasCounter``,
    ``isCopyable``, ``isCloneable`` booleans. ``None`` means "not reported".
    """
    rolling = info.get("hasCounter")
    if rolling is None and isinstance(info.get("counter"), int):
        rolling = info["counter"] > 0
    cloneable = info.get("isCloneable")
    if cloneable is None and isinstance(info.get("clone"), int):
        cloneable = info["clone"] >= 2
    return {
        "id": info.get("id"),
        "name": info.get("name"),
        "band": info.get("band"),
        "rolling": rolling,
        "copyable": info.get("isCopyable"),
        "cloneable": cloneable,
    }


def describe_protocol(info: dict) -> str:
    """Human label for a ChannelInfo item."""
    proto = normalize_protocol(info)
    parts = [str(proto["name"] or proto["id"])]
    if proto["band"]:
        parts.append(f"{proto['band']} MHz")
    if proto["rolling"]:
        parts.append("rolling code")
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


def pick_loose_candidates(events: list[dict]) -> list[dict]:
    """Fallback: any non-error event carrying a channel id and source.

    Used only when no event passed the strict GOT / reliability filter, so a
    firmware that labels events differently still works. Errors (type >= 256)
    are ignored.
    """
    found: dict[tuple, dict] = {}
    for event in events or []:
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if isinstance(etype, int) and etype >= 0x100:
            continue
        channel = event.get("channel")
        if not isinstance(channel, dict) or "id" not in channel or "source" not in channel:
            continue
        clean = normalize_channel(channel)
        key = tuple(clean.get(k) for k in ("id", "source", "mac", "seed"))
        found.setdefault(key, clean)
    return list(found.values())


def summarize_events(events: list[dict]) -> str:
    """Secret-free description of what the service sent (for error messages/logs)."""
    if not events:
        return "no event received from the service"
    types: dict[str, int] = {}
    ids: set = set()
    reliabilities: set = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        types[str(event.get("type"))] = types.get(str(event.get("type")), 0) + 1
        channel = event.get("channel")
        if isinstance(channel, dict):
            if "id" in channel:
                ids.add(channel["id"])
            rel = event.get("reliability", channel.get("reliability"))
            if rel is not None:
                reliabilities.add(rel)
    return (
        f"{len(events)} event(s); types={dict(sorted(types.items()))}; "
        f"channel ids={sorted(ids, key=str)}; reliability={sorted(reliabilities, key=str)}"
    )


def rank_candidates(events: list[dict], loose: bool = False) -> list[tuple[dict, int, float | None]]:
    """Count frames per distinct channel: [(channel, hits, best_reliability)].

    Sorted most frames first, then best reliability: the remote held next to
    the box is usually the first one. ``loose`` uses the relaxed filter.
    """
    stats: dict[tuple, list] = {}
    for event in events or []:
        found = pick_loose_candidates([event]) if loose else pick_candidates([event])
        if not found:
            continue
        channel = found[0]
        rel = event.get("reliability", (event.get("channel") or {}).get("reliability"))
        rel = rel if isinstance(rel, (int, float)) else None
        key = tuple(channel.get(k) for k in ("id", "source", "mac", "seed"))
        entry = stats.setdefault(key, [channel, 0, None])
        entry[1] += 1
        if rel is not None and (entry[2] is None or rel > entry[2]):
            entry[2] = rel
    ranked = sorted(stats.values(), key=lambda e: (-e[1], -(e[2] if e[2] is not None else -1)))
    return [(c, n, r) for c, n, r in ranked]


def describe_candidate(channel: dict, hits: int, protocol_name: str | None = None) -> str:
    """Label for a heard remote that never exposes its full address."""
    source = str(channel.get("source", ""))
    tail = "..." + source[-4:] if source else "?"
    return f"{protocol_name or channel.get('id')} \u00b7 {tail} \u00b7 x{hits}"


def candidate_key(channel: dict) -> tuple:
    """Identity of a heard transmitter on one protocol decoder."""
    return tuple(channel.get(k) for k in ("id", "source", "mac", "seed"))


def subtract_baseline(
    channels: list[dict], hits: list[int], baseline: set
) -> tuple[list[dict], list[int], bool]:
    """Remove what was already heard during the quiet baseline listening.

    Returns (channels, hits, filtered). Falls back to the full list (and
    ``filtered`` False) when nothing new remains, e.g. a very chatty remote.
    """
    if not baseline:
        return channels, hits, False
    kept = [
        (c, hits[i] if i < len(hits) else 1)
        for i, c in enumerate(channels)
        if candidate_key(c) not in baseline
    ]
    if not kept:
        return channels, hits, False
    return [c for c, _h in kept], [h for _c, h in kept], True


def group_candidates(channels: list[dict], hits: list[int]) -> list[list[tuple[dict, int]]]:
    """Group the protocol-decoder variants of one physical transmitter.

    The service may decode the same radio frame with several variants of a
    protocol (e.g. EV1527, EV1527_1, EV1527_2): same source/mac/seed. Each
    group is [(channel, hits), ...], best variant first; groups keep the
    incoming ranking (most frames first).
    """
    groups: dict[tuple, list] = {}
    for i, channel in enumerate(channels):
        key = (channel.get("source"), channel.get("mac"), channel.get("seed"))
        groups.setdefault(key, []).append((channel, hits[i] if i < len(hits) else 1))
    result = []
    for members in groups.values():
        members.sort(key=lambda m: (-m[1], m[0].get("id") if isinstance(m[0].get("id"), int) else 0))
        result.append(members)
    result.sort(key=lambda g: -g[0][1])
    return result


class AirSendApi:
    """Thin async client (one instance per wizard run)."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str, spurl: str | None = None) -> None:
        self._session = session
        self._base = base_url if base_url.endswith("/") else base_url + "/"
        self._spurl = spurl
        self._ssl = False if self._base.startswith("https://") else None
        self.diagnostic = ""
        self.used_fallback = False
        self.candidate_hits: list[int] = []

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

    async def protocol_info(self, channel_id: int) -> dict | None:
        """Normalized capabilities of a protocol, or None when unknown/unreachable."""
        try:
            for info in await self.list_channels():
                if info.get("id") == channel_id:
                    return normalize_protocol(info)
        except AirSendApiError:
            return None
        return None

    async def build_channel(self, model: dict) -> dict:
        """Create a NEW transmitter channel from a model channel.

        The service documents ``/channels/build`` as "generate and build a new
        channel from another channel": the model is a full Channel (id, source,
        counter/mac/seed). The model itself is never stored by the caller.
        """
        model = normalize_channel(model)
        try:
            data = await self._request("POST", "channels/build", model, auth=False)
        except AirSendApiError as err:
            if err.status != 405 or set(model) == {"id"}:
                raise
            # Some versions may only want the protocol id.
            data = await self._request("POST", "channels/build", {"id": model["id"]}, auth=False)
        if not isinstance(data, dict) or "id" not in data:
            raise AirSendApiError("bad_response")
        channel = normalize_channel(data)
        if "source" not in channel:
            raise AirSendApiError("bad_response")
        if (
            model.get("source") is not None
            and channel.get("source") == model.get("source")
            and channel.get("mac") == model.get("mac")
            and channel.get("seed") == model.get("seed")
        ):
            raise AirSendApiError("build_same_channel")
        return channel

    async def bind(self, callback: str = "http://127.0.0.1/") -> None:
        """Listen indefinitely; the caller bounds the window and calls unbind().

        A callback is always sent (a dummy one by default) like every known
        client of the service does.
        """
        await self._request("POST", "airsend/bind", {"duration": 0, "callback": callback})

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

    async def capture(
        self,
        window: float = 60,
        settle: float = 2.0,
        poll: float = 1.0,
        callback: str = "http://127.0.0.1/",
        inbox: list | None = None,
    ) -> list[dict]:
        """Listen for a remote for ``window`` seconds and return distinct channels.

        Events come from polling ``GET /airsend/events`` and, when given, from
        ``inbox`` (filled by a webhook registered as the bind callback). All
        events are accumulated: the service may drain its buffer on read.
        Stops ``settle`` seconds after the first valid frame. Always unbinds.
        ``self.diagnostic`` describes what was seen.
        """
        self.used_fallback = False
        await self.bind(callback)
        deadline = time.monotonic() + window
        first_seen: float | None = None
        all_events: list[dict] = []
        strict: dict[tuple, dict] = {}
        try:
            while time.monotonic() < deadline:
                try:
                    new_events = await self.events()
                except AirSendApiError as err:
                    if err.code in ("invalid_spurl", "cannot_connect"):
                        raise
                    new_events = []
                if inbox:
                    new_events = new_events + list(inbox)
                    inbox.clear()
                all_events.extend(new_events)
                for cand in pick_candidates(new_events):
                    key = tuple(cand.get(k) for k in ("id", "source", "mac", "seed"))
                    strict.setdefault(key, cand)
                if strict and first_seen is None:
                    first_seen = time.monotonic()
                if first_seen is not None and time.monotonic() - first_seen >= settle:
                    break
                await asyncio.sleep(poll)
        finally:
            try:
                await self.unbind()
            except AirSendApiError:
                _LOGGER.debug("unbind failed after capture")
        self.diagnostic = summarize_events(all_events)
        _LOGGER.debug("AirSend capture finished: %s", self.diagnostic)
        ranked = rank_candidates(all_events)
        if not ranked:
            ranked = rank_candidates(all_events, loose=True)
            if ranked:
                self.used_fallback = True
                _LOGGER.debug("AirSend capture: no strict GOT frame, using %d loose candidate(s)", len(ranked))
        self.candidate_hits = [hits for _c, hits, _r in ranked]
        return [channel for channel, _hits, _r in ranked]

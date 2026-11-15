"""Tests for the AirSendWebService client, against a fake local server."""
import asyncio
import importlib.util
import pathlib

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

_PATH = pathlib.Path(__file__).parents[1] / "custom_components" / "airsend" / "enroll.py"
_spec = importlib.util.spec_from_file_location("airsend_enroll", _PATH)
enroll = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(enroll)

SPURL = "sp://pw@[fe80::1]?gw=0&rhost=10.0.0.2"


def run(coro):
    return asyncio.run(coro)


def make_app(state):
    app = web.Application()

    def need_auth(request):
        if request.headers.get("Authorization") != "Bearer " + SPURL:
            raise web.HTTPUnauthorized()

    async def status(_):
        return web.json_response({"version": 0.22})

    async def channels(_):
        return web.json_response([{"id": 808, "name": "EV1527", "band": 433, "counter": 0, "clone": 3}])

    async def build(request):
        body = await request.json()
        state.setdefault("build_bodies", []).append(body)
        state["build_body"] = body
        if state.get("build_bad"):
            return web.json_response({"nope": 1})
        if state.get("build_needs_id_only") and set(body) != {"id"}:
            raise web.HTTPMethodNotAllowed("POST", ["GET"])
        if state.get("build_echo"):
            return web.json_response(body)
        return web.json_response({"id": body["id"], "source": 5551234, "counter": 1, "mac": 9, "seed": 4, "x": "dropped"})

    async def bind(request):
        need_auth(request)
        state["bound"] = await request.json()
        return web.json_response({})

    async def unbind(request):
        need_auth(request)
        state["unbound"] = True
        return web.Response(text="")

    async def events(request):
        need_auth(request)
        state["polls"] = state.get("polls", 0) + 1
        if "queue" in state:  # draining buffer: each batch is returned only once
            batch = state["queue"].pop(0) if state["queue"] else []
            return web.json_response(batch)
        return web.json_response(state.get("events", []))

    async def transfer(request):
        need_auth(request)
        state["transfer"] = await request.json()
        return web.json_response({"type": 1})

    app.router.add_get("/service/status", status)
    app.router.add_get("/channels", channels)
    app.router.add_post("/channels/build", build)
    app.router.add_post("/airsend/bind", bind)
    app.router.add_get("/airsend/unbind", unbind)
    app.router.add_get("/airsend/events", events)
    app.router.add_post("/airsend/transfer", transfer)
    return app


async def with_api(state, fn, spurl=SPURL):
    async with TestServer(make_app(state)) as server:
        async with aiohttp.ClientSession() as session:
            api = enroll.AirSendApi(session, str(server.make_url("/")), spurl)
            return await fn(api)


def test_list_and_build():
    state = {}

    async def go(api):
        chans = await api.list_channels()
        built = await api.build_channel({"id": 808, "source": 77, "counter": 5, "seed": 3, "junk": 1})
        return chans, built

    chans, built = run(with_api(state, go))
    assert chans[0]["name"] == "EV1527"
    # the whole model channel (documented fields only) is sent to the service
    assert state["build_body"] == {"id": 808, "source": 77, "counter": 5, "seed": 3}
    assert built == {"id": 808, "source": 5551234, "counter": 1, "mac": 9, "seed": 4}


def test_build_bad_response():
    async def go(api):
        with pytest.raises(enroll.AirSendApiError):
            await api.build_channel({"id": 808, "source": 1})

    run(with_api({"build_bad": True}, go))


def test_wrong_spurl_is_401():
    async def go(api):
        with pytest.raises(enroll.AirSendApiError) as exc:
            await api.events()
        assert exc.value.code == "invalid_spurl"

    run(with_api({}, go, spurl="sp://wrong"))


def test_capture_stops_after_first_frame_and_unbinds():
    state = {"events": [
        {"type": 3, "reliability": 20, "channel": {"id": 808, "source": 77, "mac": 1}},
        {"type": 3, "reliability": 20, "channel": {"id": 808, "source": 77, "mac": 1}},  # duplicate
        {"type": 0, "channel": {"id": 1, "source": 1}},  # not a GOT
    ]}

    async def go(api):
        return await api.capture(window=5, settle=0.2, poll=0.05)

    result = run(with_api(state, go))
    assert result == [{"id": 808, "source": 77, "mac": 1}]
    assert state["bound"]["duration"] == 0
    assert state["unbound"] is True


def test_capture_timeout_returns_empty_and_unbinds():
    state = {"events": []}

    async def go(api):
        return await api.capture(window=0.3, settle=0.1, poll=0.05)

    assert run(with_api(state, go)) == []
    assert state["unbound"] is True


def test_pick_candidates_filters():
    ev = [
        {"type": "GOT", "reliability": 0x6, "channel": {"id": 1, "source": 1}},   # boundary excluded
        {"type": "GOT", "reliability": 0x47, "channel": {"id": 1, "source": 2}},  # boundary excluded
        {"type": "GOT", "channel": {"id": 1, "source": 3, "reliability": 30}},    # reliability inside channel
        {"type": "GOT", "channel": {"id": 1, "source": 4}},                      # no reliability info: kept
        {"type": "GOT", "channel": {"source": 5}},                               # no id
        "garbage",
    ]
    assert [c["source"] for c in enroll.pick_candidates(ev)] == [3, 4]


def test_send_prog_payload():
    state = {}

    async def go(api):
        await api.send_note({"id": 808, "source": 5}, enroll.PROG_NOTE)

    run(with_api(state, go))
    assert state["transfer"] == {
        "wait": True, "channel": {"id": 808, "source": 5},
        "thingnotes": {"notes": [{"method": 1, "type": 0, "value": "PROG"}]},
    }


def test_capture_keeps_frame_when_service_drains_its_buffer():
    frame = {"type": 3, "reliability": 20, "channel": {"id": 808, "source": 77}}
    state = {"queue": [[frame], [], [], [], []]}

    async def go(api):
        return await api.capture(window=2, settle=0.2, poll=0.05)

    assert run(with_api(state, go)) == [{"id": 808, "source": 77}]


def test_bind_sends_a_callback():
    state = {"events": []}

    async def go(api):
        await api.capture(window=0.1, settle=0.1, poll=0.05)

    run(with_api(state, go))
    assert state["bound"]["callback"] == "http://127.0.0.1/"


def test_bind_uses_custom_callback_and_inbox_events():
    state = {"events": []}
    inbox = [{"type": 3, "reliability": 20, "channel": {"id": 9, "source": 5}}]

    async def go(api):
        return await api.capture(window=2, settle=0.1, poll=0.05,
                                 callback="http://ha:8123/api/webhook/x", inbox=inbox)

    assert run(with_api(state, go)) == [{"id": 9, "source": 5}]
    assert state["bound"]["callback"] == "http://ha:8123/api/webhook/x"
    assert inbox == []


def test_loose_fallback_and_diagnostic():
    # unknown type label and out-of-range reliability: strict filter rejects it
    state = {"events": [{"type": 99, "reliability": 200, "channel": {"id": 808, "source": 7}}]}
    holder = {}

    async def go(api):
        result = await api.capture(window=0.4, settle=0.1, poll=0.05)
        holder["api"] = api
        return result

    assert run(with_api(state, go)) == [{"id": 808, "source": 7}]
    assert holder["api"].used_fallback is True
    assert "event(s)" in holder["api"].diagnostic and "99" in holder["api"].diagnostic


def test_diagnostic_without_events_and_without_secrets():
    state = {"events": []}
    holder = {}

    async def go(api):
        assert await api.capture(window=0.2, settle=0.1, poll=0.05) == []
        holder["d"] = api.diagnostic

    run(with_api(state, go))
    assert holder["d"] == "no event received from the service"
    d = enroll.summarize_events([{"type": 3, "reliability": 20, "channel": {"id": 808, "source": 987654}}])
    assert "987654" not in d and "808" in d


def test_errors_are_ignored_by_loose_fallback():
    assert enroll.pick_loose_candidates([{"type": 0x105, "channel": {"id": 1, "source": 2}}]) == []


def test_build_same_channel_is_rejected():
    async def go(api):
        with pytest.raises(enroll.AirSendApiError) as exc:
            await api.build_channel({"id": 808, "source": 5551234, "mac": 9, "seed": 4, "counter": 1})
        assert exc.value.code == "build_same_channel"

    run(with_api({"build_echo": True}, go))


def test_build_falls_back_to_id_only_on_405():
    state = {"build_needs_id_only": True}

    async def go(api):
        return await api.build_channel({"id": 808, "source": 77})

    built = run(with_api(state, go))
    assert state["build_bodies"] == [{"id": 808, "source": 77}, {"id": 808}]
    assert built["source"] == 5551234


def test_normalize_protocol_both_api_generations():
    new = enroll.normalize_protocol({"id": 1, "name": "A", "band": 433, "hasCounter": True,
                                     "isCopyable": False, "isCloneable": True})
    assert (new["rolling"], new["copyable"], new["cloneable"]) == (True, False, True)
    old = enroll.normalize_protocol({"id": 2, "counter": 2, "clone": 1})
    assert (old["rolling"], old["cloneable"], old["copyable"]) == (True, False, None)
    old_full = enroll.normalize_protocol({"id": 3, "counter": 0, "clone": 3})
    assert (old_full["rolling"], old_full["cloneable"]) == (False, True)
    unknown = enroll.normalize_protocol({"id": 4})
    assert unknown["rolling"] is None and unknown["cloneable"] is None
    assert "rolling code" in enroll.describe_protocol({"id": 1, "name": "A", "band": 868, "hasCounter": True})


def test_protocol_info_lookup():
    async def go(api):
        assert (await api.protocol_info(808))["cloneable"] is True
        assert await api.protocol_info(999) is None

    run(with_api({}, go))


def _got(source, rel=20, id_=808):
    return {"type": 3, "reliability": rel, "channel": {"id": id_, "source": source}}


def test_rank_candidates_orders_by_frames_then_reliability():
    events = [_got(111, 10), _got(222, 30), _got(222, 25), _got(222, 28), _got(333, 40), _got(333, 12)]
    ranked = enroll.rank_candidates(events)
    assert [c["source"] for c, _h, _r in ranked] == [222, 333, 111]
    assert [h for _c, h, _r in ranked] == [3, 2, 1]
    assert ranked[0][2] == 30 and ranked[1][2] == 40


def test_capture_returns_sorted_candidates_and_hits():
    state = {"events": [_got(111, 10), _got(222, 30), _got(222, 25)]}
    holder = {}

    async def go(api):
        result = await api.capture(window=1.5, settle=0.2, poll=0.05)
        holder["hits"] = api.candidate_hits
        return result

    result = run(with_api(state, go))
    assert [c["source"] for c in result][0] == 222
    assert holder["hits"][0] >= holder["hits"][1]
    assert len(result) == 2


def test_describe_candidate_masks_address():
    label = enroll.describe_candidate({"id": 808, "source": 987654321}, 3, "EV1527")
    assert "987654321" not in label and "4321" in label and "x3" in label and "EV1527" in label
    assert enroll.describe_candidate({"id": 5}, 1) .startswith("5 ")

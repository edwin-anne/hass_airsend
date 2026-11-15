"""Options-flow robustness tests using minimal Home Assistant stubs.

Home Assistant is not installed in the test environment, so the few HA names
used by options_flow.py are stubbed. This checks our own step logic only.
"""
import asyncio
import importlib
import pathlib
import sys
import types
from unittest.mock import MagicMock

import pytest

PKG_DIR = pathlib.Path(__file__).parents[1] / "custom_components" / "airsend"


class _Flow:
    """Stand-in for homeassistant.config_entries.OptionsFlow."""

    def async_show_form(self, **kw):
        return {"type": "form", **kw}

    def async_show_menu(self, **kw):
        return {"type": "menu", **kw}

    def async_show_progress(self, **kw):
        return {"type": "progress", **kw}

    def async_show_progress_done(self, **kw):
        return {"type": "progress_done", **kw}

    def async_abort(self, **kw):
        return {"type": "abort", **kw}

    def async_create_entry(self, **kw):
        return {"type": "create_entry", **kw}


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


@pytest.fixture(scope="module")
def of():
    saved = dict(sys.modules)
    _stub("homeassistant")
    _stub("homeassistant.config_entries", OptionsFlow=_Flow)
    _stub("homeassistant.const", CONF_INTERNAL_URL="internal_url")
    _stub("homeassistant.core", callback=lambda f: f, CALLBACK_TYPE=object, HomeAssistant=object)
    _stub("homeassistant.exceptions", ServiceValidationError=Exception)
    _stub("homeassistant.helpers")
    notif = MagicMock()
    _stub("homeassistant.components")
    _stub("homeassistant.components.persistent_notification", async_create=notif)
    sys.modules["homeassistant.components"].persistent_notification = sys.modules[
        "homeassistant.components.persistent_notification"
    ]
    _stub("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: MagicMock())
    _stub("homeassistant.helpers.dispatcher", async_dispatcher_send=MagicMock())
    _stub("homeassistant.helpers.event", async_call_later=MagicMock())
    _stub("homeassistant.helpers.storage", Store=MagicMock())
    sel = lambda *a, **k: MagicMock()  # noqa: E731
    _stub(
        "homeassistant.helpers.selector",
        SelectOptionDict=dict, SelectSelector=sel, SelectSelectorConfig=sel,
        SelectSelectorMode=types.SimpleNamespace(DROPDOWN="dropdown"),
        TextSelector=sel, TextSelectorConfig=sel,
        TextSelectorType=types.SimpleNamespace(PASSWORD="password"),
    )
    pkg = types.ModuleType("airsendpkg")
    pkg.__path__ = [str(PKG_DIR)]
    pkg.DOMAIN = "airsend"
    sys.modules["airsendpkg"] = pkg
    module = importlib.import_module("airsendpkg.options_flow")
    module._test_notification = notif
    yield module
    for key in list(sys.modules):
        if key not in saved:
            del sys.modules[key]


def make_flow(of, mode="capture"):
    flow = of.AirSendOptionsFlow()
    flow.hass = MagicMock()
    flow.config_entry = MagicMock()
    flow.config_entry.data = {"internal_url": "http://x/", "devices": {}}
    flow._mode = mode
    flow._spurl = "sp://x"

    class FakeApi:
        async def list_channels(self):
            return [{"id": 808, "name": "EV1527", "band": 433}]

        async def protocol_info(self, _id):
            return {"cloneable": True}

    flow._api = lambda: FakeApi()
    return flow


def done_task(result=None, exc=None):
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    if exc:
        fut.set_exception(exc)
    else:
        fut.set_result(result)
    # silence "exception never retrieved" for the failing case
    fut.exception() if exc else None
    return fut


def run(coro):
    return asyncio.run(coro)


def test_capture_done_twice_does_not_crash(of):
    flow = make_flow(of)
    flow._task = done_task([{"id": 808, "source": 5}])
    flow._capture_api = MagicMock(diagnostic="", used_fallback=False, candidate_hits=[1])
    first = run(flow.async_step_capture_done())
    assert first["step_id"] == "device"
    # the frontend calls the step a second time: must not raise AttributeError
    second = run(flow.async_step_capture_done())
    assert second["step_id"] == "device"


def test_capture_done_unexpected_exception_is_reported(of):
    flow = make_flow(of)
    flow._task = done_task(exc=RuntimeError("boom"))
    flow._capture_api = MagicMock(diagnostic="", used_fallback=False, candidate_hits=[])
    result = run(flow.async_step_capture_done())
    assert result["type"] == "form" and result["step_id"] == "capture_ready"
    assert result["errors"] == {"base": "unknown"}
    assert "RuntimeError" in result["description_placeholders"]["diagnostic"]


def test_several_candidates_show_choice_and_are_idempotent(of):
    flow = make_flow(of)
    flow._task = done_task([{"id": 808, "source": 111111}, {"id": 808, "source": 222222}])
    flow._capture_api = MagicMock(diagnostic="", used_fallback=False, candidate_hits=[3, 1])
    first = run(flow.async_step_capture_done())
    assert first["step_id"] == "capture_choose"
    assert run(flow.async_step_capture_done())["step_id"] == "capture_choose"
    labels = " ".join(flow._candidate_labels.values())
    assert "111111" not in labels and "1111" in labels and "EV1527" in labels


def test_choose_candidate_then_device_and_retry(of):
    flow = make_flow(of)
    flow._candidates = [{"id": 808, "source": 1}, {"id": 808, "source": 2}]
    nxt = run(flow.async_step_capture_choose({"candidate": "1"}))
    assert nxt["step_id"] == "device" and flow._channel == {"id": 808, "source": 2}
    flow2 = make_flow(of)
    flow2._candidates = [{"id": 808, "source": 1}]
    assert run(flow2.async_step_capture_choose({"candidate": "retry"}))["step_id"] == "capture_ready"
    flow3 = make_flow(of)
    flow3._candidates = [{"id": 808, "source": 1}]
    assert run(flow3.async_step_capture_choose({"candidate": "9"}))["step_id"] == "capture_ready"


def test_virtual_mode_uses_candidate_as_model(of):
    flow = make_flow(of, mode="virtual")
    result = run(flow._async_use_candidate({"id": 808, "source": 7}))
    assert result["step_id"] == "virtual_prog"
    assert flow._model == {"id": 808, "source": 7} and flow._channel is None


def test_unexpected_error_gives_location_and_notification(of, monkeypatch):
    def boom(*_a, **_k):
        raise ValueError("kaboom")

    monkeypatch.setattr(of, "describe_candidate", boom)
    of._test_notification.reset_mock()
    flow = make_flow(of)
    flow._task = done_task([{"id": 808, "source": 111111}, {"id": 808, "source": 222222}])
    flow._capture_api = MagicMock(diagnostic="", used_fallback=False, candidate_hits=[1, 1])
    result = run(flow.async_step_capture_done())
    assert result["step_id"] == "capture_ready" and result["errors"] == {"base": "unknown"}
    diag = result["description_placeholders"]["diagnostic"]
    assert "ValueError: kaboom" in diag and "options_flow.py:" in diag and "_async_prepare_choice" in diag
    assert of._test_notification.call_count == 1
    text = of._test_notification.call_args.args[1]
    assert "kaboom" in text and "111111" not in text and "sp://" not in text

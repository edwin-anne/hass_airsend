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
    def sel(*_a, **kw):
        # Mirror HA's schema: translation_key must be a string when present.
        if "translation_key" in kw and not isinstance(kw["translation_key"], str):
            raise ValueError("string value is None at 'translation_key'")
        return MagicMock()

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
    flow._candidates = [[({"id": 808, "source": 1}, 3)], [({"id": 808, "source": 2}, 1)]]
    nxt = run(flow.async_step_capture_choose({"candidate": "1"}))
    assert nxt["step_id"] == "device" and flow._channel == {"id": 808, "source": 2}
    flow2 = make_flow(of)
    flow2._candidates = [[({"id": 808, "source": 1}, 3)]]
    assert run(flow2.async_step_capture_choose({"candidate": "retry"}))["step_id"] == "capture_ready"
    flow3 = make_flow(of)
    flow3._candidates = [[({"id": 808, "source": 1}, 3)]]
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
    assert "ValueError: kaboom" in diag and "options_flow.py:" in diag and "_label" in diag
    assert of._test_notification.call_count == 1
    text = of._test_notification.call_args.args[1]
    assert "kaboom" in text and "111111" not in text and "sp://" not in text


def test_selectors_never_pass_none_translation_key(of):
    of._select([("a", "A")])  # no key
    of._select([("a", "A")], "pair_mode")  # with key


def test_capture_choose_form_builds_with_strict_selector(of):
    flow = make_flow(of)
    flow._candidate_labels = {"0": "EV1527 \u00b7 ...1111 \u00b7 x3", "1": "EV1527 \u00b7 ...2222 \u00b7 x1"}
    form = run(flow.async_step_capture_choose())
    assert form["type"] == "form" and form["step_id"] == "capture_choose"


def test_remove_device_form_builds_with_strict_selector(of):
    flow = make_flow(of)
    flow.config_entry.data = {"internal_url": "x", "devices": {"Garage": {"native": True, "secure": True}}}
    form = run(flow.async_step_remove_device())
    assert form["type"] == "form" and form["step_id"] == "remove_device"


def _capture_flow(of, channels, hits, baseline=None):
    flow = make_flow(of)
    flow._baseline = baseline or set()
    flow._task = done_task(channels)
    flow._capture_api = MagicMock(diagnostic="", used_fallback=False, candidate_hits=hits)
    return flow


def test_baseline_removes_ambient_transmitters(of):
    ambient = {"id": 1, "source": 100}
    mine = {"id": 808, "source": 222}
    flow = _capture_flow(of, [ambient, mine], [6, 2], baseline={(1, 100, None, None)})
    result = run(flow.async_step_capture_done())
    assert result["step_id"] == "device"  # only one new transmitter left: no list
    assert flow._channel == mine


def test_baseline_that_removes_everything_falls_back_to_full_list(of):
    a, b = {"id": 1, "source": 100}, {"id": 2, "source": 200}
    flow = _capture_flow(of, [a, b], [3, 2], baseline={(1, 100, None, None), (2, 200, None, None)})
    result = run(flow.async_step_capture_done())
    assert result["step_id"] == "capture_choose" and len(flow._candidates) == 2


def test_variants_of_one_transmitter_are_grouped(of):
    chans = [{"id": 10, "source": 2391}, {"id": 11, "source": 2391}, {"id": 12, "source": 2391},
             {"id": 20, "source": 7952}]
    flow = _capture_flow(of, chans, [3, 3, 3, 6])
    result = run(flow.async_step_capture_done())
    assert result["step_id"] == "capture_choose"
    assert len(flow._candidates) == 2  # 4 decodings -> 2 physical transmitters
    labels = list(flow._candidate_labels.values())
    assert any("(+2)" in label for label in labels)
    # choosing the group with 3 variants asks which variant
    idx = next(i for i, label in labels and enumerate(labels) if "(+2)" in label)
    nxt = run(flow.async_step_capture_choose({"candidate": str(idx)}))
    assert nxt["step_id"] == "capture_variant"
    assert len(flow._candidate_labels) == 3
    done = run(flow.async_step_capture_variant({"variant": "1"}))
    assert done["step_id"] == "device" and flow._channel["source"] == 2391


def test_single_transmitter_with_variants_goes_straight_to_variant_step(of):
    chans = [{"id": 10, "source": 5}, {"id": 11, "source": 5}]
    flow = _capture_flow(of, chans, [3, 3])
    assert run(flow.async_step_capture_done())["step_id"] == "capture_variant"
    # re-entry must not crash
    assert run(flow.async_step_capture_done())["step_id"] == "capture_variant"


def test_prepare_menu_and_skip(of):
    flow = make_flow(of)
    menu = run(flow.async_step_capture_prepare())
    assert menu["type"] == "menu" and menu["menu_options"] == ["baseline", "skip_baseline"]
    flow._baseline = {(1, 1, None, None)}
    assert run(flow.async_step_skip_baseline())["step_id"] == "capture_ready"
    assert flow._baseline == set()


def test_baseline_done_collects_keys_and_is_safe_twice(of):
    flow = make_flow(of)
    flow._baseline_task = done_task([{"id": 1, "source": 100}])
    assert run(flow.async_step_baseline_done())["step_id"] == "capture_ready"
    assert flow._baseline == {(1, 100, None, None)}
    assert run(flow.async_step_baseline_done())["step_id"] == "capture_ready"
    flow2 = make_flow(of)
    flow2._baseline_task = done_task(exc=RuntimeError("x"))
    assert run(flow2.async_step_baseline_done())["step_id"] == "capture_ready"
    assert flow2._baseline == set()

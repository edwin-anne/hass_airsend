"""Unit tests for the pure-crypto vault (no Home Assistant required)."""
import importlib.util
import json
import os
import pathlib
import stat

import pytest

_PATH = pathlib.Path(__file__).parents[1] / "custom_components" / "airsend" / "vault.py"
_spec = importlib.util.spec_from_file_location("airsend_vault", _PATH)
vault = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vault)

PIN = "482916"
PEPPER = os.urandom(32)
SECRETS = {"channel": {"id": 4242, "source": 987654321, "mac": 7, "seed": 9},
           "spurl": "sp://pass@[fe80::1]?gw=0&rhost=10.0.0.2", "apiKey": "k"}


def make(pin=PIN, pepper=PEPPER):
    return vault.Vault.create(pin, pepper, n=2**10)  # tiny n: fast tests


def test_roundtrip():
    v = make()
    dek = v.unlock(PIN)
    v.seal_device(dek, "garage", SECRETS)
    assert v.open_device(v.unlock(PIN), "garage") == SECRETS


def test_no_plaintext_in_serialized_blob():
    v = make()
    v.seal_device(v.unlock(PIN), "garage", SECRETS)
    blob = json.dumps(v.to_dict())
    for needle in ("987654321", "fe80", "pass@", "4242"):
        assert needle not in blob


def test_wrong_pin_rejected():
    v = make()
    with pytest.raises(vault.InvalidPin):
        v.unlock("111112")


def test_wrong_pepper_rejected():
    v = make()
    data = v.to_dict()
    other = vault.Vault(data, os.urandom(32))
    with pytest.raises(vault.InvalidPin):
        other.unlock(PIN)


def test_persist_and_reload():
    v = make()
    v.seal_device(v.unlock(PIN), "garage", SECRETS)
    v2 = vault.Vault(json.loads(json.dumps(v.to_dict())), PEPPER)
    assert v2.open_device(v2.unlock(PIN), "garage") == SECRETS


def test_ciphertext_bound_to_device_id():
    v = make()
    dek = v.unlock(PIN)
    v.seal_device(dek, "garage", SECRETS)
    v._d["devices"]["portail"] = v._d["devices"]["garage"]
    with pytest.raises(Exception):
        v.open_device(dek, "portail")


def test_lockout_and_reset():
    v = make()
    for _ in range(4):
        with pytest.raises(vault.InvalidPin):
            v.unlock("111112", now=1000)
    assert v.unlock(PIN, now=1000)  # success resets the counter
    for _ in range(5):
        with pytest.raises(vault.InvalidPin):
            v.unlock("111112", now=1000)
    with pytest.raises(vault.PinLocked) as exc:
        v.unlock(PIN, now=1001)  # even the right PIN is refused while locked
    assert 290 < exc.value.retry_after <= 300
    assert v.unlock(PIN, now=1000 + 301)


def test_lockout_escalates():
    v = make()
    now = 0
    for _ in range(5):
        with pytest.raises(vault.InvalidPin):
            v.unlock("111112", now=now)
    now += 301
    for _ in range(5):
        with pytest.raises(vault.InvalidPin):
            v.unlock("111112", now=now)
    with pytest.raises(vault.PinLocked) as exc:
        v.unlock(PIN, now=now)
    assert exc.value.retry_after > 800  # 15 minutes level


def test_lockout_persists_in_dict():
    v = make()
    for _ in range(5):
        with pytest.raises(vault.InvalidPin):
            v.unlock("111112", now=0)
    v2 = vault.Vault(v.to_dict(), PEPPER)
    with pytest.raises(vault.PinLocked):
        v2.unlock(PIN, now=10)


def test_change_pin():
    v = make()
    v.seal_device(v.unlock(PIN), "garage", SECRETS)
    v.change_pin(PIN, "739105")
    with pytest.raises(vault.InvalidPin):
        v.unlock(PIN)
    assert v.open_device(v.unlock("739105"), "garage") == SECRETS


def test_change_pin_requires_old_and_strong_new():
    v = make()
    with pytest.raises(vault.InvalidPin):
        v.change_pin("111112", "739105")
    with pytest.raises(vault.WeakPin):
        v.change_pin(PIN, "123456")


@pytest.mark.parametrize("pin", ["12345", "123456789", "abcdef", "000000", "123456",
                                 "654321", "121212", "123123", "9999999", ""])
def test_weak_pins_rejected(pin):
    with pytest.raises(vault.WeakPin):
        vault.validate_pin(pin)


@pytest.mark.parametrize("pin", ["482916", "73910582", "290417"])
def test_good_pins(pin):
    vault.validate_pin(pin)


def test_pepper_file(tmp_path):
    path = str(tmp_path / "pepper")
    with pytest.raises(vault.PepperMissing):
        vault.load_pepper(path, create=False)
    p1 = vault.load_pepper(path, create=True)
    assert len(p1) == 32 and vault.load_pepper(path, create=False) == p1
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with open(path, "wb") as f:
        f.write(b"short")
    with pytest.raises(vault.PepperMissing):
        vault.load_pepper(path, create=False)


def test_remove_device_and_unknown():
    v = make()
    dek = v.unlock(PIN)
    v.seal_device(dek, "garage", SECRETS)
    v.remove_device("garage")
    with pytest.raises(vault.UnknownDevice):
        v.open_device(dek, "garage")


def test_split_secure_devices():
    devices = {
        "garage": {"type": 4098, "secure": True, "id": 7,
                   "channel": {"id": 1, "source": 2}, "spurl": "sp://x", "apiKey": "k", "invert": True},
        "volet": {"type": 4098, "channel": {"id": 3}, "spurl": "sp://y"},
        "empty": {"type": 4098, "secure": True},
    }
    public, secrets = vault.split_secure_devices(devices)
    assert public["volet"] == devices["volet"]  # untouched
    assert public["garage"] == {"type": 4098, "secure": True, "id": 7, "invert": True}
    assert secrets == {"garage": {"channel": {"id": 1, "source": 2}, "spurl": "sp://x", "apiKey": "k"}}
    assert "empty" in public and "empty" not in secrets
    assert "channel" in devices["garage"]  # input not mutated

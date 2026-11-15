"""Encrypted vault for AirSend secrets.

Pure cryptography, no Home Assistant import, so it can be unit-tested alone.

Design
------
* One random 256-bit data key (DEK) encrypts each device's secrets with
  AES-256-GCM (AAD binds the ciphertext to its device id).
* The DEK is wrapped by a key-encryption key (KEK) derived from
  ``scrypt(PIN, salt)`` mixed through HKDF with a *pepper* kept in a separate
  file.  A leaked vault blob alone (e.g. a backup) is therefore not attackable
  offline by brute-forcing a short numeric PIN; the pepper alone is useless
  without the PIN.
* Failed unlock attempts are counted and persisted; every 5 failures locks the
  vault for an increasing delay (5 min, 15 min, 1 h max).
"""
from __future__ import annotations

import base64
import json
import os
import re
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import hashlib

FORMAT_VERSION = 1
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
PEPPER_BYTES = 32
FAILS_PER_LOCK = 5
LOCK_BASE_SECONDS = 300
LOCK_MAX_SECONDS = 3600
SECRET_KEYS = ("channel", "spurl", "apiKey")
PIN_RE = re.compile(r"^\d{6,8}$")
_DIGITS = "0123456789012345678901234567890123456789"


class VaultError(Exception):
    """Base vault error."""


class InvalidPin(VaultError):
    """Wrong PIN."""


class WeakPin(VaultError):
    """PIN does not meet the policy."""


class PinLocked(VaultError):
    """Too many failures; retry later."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"locked for {retry_after}s")
        self.retry_after = retry_after


class UnknownDevice(VaultError):
    """Device not present in the vault."""


class PepperMissing(VaultError):
    """The vault exists but its pepper file is gone (vault unrecoverable)."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


def validate_pin(pin: str) -> None:
    """Raise WeakPin unless pin is 6-8 digits and not trivial."""
    if not isinstance(pin, str) or not PIN_RE.match(pin):
        raise WeakPin("pin_format")
    if len(set(pin)) == 1:
        raise WeakPin("pin_trivial")
    if pin in _DIGITS or pin[::-1] in _DIGITS:
        raise WeakPin("pin_trivial")
    for period in (2, 3):
        if len(pin) % period == 0 and pin == pin[:period] * (len(pin) // period):
            raise WeakPin("pin_trivial")


def load_pepper(path: str, create: bool) -> bytes:
    """Read the pepper file, creating it (mode 0600) when allowed."""
    try:
        with open(path, "rb") as handle:
            pepper = handle.read()
        if len(pepper) != PEPPER_BYTES:
            raise PepperMissing("pepper file is corrupted")
        return pepper
    except FileNotFoundError:
        if not create:
            raise PepperMissing("pepper file not found") from None
    pepper = os.urandom(PEPPER_BYTES)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pepper)
    return pepper


def _derive_kek(pin: str, salt: bytes, pepper: bytes, n: int, r: int, p: int) -> bytes:
    raw = hashlib.scrypt(
        pin.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=256 * n * r * p
    )
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=pepper, info=b"airsend-vault-kek-v1"
    ).derive(raw)


def _encrypt(key: bytes, plaintext: bytes, aad: bytes) -> dict:
    nonce = os.urandom(12)
    return {"nonce": _b64(nonce), "ct": _b64(AESGCM(key).encrypt(nonce, plaintext, aad))}


def _decrypt(key: bytes, blob: dict, aad: bytes) -> bytes:
    return AESGCM(key).decrypt(_unb64(blob["nonce"]), _unb64(blob["ct"]), aad)


class Vault:
    """In-memory view of the vault; ``to_dict()`` is what gets persisted."""

    def __init__(self, data: dict, pepper: bytes) -> None:
        if data.get("v") != FORMAT_VERSION:
            raise VaultError("unsupported vault version")
        self._d = data
        self._pepper = pepper
        self.dirty = False

    @classmethod
    def create(cls, pin: str, pepper: bytes, *, n: int = SCRYPT_N) -> "Vault":
        validate_pin(pin)
        data = {
            "v": FORMAT_VERSION,
            "kdf": {"n": n, "r": SCRYPT_R, "p": SCRYPT_P},
            "salt": "",
            "wrap": {},
            "fails": 0,
            "locked_until": 0,
            "devices": {},
        }
        vault = cls(data, pepper)
        vault._wrap_new_dek(os.urandom(32), pin)
        vault.dirty = True
        return vault

    # -- persistence -----------------------------------------------------
    def to_dict(self) -> dict:
        return json.loads(json.dumps(self._d))

    @property
    def device_ids(self) -> list[str]:
        return list(self._d["devices"])

    def has_device(self, device_id: str) -> bool:
        return device_id in self._d["devices"]

    # -- key handling ----------------------------------------------------
    def _kek(self, pin: str, salt: bytes) -> bytes:
        kdf = self._d["kdf"]
        return _derive_kek(pin, salt, self._pepper, kdf["n"], kdf["r"], kdf["p"])

    def _wrap_new_dek(self, dek: bytes, pin: str) -> None:
        salt = os.urandom(16)
        self._d["salt"] = _b64(salt)
        self._d["wrap"] = _encrypt(self._kek(pin, salt), dek, b"dek")

    def seconds_locked(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return max(0, int(self._d["locked_until"] - now + 0.999))

    def unlock(self, pin: str, now: float | None = None) -> bytes:
        """Return the DEK, or raise InvalidPin / PinLocked."""
        now = time.time() if now is None else now
        remaining = self.seconds_locked(now)
        if remaining:
            raise PinLocked(remaining)
        try:
            key = self._kek(str(pin), _unb64(self._d["salt"]))
            dek = _decrypt(key, self._d["wrap"], b"dek")
        except InvalidTag:
            self._register_failure(now)
            raise InvalidPin("invalid pin") from None
        if self._d["fails"]:
            self._d["fails"] = 0
            self._d["locked_until"] = 0
            self.dirty = True
        return dek

    def _register_failure(self, now: float) -> None:
        fails = self._d["fails"] + 1
        self._d["fails"] = fails
        if fails % FAILS_PER_LOCK == 0:
            level = fails // FAILS_PER_LOCK
            delay = min(LOCK_BASE_SECONDS * 3 ** (level - 1), LOCK_MAX_SECONDS)
            self._d["locked_until"] = now + delay
        self.dirty = True

    def change_pin(self, old_pin: str, new_pin: str, now: float | None = None) -> None:
        dek = self.unlock(old_pin, now)
        validate_pin(new_pin)
        self._wrap_new_dek(dek, new_pin)
        self.dirty = True

    # -- device secrets --------------------------------------------------
    @staticmethod
    def _aad(device_id: str) -> bytes:
        return b"device:" + device_id.encode("utf-8")

    def seal_device(self, dek: bytes, device_id: str, secrets: dict) -> None:
        payload = json.dumps(secrets, sort_keys=True).encode("utf-8")
        self._d["devices"][device_id] = _encrypt(dek, payload, self._aad(device_id))
        self.dirty = True

    def open_device(self, dek: bytes, device_id: str) -> dict:
        blob = self._d["devices"].get(device_id)
        if blob is None:
            raise UnknownDevice(device_id)
        return json.loads(_decrypt(dek, blob, self._aad(device_id)))

    def remove_device(self, device_id: str) -> None:
        if self._d["devices"].pop(device_id, None) is not None:
            self.dirty = True


def split_secure_devices(devices: dict) -> tuple[dict, dict]:
    """Return (public_devices, secrets_by_device_name).

    Devices flagged ``secure: true`` lose their secret fields; the removed
    values are returned so they can be sealed in the vault.
    """
    public: dict = {}
    secrets: dict = {}
    for name, options in devices.items():
        if isinstance(options, dict) and options.get("secure"):
            found = {k: options[k] for k in SECRET_KEYS if options.get(k)}
            public[name] = {k: v for k, v in options.items() if k not in SECRET_KEYS}
            if found:
                secrets[name] = found
        else:
            public[name] = options
    return public, secrets

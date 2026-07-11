"""Validated persistent identity storage for Larapaper Bridge."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, IDENTITY_VERSION, STORE_KEY, STORE_VERSION

_MAC_RE = re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")
_PENDING_KEYS = frozenset({"version", "mac"})
_COMPLETE_KEYS = frozenset({"version", "mac", "api_key", "friendly_id"})


class InvalidStoredState(ValueError):
    """Raised when the Larapaper identity payload is malformed."""


def canonicalize_mac(value: str) -> str:
    """Return an uppercase canonical MAC address."""
    if not isinstance(value, str):
        raise ValueError("MAC must be a string")
    mac = value.strip().upper()
    if not _MAC_RE.fullmatch(mac):
        raise ValueError("MAC must contain six hexadecimal octets")
    return mac


def validate_identity_payload(data: Any) -> dict[str, Any]:
    """Validate and return an exact pending or complete identity payload."""
    if not isinstance(data, Mapping):
        raise InvalidStoredState("identity payload must be an object")
    keys = frozenset(data)
    if keys not in (_PENDING_KEYS, _COMPLETE_KEYS):
        raise InvalidStoredState("identity payload has unexpected fields")
    if data.get("version") != IDENTITY_VERSION or isinstance(
        data.get("version"), bool
    ):
        raise InvalidStoredState("unsupported identity payload version")
    try:
        mac = canonicalize_mac(data["mac"])
    except (KeyError, ValueError) as err:
        raise InvalidStoredState("invalid identity MAC") from err
    payload: dict[str, Any] = {"version": IDENTITY_VERSION, "mac": mac}
    if keys == _COMPLETE_KEYS:
        for field in ("api_key", "friendly_id"):
            value = data.get(field)
            if not isinstance(value, str) or not value.strip():
                raise InvalidStoredState(f"invalid {field}")
            payload[field] = value.strip()
    return payload


class LarapaperStore:
    """Single adapter for the domain-wide Home Assistant Store."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass,
            STORE_VERSION,
            STORE_KEY,
            private=True,
            atomic_writes=True,
        )

    async def async_load(self) -> dict[str, Any] | None:
        """Load and validate the identity payload."""
        data = await self._store.async_load()
        if data is None:
            return None
        return validate_identity_payload(data)

    async def async_save_pending(self, mac: str) -> None:
        """Persist a pending identity."""
        await self._store.async_save(
            validate_identity_payload({"version": IDENTITY_VERSION, "mac": mac})
        )

    async def async_save_complete(
        self, mac: str, api_key: str, friendly_id: str
    ) -> None:
        """Persist a complete identity."""
        await self._store.async_save(
            validate_identity_payload(
                {
                    "version": IDENTITY_VERSION,
                    "mac": mac,
                    "api_key": api_key,
                    "friendly_id": friendly_id,
                }
            )
        )

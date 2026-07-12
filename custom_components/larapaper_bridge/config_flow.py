"""Configuration flow for Larapaper Bridge."""

from __future__ import annotations

import math
import secrets
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult

from .const import (
    CONF_BASE_URL,
    CONF_IMAGE_BASE_URL,
    CONF_MAC,
    CONF_MAX_IMAGE_BYTES,
    CONF_MAX_STALE_SECONDS,
    CONF_MIN_POLL_SECONDS,
    DEFAULT_MAX_IMAGE_BYTES,
    DEFAULT_MAX_STALE_SECONDS,
    DEFAULT_MIN_POLL_SECONDS,
    DOMAIN,
    ERROR_INVALID_BASE_URL,
    ERROR_INVALID_IMAGE_BASE_URL,
    ERROR_INVALID_MAC,
    ERROR_INVALID_MAX_IMAGE_BYTES,
    ERROR_INVALID_MAX_STALE_SECONDS,
    ERROR_INVALID_MIN_POLL_SECONDS,
    ERROR_INVALID_STORED_STATE,
)
from .storage import (
    IdentityAlreadyConfigured,
    InvalidStoredState,
    LarapaperStore,
    canonicalize_mac,
)


def _split_http_url(value: str, *, field: str) -> SplitResult:
    """Parse a credential-free HTTP(S) URL."""
    if not isinstance(value, str) or not (value := value.strip()):
        raise ValueError(field)
    if any(character.isspace() for character in value):
        raise ValueError(field)
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(field)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(field)
    if parsed.query or parsed.fragment:
        raise ValueError(field)
    try:
        parsed.port
    except ValueError as err:
        raise ValueError(field) from err
    return parsed


def _host(parsed: SplitResult) -> str:
    """Return a normalized URL hostname."""
    hostname = parsed.hostname
    assert hostname is not None
    return hostname.lower()


def _origin(parsed: SplitResult) -> str:
    """Return a normalized scheme/host/effective-port origin."""
    hostname = _host(parsed)
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = parsed.port
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    netloc = hostname if port in (None, default_port) else f"{hostname}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))


def normalize_base_url(value: str) -> str:
    """Normalize a Larapaper base URL while preserving its path prefix."""
    parsed = _split_http_url(value, field=CONF_BASE_URL)
    path = parsed.path.rstrip("/") + "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))


def normalize_image_base_url(value: str | None) -> str | None:
    """Normalize an optional image origin."""
    if value is None or not value.strip():
        return None
    parsed = _split_http_url(value, field=CONF_IMAGE_BASE_URL)
    if parsed.path not in ("", "/"):
        raise ValueError(CONF_IMAGE_BASE_URL)
    return _origin(parsed)


def _parse_positive_float(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, str):
        value = value.strip()
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError
    return parsed


def _parse_positive_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, str):
        value = value.strip()
        if not value.isdecimal():
            raise ValueError
    elif not isinstance(value, int):
        raise ValueError
    parsed = int(value)
    if parsed <= 0:
        raise ValueError
    return parsed


def _generate_mac() -> str:
    """Generate a locally administered unicast MAC with CSPRNG bytes."""
    raw = bytearray(secrets.token_bytes(6))
    raw[0] = (raw[0] & 0xFC) | 0x02
    return ":".join(f"{octet:02X}" for octet in raw)


def _schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_BASE_URL): str,
            vol.Optional(CONF_IMAGE_BASE_URL, default=""): str,
            vol.Optional(CONF_MAC, default=""): str,
            vol.Optional(
                CONF_MIN_POLL_SECONDS, default=str(DEFAULT_MIN_POLL_SECONDS)
            ): str,
            vol.Optional(
                CONF_MAX_STALE_SECONDS, default=str(DEFAULT_MAX_STALE_SECONDS)
            ): str,
            vol.Optional(
                CONF_MAX_IMAGE_BYTES, default=str(DEFAULT_MAX_IMAGE_BYTES)
            ): str,
        }
    )


class LarapaperBridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle Larapaper Bridge configuration."""

    VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user configuration step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = self._normalize_input(user_input)
            except ValueError as err:
                field = str(err)
                errors[field] = self._error_for_field(field)
            else:
                store = LarapaperStore(self.hass)
                configured_unique_ids = {
                    entry.unique_id
                    for entry in self.hass.config_entries.async_entries(DOMAIN)
                    if entry.unique_id
                }
                try:
                    async with store.async_flow_transaction() as transaction:
                        selection = await transaction.async_claim(
                            data[CONF_MAC],
                            configured_unique_ids,
                            _generate_mac,
                        )
                        await self.async_set_unique_id(selection.mac)
                        self._abort_if_unique_id_configured()
                        data[CONF_MAC] = selection.mac
                        return self.async_create_entry(
                            title=selection.mac,
                            data=data,
                        )
                except IdentityAlreadyConfigured:
                    return self.async_abort(reason="already_configured")
                except InvalidStoredState:
                    errors["base"] = ERROR_INVALID_STORED_STATE

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(),
            errors=errors,
        )

    @staticmethod
    def _normalize_input(user_input: dict[str, Any]) -> dict[str, Any]:
        """Trim and validate all configuration fields."""
        try:
            base_url = normalize_base_url(user_input[CONF_BASE_URL])
        except (KeyError, ValueError) as err:
            raise ValueError(CONF_BASE_URL) from err
        try:
            image_base_url = normalize_image_base_url(
                user_input.get(CONF_IMAGE_BASE_URL)
            )
        except ValueError as err:
            raise ValueError(CONF_IMAGE_BASE_URL) from err

        mac_input = user_input.get(CONF_MAC)
        if mac_input is None or (isinstance(mac_input, str) and not mac_input.strip()):
            mac = None
        else:
            try:
                mac = canonicalize_mac(mac_input)
            except ValueError as err:
                raise ValueError(CONF_MAC) from err

        numeric_fields = (
            (CONF_MIN_POLL_SECONDS, _parse_positive_float),
            (CONF_MAX_STALE_SECONDS, _parse_positive_int),
            (CONF_MAX_IMAGE_BYTES, _parse_positive_int),
        )
        numeric: dict[str, float | int] = {}
        for field, parser in numeric_fields:
            default = {
                CONF_MIN_POLL_SECONDS: DEFAULT_MIN_POLL_SECONDS,
                CONF_MAX_STALE_SECONDS: DEFAULT_MAX_STALE_SECONDS,
                CONF_MAX_IMAGE_BYTES: DEFAULT_MAX_IMAGE_BYTES,
            }[field]
            value = user_input.get(field, default)
            if value is None or (isinstance(value, str) and not value.strip()):
                value = default
            try:
                numeric[field] = parser(value)
            except (TypeError, ValueError, OverflowError) as err:
                raise ValueError(field) from err
        return {
            CONF_BASE_URL: base_url,
            CONF_IMAGE_BASE_URL: image_base_url,
            CONF_MAC: mac,
            **numeric,
        }

    @staticmethod
    def _error_for_field(field: str) -> str:
        return {
            CONF_BASE_URL: ERROR_INVALID_BASE_URL,
            CONF_IMAGE_BASE_URL: ERROR_INVALID_IMAGE_BASE_URL,
            CONF_MAC: ERROR_INVALID_MAC,
            CONF_MIN_POLL_SECONDS: ERROR_INVALID_MIN_POLL_SECONDS,
            CONF_MAX_STALE_SECONDS: ERROR_INVALID_MAX_STALE_SECONDS,
            CONF_MAX_IMAGE_BYTES: ERROR_INVALID_MAX_IMAGE_BYTES,
        }.get(field, ERROR_INVALID_BASE_URL)


async def async_migrate_entry(hass: Any, config_entry: ConfigEntry) -> bool:
    """Migrate the V1 config entry and identity payload atomically."""
    if config_entry.version != 1:
        return True
    try:
        mac = canonicalize_mac(config_entry.data[CONF_MAC])
    except (KeyError, ValueError) as err:
        raise InvalidStoredState("config entry has an invalid MAC") from err
    identity = await LarapaperStore(hass).async_migrate_v1(mac)
    title = identity.get("friendly_id", mac)
    hass.config_entries.async_update_entry(
        config_entry,
        version=2,
        unique_id=mac,
        title=title,
    )
    return True

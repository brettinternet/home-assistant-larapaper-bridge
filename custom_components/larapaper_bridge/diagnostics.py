"""Redacted, deterministic Larapaper integration diagnostics."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .runtime import RuntimeHolder
from .scheduler import DiagnosticsState, _safe_error_code

_DIAGNOSTIC_KEYS = (
    "status",
    "ready",
    "stale",
    "last_success_at",
    "last_success_age_seconds",
    "last_error",
    "next_display_at",
    "next_retry_at",
)


def _serialize_datetime(value: datetime | None) -> str | None:
    """Serialize one scheduler timestamp without exposing non-JSON values."""
    return value.isoformat() if value is not None else None


def _empty_state(last_error: str | None = None) -> DiagnosticsState:
    """Return the cold-start or provisioning-error projection."""
    return DiagnosticsState(
        status="error" if last_error is not None else "starting",
        last_error=last_error,
    )


def _diagnostics_dict(state: DiagnosticsState) -> dict[str, Any]:
    """Convert the typed scheduler projection into its fixed whitelist."""
    last_error = (
        None if state.last_error is None else _safe_error_code(state.last_error)
    )
    age = state.last_success_age_seconds
    return {
        "status": state.status,
        "ready": state.ready,
        "stale": state.stale,
        "last_success_at": _serialize_datetime(state.last_success_at),
        "last_success_age_seconds": max(0, age) if age is not None else None,
        "last_error": last_error,
        "next_display_at": _serialize_datetime(state.next_display_at),
        "next_retry_at": _serialize_datetime(state.next_retry_at),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return only the current in-memory, redacted runtime projection."""
    holder = hass.data.get(DOMAIN)
    state = _empty_state()
    if isinstance(holder, RuntimeHolder):
        runtime = holder.current
        if runtime is not None and runtime.config_entry is entry:
            if runtime.scheduler is not None:
                state = runtime.scheduler.diagnostics_state()
            else:
                state = _empty_state(getattr(runtime, "provisioning_error", None))
    result = _diagnostics_dict(state)
    assert tuple(result) == _DIAGNOSTIC_KEYS
    return result


__all__ = ["async_get_config_entry_diagnostics"]

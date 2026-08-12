"""Diagnostics support for RaySharp NVR.

Dumps what the NVR actually returned, which is otherwise invisible: the API
shapes differ between firmwares, and a channel that never reports an alarm
looks exactly like a parser that drops it.  Download from the integration's
device page, or GET /api/diagnostics/config_entry/<entry_id>.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PASSWORD, CONF_USERNAME, DOMAIN
from .coordinator import RaySharpNVRCoordinator

TO_REDACT = {CONF_USERNAME, CONF_PASSWORD, "password", "username"}

# Base64 images and long record lists make the dump unreadable and huge.
_MAX_STR = 200
_MAX_LIST = 10


def _trim(value: Any, depth: int = 0) -> Any:
    """Shorten base64 blobs and long lists so the dump stays readable."""
    if depth > 6:
        return "…"
    if isinstance(value, str):
        if len(value) > _MAX_STR:
            return f"<{len(value)} chars> {value[:60]}…"
        return value
    if isinstance(value, dict):
        return {k: _trim(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        trimmed = [_trim(v, depth + 1) for v in value[:_MAX_LIST]]
        if len(value) > _MAX_LIST:
            trimmed.append(f"… {len(value) - _MAX_LIST} more of {len(value)}")
        return trimmed
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: RaySharpNVRCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )

    diag: dict[str, Any] = {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
            "state": str(entry.state),
        }
    }

    if coordinator is None:
        diag["error"] = "coordinator not loaded"
        return diag

    diag["coordinator"] = {
        "last_update_success": coordinator.last_update_success,
        "update_interval": str(coordinator.update_interval),
        "failing_endpoints": sorted(coordinator._failing_endpoints),  # noqa: SLF001
        "keys_with_data": sorted(
            k for k, v in (coordinator.data or {}).items() if v is not None
        ),
        "keys_empty": sorted(
            k for k, v in (coordinator.data or {}).items() if v is None
        ),
    }
    diag["event_check"] = coordinator.event_check_diagnostics()
    diag["ai_poll"] = coordinator.ai_poll_diagnostics()
    diag["data"] = _trim(async_redact_data(coordinator.data or {}, TO_REDACT))
    return diag

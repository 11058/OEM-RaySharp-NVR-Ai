"""Sidebar panel "Видео": a web component served from the integration."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components import panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

WWW = Path(__file__).parent / "www"
BUNDLE = "raysharp-nvr-panel.js"
STATIC = "/raysharp_nvr_panel"
KEY = f"{DOMAIN}_panel"


async def async_setup_panel(hass: HomeAssistant) -> None:
    """Register once per HA run; further NVR entries reuse it."""
    if hass.data.get(KEY):
        return
    hass.data[KEY] = True
    from .events import async_setup_event_log
    from .websocket_api import async_register_websocket

    await async_setup_event_log(hass)
    async_register_websocket(hass)
    if not (WWW / BUNDLE).is_file():
        _LOGGER.warning("Panel bundle missing, video panel not registered")
        return
    await hass.http.async_register_static_paths(
        [StaticPathConfig(f"{STATIC}/{BUNDLE}", str(WWW / BUNDLE), cache_headers=False)]
    )
    version = (await async_get_integration(hass, DOMAIN)).version or "0"
    await panel_custom.async_register_panel(
        hass,
        webcomponent_name="raysharp-nvr-panel",
        frontend_url_path="video",
        module_url=f"{STATIC}/{BUNDLE}?v={version}",
        sidebar_title="Видео",
        sidebar_icon="mdi:cctv",
        require_admin=False,
        config={},
    )

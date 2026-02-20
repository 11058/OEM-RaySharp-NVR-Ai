"""DataUpdateCoordinator for RaySharp NVR."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api_client import (
    RaySharpNVRAuthError,
    RaySharpNVRClient,
    RaySharpNVRConnectionError,
)
from .const import (
    API_AI_CC_STATS,
    API_AI_CROSS_COUNTING,
    API_AI_FACES,
    API_AI_FD_SETUP,
    API_AI_FACE_STATS,
    API_AI_HEATMAP_STATS,
    API_AI_INTRUSION_SETUP,
    API_AI_LCD_SETUP,
    API_AI_MODEL,
    API_AI_OBJECT_STATS,
    API_AI_PLATES,
    API_AI_PROCESS_ALARM,
    API_AI_PVD_SETUP,
    API_AI_SCHEDULE,
    API_AI_VHD_COUNT,
    API_ALARM_FD,
    API_ALARM_LCD,
    API_ALARM_PID,
    API_ALARM_SOD,
    API_CHANNEL_INFO,
    API_DATE_TIME,
    API_DEVICE_INFO,
    API_DISARMING,
    API_DISK_GET,
    API_EVENT_PUSH_CONFIG,
    API_EXCEPTION_ALARM,
    API_IO_ALARM,
    API_MOTION_ALARM,
    API_NETWORK_STATE,
    API_RECORD_INFO,
    API_STREAM_URL,
    API_SYSTEM_INFO,
    DATA_AI_CC_STATS,
    DATA_AI_CROSS_COUNTING,
    DATA_AI_FACE_STATS,
    DATA_AI_FACES,
    DATA_AI_FD_SETUP,
    DATA_AI_HEATMAP_STATS,
    DATA_AI_INTRUSION_SETUP,
    DATA_AI_LCD_SETUP,
    DATA_AI_MODEL,
    DATA_AI_OBJECT_STATS,
    DATA_AI_PLATES,
    DATA_AI_PROCESS_ALARM,
    DATA_AI_PVD_SETUP,
    DATA_AI_SCHEDULE,
    DATA_AI_VHD_COUNT,
    DATA_ALARM_FD,
    DATA_ALARM_LCD,
    DATA_ALARM_PID,
    DATA_ALARM_SOD,
    DATA_CHANNEL_INFO,
    DATA_DATE_TIME,
    DATA_DEVICE_INFO,
    DATA_DISARMING,
    DATA_DISK_CONFIG,
    DATA_EVENT_PUSH_CONFIG,
    DATA_EXCEPTION_ALARM,
    DATA_IO_ALARM,
    DATA_MOTION_ALARM,
    DATA_NETWORK_STATE,
    DATA_RECORD_INFO,
    DATA_RTSP_URLS,
    DATA_SYSTEM_INFO,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Core endpoints — always fetched; failures logged as warnings
ENDPOINT_MAP = {
    DATA_DEVICE_INFO: API_DEVICE_INFO,
    DATA_CHANNEL_INFO: API_CHANNEL_INFO,
    DATA_DISK_CONFIG: API_DISK_GET,
    DATA_RTSP_URLS: API_STREAM_URL,
    DATA_DISARMING: API_DISARMING,
    DATA_EVENT_PUSH_CONFIG: API_EVENT_PUSH_CONFIG,
    DATA_SYSTEM_INFO: API_SYSTEM_INFO,
    DATA_NETWORK_STATE: API_NETWORK_STATE,
    DATA_RECORD_INFO: API_RECORD_INFO,
}

# Alarm config endpoints — fetched optionally (firmware-dependent)
ALARM_ENDPOINT_MAP = {
    DATA_DATE_TIME: API_DATE_TIME,
    DATA_MOTION_ALARM: API_MOTION_ALARM,
    DATA_IO_ALARM: API_IO_ALARM,
    DATA_EXCEPTION_ALARM: API_EXCEPTION_ALARM,
    DATA_ALARM_FD: API_ALARM_FD,
    DATA_ALARM_LCD: API_ALARM_LCD,
    DATA_ALARM_PID: API_ALARM_PID,
    DATA_ALARM_SOD: API_ALARM_SOD,
}

# AI endpoints — fetched optionally, require AI-capable hardware.
# Stored as (data_key, api_endpoint, request_data_or_None).
# Endpoints that need time-range parameters are listed with a sentinel;
# the actual params are built at fetch time in _async_update_data.
AI_NO_PARAM_ENDPOINTS: dict[str, str] = {
    DATA_AI_SCHEDULE: API_AI_SCHEDULE,
    DATA_AI_PROCESS_ALARM: API_AI_PROCESS_ALARM,
    DATA_AI_MODEL: API_AI_MODEL,
}
# Setup endpoints for AI detection types: require page_type="ChannelConfig"
# to get per-channel enable/disable state (used by binary_sensor).
AI_SETUP_ENDPOINTS: dict[str, str] = {
    DATA_AI_FD_SETUP: API_AI_FD_SETUP,
    DATA_AI_PVD_SETUP: API_AI_PVD_SETUP,
    DATA_AI_LCD_SETUP: API_AI_LCD_SETUP,
    DATA_AI_INTRUSION_SETUP: API_AI_INTRUSION_SETUP,
}
_CHANNEL_CONFIG_PARAMS: dict[str, str] = {"page_type": "ChannelConfig"}


class RaySharpNVRCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for polling all RaySharp NVR data."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: RaySharpNVRClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        self.client = client
        scan_interval = entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
            config_entry=entry,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch all data from the NVR."""
        if not self.client.authenticated:
            try:
                await self.client.async_login()
            except RaySharpNVRAuthError as err:
                raise ConfigEntryAuthFailed(
                    "Authentication failed, check credentials"
                ) from err
            except RaySharpNVRConnectionError as err:
                raise UpdateFailed(f"Cannot connect to NVR: {err}") from err

        # Heartbeat to keep session alive
        try:
            await self.client.async_heartbeat()
        except (RaySharpNVRAuthError, RaySharpNVRConnectionError):
            _LOGGER.debug("Heartbeat failed, will re-login on next API call")

        data: dict[str, Any] = {}

        # ── Core endpoints (warnings on failure) ─────────────────────────────
        try:
            results = await asyncio.gather(
                *[
                    self.client.async_api_call(endpoint)
                    for endpoint in ENDPOINT_MAP.values()
                ],
                return_exceptions=True,
            )
            for key, result in zip(ENDPOINT_MAP.keys(), results):
                if isinstance(result, RaySharpNVRAuthError):
                    raise ConfigEntryAuthFailed(
                        "Authentication failed during data fetch"
                    ) from result
                if isinstance(result, Exception):
                    _LOGGER.warning("Failed to fetch %s: %s", key, result)
                    data[key] = None
                else:
                    data[key] = self._extract_data(result)
        except RaySharpNVRAuthError as err:
            raise ConfigEntryAuthFailed(
                "Authentication failed, check credentials"
            ) from err
        except RaySharpNVRConnectionError as err:
            raise UpdateFailed(f"Error communicating with NVR: {err}") from err

        # ── Alarm config endpoints (debug on failure) ─────────────────────────
        alarm_results = await asyncio.gather(
            *[
                self.client.async_api_call(endpoint)
                for endpoint in ALARM_ENDPOINT_MAP.values()
            ],
            return_exceptions=True,
        )
        for key, result in zip(ALARM_ENDPOINT_MAP.keys(), alarm_results):
            if isinstance(result, Exception):
                _LOGGER.debug("Alarm endpoint %s not available: %s", key, result)
                data[key] = None
            else:
                data[key] = self._extract_data(result)

        # ── AI endpoints (debug on failure) ──────────────────────────────────
        # Build today's date range for parametrized AI queries
        now = datetime.now()
        today_start = now.strftime("%Y-%m-%d 00:00:00")
        today_end = now.strftime("%Y-%m-%d 23:59:59")

        # VhdLogCount: counts for face(0), person(1), vehicle(2), plate(10)
        vhd_params = {
            "MsgId": None,
            "StartTime": today_start,
            "EndTime": today_end,
            "Chn": [],          # empty = all channels
            "Type": [0, 1, 2, 10],
            "Engine": 0,
        }

        # Build full list of (key, endpoint, params) for gathering
        ai_calls: list[tuple[str, str, dict | None]] = [
            (key, ep, None) for key, ep in AI_NO_PARAM_ENDPOINTS.items()
        ] + [
            # Setup endpoints: page_type="ChannelConfig" returns per-channel switch state
            (key, ep, _CHANNEL_CONFIG_PARAMS)
            for key, ep in AI_SETUP_ENDPOINTS.items()
        ] + [
            (DATA_AI_VHD_COUNT, API_AI_VHD_COUNT, vhd_params),
        ]

        ai_results = await asyncio.gather(
            *[
                self.client.async_api_call(ep, params)
                for _, ep, params in ai_calls
            ],
            return_exceptions=True,
        )
        for (key, _ep, _params), result in zip(ai_calls, ai_results):
            if isinstance(result, Exception):
                _LOGGER.debug("AI endpoint %s not available: %s", key, result)
                data[key] = None
            else:
                data[key] = self._extract_data(result)

        return data

    @staticmethod
    def _extract_data(response: dict[str, Any]) -> Any:
        """Extract data payload from API response envelope."""
        if isinstance(response, dict):
            return response.get("data", response)
        return response

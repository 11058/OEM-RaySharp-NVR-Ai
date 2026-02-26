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
    API_AI_LPD_SETUP,
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
    DATA_AI_LPD_SETUP,
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
    DATA_AI_LPD_SETUP: API_AI_LPD_SETUP,  # LPD may 404 on older firmware — handled gracefully
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
        self._event_check_task: asyncio.Task | None = None
        self._event_check_reader_id: int | None = None
        self._event_check_sequence: int | None = None
        self._event_check_lap: int | None = None

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

    # ── Event Check long-polling ──────────────────────────────────────────────

    def async_start_event_check_loop(self) -> None:
        """Start the NVR Event Check long-polling loop (idempotent)."""
        if self._event_check_task and not self._event_check_task.done():
            return
        self._event_check_reader_id = None
        self._event_check_sequence = None
        self._event_check_lap = None
        self._event_check_task = self.hass.async_create_background_task(
            self._async_event_check_loop(),
            "raysharp_nvr_event_check",
        )
        _LOGGER.debug("Started NVR Event Check long-polling task")

    async def async_stop_event_check_loop(self) -> None:
        """Stop the Event Check loop and await task teardown."""
        task = self._event_check_task
        self._event_check_task = None
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.shield(task)
            except (asyncio.CancelledError, Exception):
                pass
        _LOGGER.debug("Stopped NVR Event Check long-polling task")

    async def _async_event_check_loop(self) -> None:
        """Continuously long-poll /API/Event/Check for real-time NVR events.

        – On first call (reader_id=None) the NVR creates a subscription.
        – Subsequent calls carry the reader_id + sequence to receive only new
          events.  A "heat_alarm" response means no new events; the NVR already
          blocked for its internal long-poll window before returning it.
        – On transient errors (network, timeout) we keep reader_id and retry
          after a short delay.  On auth errors we re-subscribe from scratch.
        """
        retry_delay = 5.0
        consecutive_errors = 0

        while True:
            try:
                response = await self.client.async_event_check(
                    reader_id=self._event_check_reader_id,
                    sequence=self._event_check_sequence,
                    lap_number=self._event_check_lap,
                )

                # Empty response = OS-level timeout (no network data received).
                # Keep existing reader_id / sequence and retry immediately.
                if not response:
                    await asyncio.sleep(1)
                    continue

                data = response.get("data", response) if isinstance(response, dict) else {}
                if not isinstance(data, dict):
                    data = {}

                # Update subscription tracking
                if "reader_id" in data:
                    self._event_check_reader_id = data["reader_id"]
                if "sequence" in data:
                    self._event_check_sequence = data["sequence"]
                if "lap_number" in data:
                    self._event_check_lap = data["lap_number"]

                # Dispatch events to HA bus when payload contains real events
                has_alarm = "alarm_list" in data and data["alarm_list"]
                has_snap = "ai_snap_picture" in data and data["ai_snap_picture"]
                if has_alarm or has_snap:
                    self._dispatch_event_check_data(response)

                consecutive_errors = 0
                retry_delay = 5.0
                # Short-polling: NVR responds immediately.  Throttle to avoid
                # hammering the device — 2 s between requests is enough for
                # real-time event detection without overloading the NVR.
                await asyncio.sleep(2)

            except asyncio.CancelledError:
                _LOGGER.debug("Event Check loop cancelled")
                break

            except RaySharpNVRAuthError:
                _LOGGER.debug("Event Check auth error — re-subscribing")
                self._event_check_reader_id = None
                self._event_check_sequence = None
                self._event_check_lap = None
                await asyncio.sleep(retry_delay)

            except (RaySharpNVRConnectionError, Exception) as err:
                consecutive_errors += 1
                _LOGGER.debug(
                    "Event Check error #%d: %s — retrying in %.0fs",
                    consecutive_errors, err, retry_delay,
                )
                # Keep reader_id on transient errors so we don't re-subscribe
                # unnecessarily; reset if we've been failing for a while.
                if consecutive_errors >= 5:
                    self._event_check_reader_id = None
                    self._event_check_sequence = None
                    self._event_check_lap = None
                await asyncio.sleep(min(retry_delay, 60.0))
                retry_delay = min(retry_delay * 1.5, 60.0)

    def _dispatch_event_check_data(self, response: dict[str, Any]) -> None:
        """Parse Event Check response and fire HA bus events.

        Lazy-imports the parsing helpers from __init__ to avoid circular imports
        at module load time.
        """
        try:
            from . import _parse_alarm_payload, _parse_snapshot_payload  # noqa: PLC0415
            from .const import EVENT_ALARM, EVENT_SNAPSHOT  # noqa: PLC0415
        except ImportError:
            _LOGGER.debug("Could not import event parsers — skipping dispatch")
            return

        for event_data in _parse_alarm_payload(response):
            self.hass.bus.async_fire(EVENT_ALARM, event_data)

        for snap in _parse_snapshot_payload(response):
            self.hass.bus.async_fire(EVENT_SNAPSHOT, snap)
            _LOGGER.debug(
                "Event Check: fired %s snapshot for channel %s",
                snap.get("alarm_type"),
                snap.get("channel"),
            )

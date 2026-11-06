"""DataUpdateCoordinator for RaySharp NVR."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api_client import (
    RaySharpNVRApiError,
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
    API_AI_OBJECTS_GET_BY_INDEX,
    API_AI_OBJECTS_SEARCH,
    API_AI_PLATES,
    API_AI_PROCESS_ALARM,
    API_AI_PVD_SETUP,
    API_AI_SCHEDULE,
    API_AI_VHD_COUNT,
    API_AI_VHD_GET,
    API_RECORD_SEARCH,
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
    CONF_SNAPSHOT_HISTORY_COUNT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SNAPSHOT_HISTORY_COUNT,
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

# These answer `param_error` to an empty body — like the AI setup endpoints
# they want page_type to pick which slice of the config to return.  Verified
# against V8.2.4.1: without it every one of them fails, with it all succeed.
ALARM_ENDPOINTS_NEEDING_PAGE_TYPE = frozenset(
    {
        DATA_MOTION_ALARM,
        DATA_ALARM_FD,
        DATA_ALARM_LCD,
        DATA_ALARM_PID,
        DATA_ALARM_SOD,
    }
)

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

# ── AI detection polling ──────────────────────────────────────────────────────
# Event Check carries ai_snap_picture on some firmwares and never does on
# others — verified on V8.2.4.1, which recorded 841 person detections in a day
# while its event stream stayed empty.  The records are always readable from
# the object database, so poll it and replay new rows through the same parser
# the push path uses.
AI_POLL_INTERVAL = 10.0
# Bursts happen (hundreds of detections in a couple of minutes); cap the
# per-poll fetch so one busy minute can't pull megabytes of JPEGs at once.
AI_POLL_MAX_BATCH = 20
# Event recordings on disk feed the journal: a day back at start, then
# every few minutes.
RECORD_HISTORY_HOURS = 24
RECORD_HISTORY_INTERVAL = 300.0


# Fallback for firmware that refuses SnapedObjects to non-admin accounts.
VHD_BACKFILL_HOURS = 24
VHD_MAX_PER_POLL = 40
# A window wide enough to never need the NVR's clock, which runs in its own
# local time and does not match Home Assistant's.
_AI_SEARCH_WINDOW = {
    "StartTime": "2000-01-01 00:00:00",
    "EndTime": "2099-12-31 23:59:59",
}
_AI_OBJECT_TYPES = [1, 2]  # 1 = person, 2 = vehicle
# Remember enough snapshot ids to recognise a record the push path already
# delivered, without growing without bound.
_SEEN_SNAPS_MAX = 512
# On startup, replay stored detections per channel.  Done per channel rather
# than as a flat tail of the result set: one busy camera can account for every
# recent record, leaving every other card empty.  The depth follows the
# configured history so the cards start full rather than part-filled, with a
# ceiling to bound the startup cost — every row carries a JPEG.
AI_BACKFILL_MAX_PER_CHANNEL = 24


def channel_num_from_str(ch_str: str, fallback: int) -> int:
    """Convert 'CH17' → 17, '17' → 17, or return fallback."""
    s = str(ch_str).strip()
    if s.upper().startswith("CH"):
        try:
            return int(s[2:])
        except ValueError:
            pass
    try:
        return int(s)
    except (ValueError, TypeError):
        return fallback


def get_channel_list(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the channel list from coordinator data."""
    channel_data = data.get(DATA_CHANNEL_INFO)
    if not channel_data:
        return []
    if isinstance(channel_data, dict):
        channels = channel_data.get("channel_param", {}).get("items", [])
        if not channels:
            channels = channel_data.get("channels", channel_data.get("channel", []))
    elif isinstance(channel_data, list):
        channels = channel_data
    else:
        return []
    if not isinstance(channels, list):
        channels = [channels]
    return channels


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
        # Deduplication: suppress repeated identical alarms from Event Check polling.
        # Key: (channel, alarm_type) → monotonic timestamp of last dispatch.
        self._alarm_debounce: dict[tuple[int, str], float] = {}
        self._alarm_debounce_window: float = 10.0  # seconds
        # Core endpoints currently failing — used to log each failure once
        # instead of on every poll.
        self._failing_endpoints: set[str] = set()
        # Event Check bookkeeping — surfaced through diagnostics, because a
        # silent loop and a loop the NVR never sends anything to look alike
        # from the outside.
        self._ec_polls = 0
        self.record_flags: dict[int, bool] = {}
        self.nvr_offset_s: float | None = None  # NVR wall clock minus UTC
        self.clock_skew_s: float | None = None  # newest recording end minus HA's now
        self._clock_task = None
        self.snapshot_errors: dict[int, str] = {}
        self.channel_abilities: dict[int, list[str]] = {}
        self._ec_errors = 0
        self._ec_dispatches = 0
        self._ec_last_error: str | None = None
        self._ec_last_payload: dict[str, Any] | None = None
        self._ec_last_payload_keys: list[str] = []
        # AI detection polling state.
        self._ai_task: asyncio.Task | None = None
        self._ai_cursor: int | None = None
        self._ai_dispatched = 0
        self._ai_last_error: str | None = None
        # GetByIndex pages through whatever the last Search in this session
        # selected, so searches must not interleave.  The search services in
        # __init__ take this lock too.
        self.ai_search_lock = asyncio.Lock()
        # Snapshot ids already sent to the bus, by either path.
        self._seen_snaps: deque[tuple[Any, Any]] = deque(maxlen=_SEEN_SNAPS_MAX)
        self._seen_snaps_set: set[tuple[Any, Any]] = set()

    def event_check_diagnostics(self) -> dict[str, Any]:
        """Return a snapshot of Event Check activity for diagnostics."""
        task = self._event_check_task
        return {
            "running": bool(task and not task.done()),
            "reader_id": self._event_check_reader_id,
            "sequence": self._event_check_sequence,
            "polls": self._ec_polls,
            "errors": self._ec_errors,
            "dispatches": self._ec_dispatches,
            "last_error": self._ec_last_error,
            # Keys seen across all polls: shows whether the NVR ever sends
            # alarm_list / ai_snap_picture at all on this firmware.
            "payload_keys_seen": sorted(self._ec_last_payload_keys),
            "last_payload": self._ec_last_payload,
        }

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

        # Heartbeat to keep session alive.  A failing heartbeat means the NVR
        # no longer knows our session (it answers those with HTTP 400, not
        # 401), so log in again right away — otherwise every endpoint below
        # fails and the entities silently go blank until a manual reload.
        try:
            alive = await self.client.async_heartbeat()
        except (RaySharpNVRAuthError, RaySharpNVRConnectionError):
            alive = False
        if not alive:
            _LOGGER.debug("Heartbeat failed — re-authenticating")
            try:
                await self.client.async_login()
            except RaySharpNVRAuthError as err:
                raise ConfigEntryAuthFailed(
                    "Authentication failed, check credentials"
                ) from err
            except RaySharpNVRConnectionError as err:
                raise UpdateFailed(f"Cannot connect to NVR: {err}") from err

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
                    # Log once per outage, not on every poll — a dead session
                    # otherwise buries the log under thousands of lines.
                    if key not in self._failing_endpoints:
                        self._failing_endpoints.add(key)
                        _LOGGER.warning("Failed to fetch %s: %s", key, result)
                    data[key] = None
                else:
                    self._failing_endpoints.discard(key)
                    data[key] = self._extract_data(result)
        except RaySharpNVRAuthError as err:
            raise ConfigEntryAuthFailed(
                "Authentication failed, check credentials"
            ) from err
        except RaySharpNVRConnectionError as err:
            raise UpdateFailed(f"Error communicating with NVR: {err}") from err

        # Every core endpoint failed → the NVR is unreachable or rejecting the
        # session.  Fail the refresh so the entities go unavailable instead of
        # quietly reporting "unknown" forever.
        if all(value is None for value in data.values()):
            self.client.invalidate_session()
            raise UpdateFailed("No response from any NVR endpoint")

        # ── Alarm config endpoints (debug on failure) ─────────────────────────
        alarm_results = await asyncio.gather(
            *[
                self.client.async_api_call(
                    endpoint,
                    _CHANNEL_CONFIG_PARAMS
                    if key in ALARM_ENDPOINTS_NEEDING_PAGE_TYPE
                    else None,
                )
                for key, endpoint in ALARM_ENDPOINT_MAP.items()
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

                self._ec_polls += 1
                for key in data:
                    if key not in self._ec_last_payload_keys:
                        self._ec_last_payload_keys.append(key)
                # Keep the last payload that is more than a "nothing happened"
                # keepalive, so diagnostics can show what the NVR really sends.
                if set(data) - {"heat_alarm", "reader_id", "sequence", "lap_number"}:
                    self._ec_last_payload = data

                self._update_record_flags(data)

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
                # talkback_alarm lives inside alarm_list entries — already covered by has_alarm
                if has_alarm or has_snap:
                    self._ec_dispatches += 1
                    _LOGGER.debug("Event Check payload: %s", data)
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
                self._ec_errors += 1
                self._ec_last_error = str(err)
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

    # ── AI detection polling ──────────────────────────────────────────────────

    def async_start_ai_poll_loop(self) -> None:
        """Start polling the NVR's AI object database (idempotent)."""
        if self._ai_task and not self._ai_task.done():
            return
        self._ai_cursor = None
        self._ai_source = "objects"
        self._ai_task = self.hass.async_create_background_task(
            self._async_ai_poll_loop(),
            "raysharp_nvr_ai_poll",
        )
        _LOGGER.debug("Started AI detection polling task")

    async def async_stop_ai_poll_loop(self) -> None:
        """Stop the AI polling loop and await task teardown."""
        task = self._ai_task
        self._ai_task = None
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.shield(task)
            except (asyncio.CancelledError, Exception):
                pass
        _LOGGER.debug("Stopped AI detection polling task")

    async def _async_ai_poll_loop(self) -> None:
        """Replay AI detections the NVR stored but never pushed."""
        while True:
            try:
                await asyncio.sleep(AI_POLL_INTERVAL)
                await self._async_poll_ai_detections()
            except asyncio.CancelledError:
                _LOGGER.debug("AI detection polling cancelled")
                break
            except RaySharpNVRApiError as err:
                self._ai_last_error = str(err)
                if err.error_code == "no_permission":
                    # Some firmware keeps SnapedObjects to admin even with every
                    # right ticked, yet answers the person/vehicle log. Switch
                    # to that; stop only if it is refused too.
                    _LOGGER.info(
                        "NVR refuses AI database search (%s); falling back to "
                        "the person/vehicle log", err,
                    )
                    if not await self._async_vhd_loop():
                        break
                    continue
                _LOGGER.debug("AI detection poll refused: %s", err)
            except Exception as err:  # noqa: BLE001 — loop must survive anything
                self._ai_last_error = str(err)
                _LOGGER.debug("AI detection poll failed: %s", err)

    def async_start_record_history(self) -> None:
        """Fill the panel's journal from event recordings on the NVR's disk."""
        if getattr(self, "_rec_task", None) is None or self._rec_task.done():
            self._rec_task = self.config_entry.async_create_background_task(
                self.hass, self._async_record_history_loop(), "raysharp_nvr record history"
            )
        if getattr(self, "_arch_task", None) is None or self._arch_task.done():
            self._arch_task = self.config_entry.async_create_background_task(
                self.hass, self._async_archive_loop(), "raysharp_nvr archive bounds"
            )

    async def _async_archive_loop(self) -> None:
        """Where the NVR's archive starts, per channel, once an hour: events
        older than that have no video any more (overwritten, disk formatted).
        In the "nvr" journal mode those events leave the journal too."""
        await asyncio.sleep(20)
        while True:
            try:
                await self._async_learn_clock()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - keep trying
                _LOGGER.debug("NVR clock: %s", err)
            if self._clock_task is None or self._clock_task.done():
                self._clock_task = self.config_entry.async_create_background_task(
                    self.hass, self._async_clock_loop(), "raysharp_nvr clock"
                )
            try:
                await self._async_archive_bounds()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - keep trying
                _LOGGER.debug("Archive bounds failed: %s", err)
            await asyncio.sleep(3600)

    async def _async_clock_loop(self) -> None:
        """The NVR's time zone every 5 minutes: someone may change it."""
        while True:
            await asyncio.sleep(300)
            try:
                await self._async_learn_clock()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - keep trying
                _LOGGER.debug("NVR clock: %s", err)

    async def _async_learn_clock(self) -> None:
        """The NVR's UTC offset from the Date of an RTSP OPTIONS answer (its
        wall clock labelled GMT; no login needed). Every NVR wall-clock time —
        recordings, the archive search, AI pictures — is converted with it, so
        any NVR time zone works whatever zone HA is in."""
        import re as _re  # noqa: PLC0415
        import time as _time  # noqa: PLC0415
        from urllib.parse import urlparse  # noqa: PLC0415

        from .events import offset_from_date_header, offset_from_zone, set_nvr_offset  # noqa: PLC0415

        # The NVR's own setting: exact, and it follows a time zone change at once.
        try:
            resp = await self.client.async_api_call(API_DATE_TIME, {})
            zone = (self._extract_data(resp) or {}).get("time_zone")
            off = offset_from_zone(zone) if zone else None
        except Exception:  # noqa: BLE001 - older firmware: fall back to the Date header
            off = None
        if off is not None:
            if off != self.nvr_offset_s:
                _LOGGER.info("NVR clock: %s", zone)
            self.nvr_offset_s = off
            set_nvr_offset(off)
            return

        text = str((self.data or {}).get(DATA_RTSP_URLS) or "")
        m = _re.search(r"rtsp://[^\s'\"]+", text)
        port = urlparse(m.group(0)).port if m else None
        for p in [port or 554, 554, 80]:
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(self.client._host, p), 5)  # noqa: SLF001
            except (OSError, TimeoutError):
                continue
            try:
                writer.write(f"OPTIONS rtsp://{self.client._host}:{p}/ RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode())  # noqa: SLF001
                await writer.drain()
                head = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)).decode(errors="replace")
            except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                continue
            finally:
                writer.close()
            if not head.startswith("RTSP/"):
                continue
            d = _re.search(r"(?im)^date:\s*(.+)$", head)
            off = offset_from_date_header(d.group(1).strip(), _time.time()) if d else None
            if off is not None:
                if off != self.nvr_offset_s:
                    _LOGGER.info("NVR clock: UTC%+.2g h (Date header)", off / 3600)
                self.nvr_offset_s = off
                set_nvr_offset(off)
                return

    async def _async_archive_bounds(self) -> None:
        from .events import get_event_log  # noqa: PLC0415

        log = get_event_log(self.hass)
        channels = sorted(self.channel_abilities) or sorted(
            {e["channel"] for e in log.events if isinstance(e.get("channel"), int) and e["channel"]}
        )
        if not channels:
            return
        now = datetime.now()
        oldest = min((e["ts"] for e in log.events), default=now.timestamp())
        since = max(datetime.fromtimestamp(oldest) - timedelta(days=1), now - timedelta(days=120))
        # Search a day ahead too: an NVR whose clock runs ahead (time zone reset
        # by a firmware update) writes recordings "in the future".
        horizon = now + timedelta(days=1)
        from .events import search_window, wall_to_epoch  # noqa: PLC0415

        resp = await self.client.async_api_call(
            API_RECORD_SEARCH,
            {
                "channel": [f"CH{n}" for n in channels],
                **search_window(since.timestamp(), horizon.timestamp()),
                "record_type": 4294967295,
                "smart_region": [],
                "enable_smart_search": 0,
            },
        )
        first: dict[int, float] = {}
        latest = 0.0
        for group in self._extract_data(resp).get("record") or []:
            for rec in group if isinstance(group, list) else [group]:
                if not isinstance(rec, dict):
                    continue
                num = channel_num_from_str(str(rec.get("channel", "")), 0)
                try:
                    start = wall_to_epoch(rec["start_date"], rec["start_time"])
                except (KeyError, ValueError):
                    continue
                if num and (num not in first or start < first[num]):
                    first[num] = start
                try:
                    end = wall_to_epoch(rec["end_date"], rec["end_time"])
                    latest = max(latest, end)
                except (KeyError, ValueError):
                    pass
        # The newest recording ends "now" by the NVR's clock: far from HA's
        # now means the NVR's clock or time zone is off.
        self.clock_skew_s = latest - now.timestamp() if latest else None
        # A channel with nothing recorded in the window has no archive at all.
        for n in channels:
            log.archive_start[n] = first.get(n, now.timestamp())
        if (self.config_entry.options.get("journal_mode") or "count") == "nvr":
            removed = log.clear("gone")
            if removed:
                _LOGGER.debug("Journal: %d events without a recording removed", removed)

    async def _async_record_history_loop(self) -> None:
        since = datetime.now() - timedelta(hours=RECORD_HISTORY_HOURS)
        # Let the first refresh settle and the NVR's clock be learned first
        # (recordings come in its wall clock).
        await asyncio.sleep(20)
        for _ in range(30):
            if self.nvr_offset_s is not None:
                break
            await asyncio.sleep(1)
        while True:
            until = datetime.now()
            try:
                added = await self._async_record_history(since, until)
                self._rec_last_error = None
                if added:
                    _LOGGER.debug("Record history: %d event segments added", added)
                # Segments are written when they close: look back a while.
                since = until - timedelta(minutes=30)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - keep trying
                self._rec_last_error = str(err)
            await asyncio.sleep(RECORD_HISTORY_INTERVAL)

    async def _async_record_history(self, since: datetime, until: datetime) -> int:
        from .events import get_event_log, record_event_type  # noqa: PLC0415

        channels = [f"CH{n}" for n in sorted(self.channel_abilities)] or [
            f"CH{channel_num_from_str(ch.get('channel', '') if isinstance(ch, dict) else '', i + 1)}"
            for i, ch in enumerate(get_channel_list(self.data or {}))
        ]
        if not channels:
            return 0
        from .events import search_window, wall_to_epoch  # noqa: PLC0415

        resp = await self.client.async_api_call(
            API_RECORD_SEARCH,
            {
                "channel": channels,
                **search_window(since.timestamp(), until.timestamp()),
                "record_type": 4294967295,
                "smart_region": [],
                "enable_smart_search": 0,
            },
        )
        log = get_event_log(self.hass)
        added = 0
        for group in self._extract_data(resp).get("record") or []:
            for rec in group if isinstance(group, list) else [group]:
                if not isinstance(rec, dict):
                    continue
                kind = record_event_type(rec)
                if kind is None:
                    continue
                try:
                    start = wall_to_epoch(rec["start_date"], rec["start_time"])
                    end = wall_to_epoch(rec["end_date"], rec["end_time"])
                except (KeyError, ValueError):
                    continue
                num = channel_num_from_str(str(rec.get("channel", "")), 0)
                if num and log.add_record(num, kind, start, end):
                    added += 1
        return added

    def _all_channel_indexes(self) -> list[int]:
        """0-based indexes of every channel the NVR reports."""
        channels = get_channel_list(self.data or {})
        return [
            channel_num_from_str(ch.get("channel", "") if isinstance(ch, dict) else "", i + 1) - 1
            for i, ch in enumerate(channels)
        ] or list(range(16))

    async def _async_vhd_loop(self) -> bool:
        """Poll /API/AI/VhdLog/GetByIndex per channel by time window.

        Returns False when the NVR refuses this too (nothing more to try).
        """
        since = datetime.now() - timedelta(hours=VHD_BACKFILL_HOURS)
        while True:
            until = datetime.now()
            refused = 0
            channels = sorted(self.channel_abilities) or [
                channel_num_from_str(
                    ch.get("channel", "") if isinstance(ch, dict) else "", i + 1
                )
                for i, ch in enumerate(get_channel_list(self.data or {}))
            ]
            for number in channels:
                try:
                    resp = await self.client.async_api_call(
                        API_AI_VHD_GET,
                        {
                            "Chn": [number - 1],
                            "StartTime": since.strftime("%Y-%m-%d %H:%M:%S"),
                            "EndTime": until.strftime("%Y-%m-%d %H:%M:%S"),
                        },
                    )
                except RaySharpNVRApiError as err:
                    self._ai_last_error = str(err)
                    if err.error_code == "no_permission":
                        refused += 1
                    continue
                except (RaySharpNVRAuthError, RaySharpNVRConnectionError) as err:
                    self._ai_last_error = str(err)
                    if "404" in str(err):
                        refused += 1  # the firmware has no such API
                    continue
                items = self._extract_data(resp).get("SnapedObjInfo") or []
                if items:
                    self._dispatch_ai_records(items[-VHD_MAX_PER_POLL:])
            if channels and refused == len(channels):
                _LOGGER.info("NVR refuses the person/vehicle log too; AI polling stopped")
                return False
            if not refused:
                self._ai_last_error = None
            self._ai_source = "vhd"
            # Overlap a little: rows are written after the detection ends.
            since = until - timedelta(seconds=60)
            await asyncio.sleep(max(AI_POLL_INTERVAL, 15.0))

    async def _async_poll_ai_detections(self) -> None:
        """Fetch detections recorded since the previous poll and dispatch them.

        The result set is sorted oldest-first, so its size doubles as a cursor:
        rows added since the last poll sit at the end.
        """
        async with self.ai_search_lock:
            search = await self.client.async_api_call(
                API_AI_OBJECTS_SEARCH,
                {
                    "MsgId": None,
                    **_AI_SEARCH_WINDOW,
                    # Some firmware answers no_permission to Chn: [] ("all")
                    # yet accepts the same search with every channel listed.
                    "Chn": self._all_channel_indexes(),
                    "SortType": 1,
                    "Engine": 0,
                    "Type": _AI_OBJECT_TYPES,
                },
            )
            total = self._extract_data(search).get("Count")
            if not isinstance(total, int):
                return

            if self._ai_cursor is None:
                # First poll: start tracking from here, then populate each
                # camera from history so nothing waits for the next passer-by.
                self._ai_cursor = total
                _LOGGER.debug(
                    "AI detection polling armed at %d stored records", total
                )
                await self._async_backfill_channels()
                return
            if total < self._ai_cursor:
                # The NVR purged old rows, so every index shifted; re-baseline.
                _LOGGER.debug(
                    "AI record count dropped %d → %d, re-baselining",
                    self._ai_cursor, total,
                )
                self._ai_cursor = total
                return
            if total == self._ai_cursor:
                return

            new_rows = total - self._ai_cursor
            start = self._ai_cursor
            if new_rows > AI_POLL_MAX_BATCH:
                start = total - AI_POLL_MAX_BATCH
                _LOGGER.debug(
                    "AI poll: %d new detections, taking the newest %d",
                    new_rows, AI_POLL_MAX_BATCH,
                )
            records = await self.client.async_api_call(
                API_AI_OBJECTS_GET_BY_INDEX,
                {
                    "MsgId": None,
                    "Engine": 0,
                    "StartIndex": start,
                    "Count": min(new_rows, AI_POLL_MAX_BATCH),
                    "SimpleInfo": 0,
                    "WithObjectImage": 1,
                    "WithBackgroud": 0,
                },
            )
            self._ai_cursor = total

        items = self._extract_data(records).get("SnapedObjInfo") or []
        self._dispatch_ai_records(items)

    def _backfill_depth(self) -> int:
        """Return how many stored detections to replay per channel.

        Each channel keeps a separate history per object type, so filling the
        cards needs roughly history × types rows — a channel that only ever
        sees people simply has fewer to give.
        """
        history = self.config_entry.options.get(
            CONF_SNAPSHOT_HISTORY_COUNT, DEFAULT_SNAPSHOT_HISTORY_COUNT
        )
        try:
            history = int(history)
        except (TypeError, ValueError):
            history = DEFAULT_SNAPSHOT_HISTORY_COUNT
        return min(max(history, 1) * len(_AI_OBJECT_TYPES), AI_BACKFILL_MAX_PER_CHANNEL)

    async def _async_backfill_channels(self) -> None:
        """Replay each channel's most recent detections once, at startup.

        Caller already holds the search lock.  Searching per channel costs one
        round trip each but is the only way to give every camera a starting
        image: a single busy channel can own every recent record.
        """
        channels = get_channel_list(self.data or {})
        depth = self._backfill_depth()
        for index, channel in enumerate(channels):
            raw = channel.get("channel", "") if isinstance(channel, dict) else ""
            number = channel_num_from_str(raw, index + 1)
            try:
                search = await self.client.async_api_call(
                    API_AI_OBJECTS_SEARCH,
                    {
                        "MsgId": None,
                        **_AI_SEARCH_WINDOW,
                        "Chn": [number - 1],  # the NVR indexes channels from 0
                        "SortType": 1,
                        "Engine": 0,
                        "Type": _AI_OBJECT_TYPES,
                    },
                )
                count = self._extract_data(search).get("Count")
                if not isinstance(count, int) or count <= 0:
                    continue
                take = min(count, depth)
                records = await self.client.async_api_call(
                    API_AI_OBJECTS_GET_BY_INDEX,
                    {
                        "MsgId": None,
                        "Engine": 0,
                        "StartIndex": count - take,
                        "Count": take,
                        "SimpleInfo": 0,
                        "WithObjectImage": 1,
                        "WithBackgroud": 0,
                    },
                )
            except (RaySharpNVRAuthError, RaySharpNVRConnectionError) as err:
                _LOGGER.debug("AI backfill for CH%d failed: %s", number, err)
                continue
            self._dispatch_ai_records(
                self._extract_data(records).get("SnapedObjInfo") or []
            )

    def _update_record_flags(self, data: dict[str, Any]) -> None:
        """Keep the per-channel recording state the NVR reports in Event Check.

        channel_alarm[] entries are mostly status: record_flag, connect status.
        """
        for entry in data.get("alarm_list") or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("time"):
                from .events import learn_offset, set_nvr_offset  # noqa: PLC0415

                offset = learn_offset(entry["time"])
                if offset is not None and offset != self.nvr_offset_s:
                    _LOGGER.debug("NVR clock is UTC%+.1fh", offset / 3600)
                    self.nvr_offset_s = offset
                    set_nvr_offset(offset)
            for ch in entry.get("channel_alarm") or []:
                if not isinstance(ch, dict):
                    continue
                digits = "".join(c for c in str(ch.get("channel", "")) if c.isdigit())
                status = ch.get("camera_connect_status")
                if digits and isinstance(status, dict):
                    self.channel_abilities[int(digits)] = [
                        str(a) for a in (status.get("ability") or [])
                    ] + [str(a) for a in (status.get("intelligent_ability") or [])]
                if "record_flag" not in ch:
                    continue
                if digits:
                    self.record_flags[int(digits)] = _recording(ch.get("record_flag"))

    def _dispatch_ai_records(self, items: list[dict[str, Any]]) -> None:
        """Fire snapshot and alarm events for polled detection records.

        GetByIndex returns the same SnapedObjInfo rows the push path delivers,
        so the existing parser handles them unchanged.
        """
        if not items:
            return
        try:
            from . import _parse_snapshot_payload  # noqa: PLC0415
            from .const import EVENT_ALARM, EVENT_SNAPSHOT  # noqa: PLC0415
        except ImportError:
            _LOGGER.debug("Could not import event parsers — skipping AI dispatch")
            return

        payload = {"data": {"ai_snap_picture": {"SnapedObjInfo": items}}}
        for snap in _parse_snapshot_payload(payload):
            if not self._remember_snapshot(snap):
                continue
            self._ai_dispatched += 1
            self.hass.bus.async_fire(EVENT_SNAPSHOT, snap)
            if not _is_recent(snap.get("start_time")):
                # History replayed at startup feeds the image history only;
                # raising it as an alarm turned motion sensors on at boot.
                continue
            self.hass.bus.async_fire(
                EVENT_ALARM,
                {
                    "channel": snap.get("channel"),
                    "channel_str": snap.get("channel_str"),
                    "alarm_type": snap.get("alarm_type"),
                    "snap_id": snap.get("snap_id"),
                    "source": "ai_poll",
                },
            )
            _LOGGER.debug(
                "AI poll: %s on channel %s (snap %s)",
                snap.get("alarm_type"), snap.get("channel"), snap.get("snap_id"),
            )

    def _remember_snapshot(self, snap: dict[str, Any]) -> bool:
        """Return True the first time a snapshot id is seen.

        Guards against the polling path re-announcing a detection the NVR
        already pushed, on firmwares where both paths work.
        """
        key = (snap.get("channel"), snap.get("snap_id"))
        if key[1] is None:
            return True
        if key in self._seen_snaps_set:
            return False
        if len(self._seen_snaps) == self._seen_snaps.maxlen:
            self._seen_snaps_set.discard(self._seen_snaps[0])
        self._seen_snaps.append(key)
        self._seen_snaps_set.add(key)
        return True

    def ai_poll_diagnostics(self) -> dict[str, Any]:
        """Return AI polling state for diagnostics."""
        task = self._ai_task
        return {
            "running": bool(task and not task.done()),
            "cursor": self._ai_cursor,
            "source": getattr(self, "_ai_source", "objects"),
            "dispatched": self._ai_dispatched,
            "last_error": self._ai_last_error,
        }

    def _dispatch_event_check_data(self, response: dict[str, Any]) -> None:
        """Parse Event Check response and fire HA bus events.

        Lazy-imports the parsing helpers from __init__ to avoid circular imports
        at module load time.
        """
        try:
            from . import (  # noqa: PLC0415
                _parse_alarm_payload,
                _parse_doorbell_payload,
                _parse_snapshot_payload,
            )
            from .const import EVENT_ALARM, EVENT_DOORBELL, EVENT_SNAPSHOT  # noqa: PLC0415
        except ImportError:
            _LOGGER.debug("Could not import event parsers — skipping dispatch")
            return

        now = time.monotonic()
        for event_data in _parse_alarm_payload(response):
            key = (event_data.get("channel", 0), event_data.get("alarm_type", ""))
            if now - self._alarm_debounce.get(key, 0.0) < self._alarm_debounce_window:
                continue
            self._alarm_debounce[key] = now
            self.hass.bus.async_fire(EVENT_ALARM, event_data)

        for snap in _parse_snapshot_payload(response):
            if not self._remember_snapshot(snap):
                continue
            self.hass.bus.async_fire(EVENT_SNAPSHOT, snap)
            _LOGGER.debug(
                "Event Check: fired %s snapshot for channel %s",
                snap.get("alarm_type"),
                snap.get("channel"),
            )

        for doorbell_data in _parse_doorbell_payload(response):
            self.hass.bus.async_fire(EVENT_DOORBELL, doorbell_data)
            _LOGGER.debug(
                "Event Check: doorbell event channel=%s ringing=%s",
                doorbell_data.get("channel"),
                doorbell_data.get("ringing"),
            )



def _is_recent(start_time: Any, max_age_s: int = 300) -> bool:
    from .events import is_recent  # noqa: PLC0415

    return is_recent(start_time, max_age_s)


def _recording(flag: Any) -> bool:
    """record_flag is a bool on some firmware, {"r": "SR"} on others.

    In the dict form the value lists the active record types (letters such as
    S/R/M/A), empty when the channel is not recording.
    """
    if isinstance(flag, dict):
        return any(str(v).strip() for v in flag.values())
    if isinstance(flag, str):
        return flag.strip().lower() not in ("", "0", "false", "off", "no")
    return bool(flag)

"""The door panel as a classic intercom: ringing, one answers, the rest go quiet.

States: idle → ringing → talking (by one client) → idle; a ring nobody
answers within RING_S ends as a missed call. Every screen (browser, wall
monitor, the HA app in front) follows the state over the websocket and plays
its own ringtone; phones in the pocket get ONE push with a tag, cleared on all
of them as soon as somebody answers or the ring ends, and a quiet "missed call"
push when nobody did. Only one client can talk at a time.

Video for all of them goes over WebRTC through go2rtc, which keeps a single
connection to the door panel however many screens watch.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, EVENT_DOORBELL

_LOGGER = logging.getLogger(__name__)

RING_S = 30          # a ring nobody answers is a missed call after this long
TALK_MAX_S = 300     # a call ends by itself after this long
DETACH_GRACE_S = 15  # the answering screen went away: end the call after this
DASHBOARD = "/raysharp-doorbell"
KEY = f"{DOMAIN}_intercoms"

IDLE, RINGING, TALKING = "idle", "ringing", "talking"
STATES = [IDLE, RINGING, TALKING]

CONF_NOTIFY = "intercom_notify"      # comma separated notify services; empty = every mobile_app
CONF_SOUND = "intercom_sound"        # iOS sound name
CONF_DASHBOARD = "intercom_dashboard"


def intercoms(hass: HomeAssistant) -> dict[str, Intercom]:
    return hass.data.setdefault(KEY, {})


def listen_all(hass: HomeAssistant, cb: Callable[[dict], None]) -> Callable[[], None]:
    """Changes of every intercom, also ones created after this call (reloads)."""
    cbs: list = hass.data.setdefault(f"{KEY}_listeners", [])
    cbs.append(cb)

    def _remove() -> None:
        if cb in cbs:
            cbs.remove(cb)

    return _remove


class Intercom:
    def __init__(self, hass: HomeAssistant, entry, mac: str, channel: int, name: str) -> None:
        self.hass = hass
        self.entry = entry
        self.mac = mac
        self.channel = channel
        self.name = name
        self.state = IDLE
        self.ring_at: float | None = None
        self.answered_by: dict[str, Any] | None = None
        self.answered_at: float | None = None
        self.last_missed: float | None = None
        self._ring_timer: Callable | None = None
        self._talk_timer: Callable | None = None
        self._detach_timer: Callable | None = None
        self._listeners: list[Callable[[dict], None]] = []
        self._pushed = False
        self.sensor: IntercomSensor | None = None

    # --- wiring ---------------------------------------------------------------
    @property
    def key(self) -> str:
        return f"{self.entry.entry_id}:{self.channel}"

    @property
    def tag(self) -> str:
        return f"raysharp_intercom_{self.mac.replace(':', '')}_{self.channel}"

    def _opt(self, key: str, default: Any = "") -> Any:
        return self.entry.options.get(key, default) or default

    @property
    def dashboard(self) -> str:
        path = str(self._opt(CONF_DASHBOARD, DASHBOARD)).strip()
        return path if path.startswith("/") else f"/{path}"

    def entity(self, domain: str, suffix: str) -> str | None:
        reg = er.async_get(self.hass)
        return reg.async_get_entity_id(domain, DOMAIN, f"{self.mac}_ch{self.channel}_{suffix}")

    def view(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "entry_id": self.entry.entry_id,
            "channel": self.channel,
            "name": self.name,
            "state": self.state,
            "ring_at": self.ring_at,
            "answered_by": self.answered_by,
            "answered_at": self.answered_at,
            "last_missed": self.last_missed,
            "camera": self.entity("camera", "camera"),
            "snapshot": self.entity("image", "doorbell_snapshot"),
            "dashboard": self.dashboard,
            "ring_s": RING_S,
        }

    def listen(self, cb: Callable[[dict], None]) -> Callable[[], None]:
        self._listeners.append(cb)

        def _remove() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)

        return _remove

    @callback
    def _changed(self) -> None:
        v = self.view()
        for cb in [*self._listeners, *self.hass.data.get(f"{KEY}_listeners", [])]:
            try:
                cb(v)
            except Exception:  # noqa: BLE001 - one broken socket must not stop the rest
                _LOGGER.debug("intercom listener failed", exc_info=True)
        if self.sensor is not None:
            self.sensor.async_write_ha_state()

    def _cancel(self, name: str) -> None:
        unsub = getattr(self, name)
        if unsub is not None:
            unsub()
            setattr(self, name, None)

    # --- events ---------------------------------------------------------------
    @callback
    def on_doorbell(self, event: Event) -> None:
        data = event.data or {}
        if data.get("channel") != self.channel:
            return
        if data.get("ringing", True):
            self.ring()

    @callback
    def ring(self) -> None:
        if self.state == TALKING:
            return  # pressed again while we already talk
        first = self.state != RINGING
        self.state = RINGING
        self.ring_at = time.time()
        self._cancel("_ring_timer")
        self._ring_timer = async_call_later(self.hass, RING_S, self._ring_timeout)
        self._changed()
        if first:
            self.hass.async_create_task(self._push_ring())

    @callback
    def _ring_timeout(self, _now: datetime) -> None:
        self._ring_timer = None
        if self.state != RINGING:
            return
        self.state = IDLE
        self.last_missed = self.ring_at
        self._changed()
        self.hass.async_create_task(self._push_missed())

    @callback
    def answer(self, client: dict[str, Any]) -> dict[str, Any]:
        """Claim the call (or start talking unprompted). Busy if someone else talks."""
        if self.state == TALKING and self.answered_by and self.answered_by.get("id") != client.get("id"):
            raise BusyError(self.answered_by)
        was_ringing = self.state == RINGING
        self.state = TALKING
        self.answered_by = client
        self.answered_at = time.time()
        self._cancel("_ring_timer")
        self._cancel("_detach_timer")
        self._cancel("_talk_timer")
        self._talk_timer = async_call_later(self.hass, TALK_MAX_S, self._talk_timeout)
        self._changed()
        if was_ringing or self._pushed:
            self.hass.async_create_task(self._clear_push())
        return self.view()

    @callback
    def hangup(self, client_id: str | None = None, *, force: bool = False) -> dict[str, Any]:
        if self.state == TALKING and (force or not self.answered_by or self.answered_by.get("id") == client_id):
            self._end()
        elif self.state == RINGING and force:
            self._cancel("_ring_timer")
            self.state = IDLE
            self._changed()
            self.hass.async_create_task(self._clear_push())
        return self.view()

    @callback
    def detached(self, client_id: str) -> None:
        """The answering screen's websocket closed: give it a moment, then end."""
        if self.state != TALKING or not self.answered_by or self.answered_by.get("id") != client_id:
            return
        self._cancel("_detach_timer")
        self._detach_timer = async_call_later(
            self.hass, DETACH_GRACE_S, lambda _n: self._detach_expired(client_id)
        )

    @callback
    def _detach_expired(self, client_id: str) -> None:
        self._detach_timer = None
        if self.state == TALKING and self.answered_by and self.answered_by.get("id") == client_id:
            self._end()

    @callback
    def _talk_timeout(self, _now: datetime) -> None:
        self._talk_timer = None
        if self.state == TALKING:
            self._end()

    @callback
    def _end(self) -> None:
        for t in ("_ring_timer", "_talk_timer", "_detach_timer"):
            self._cancel(t)
        self.state = IDLE
        self.answered_by = None
        self.answered_at = None
        self._changed()

    @callback
    def shutdown(self) -> None:
        for t in ("_ring_timer", "_talk_timer", "_detach_timer"):
            self._cancel(t)
        self._listeners.clear()

    # --- phones ---------------------------------------------------------------
    def _targets(self) -> list[str]:
        raw = str(self._opt(CONF_NOTIFY, "")).strip()
        services = self.hass.services.async_services().get("notify", {})
        if raw:
            names = [s.strip().removeprefix("notify.") for s in raw.split(",") if s.strip()]
            return [n for n in names if n in services]
        return sorted(n for n in services if n.startswith("mobile_app_"))

    async def _notify(self, message: str, data: dict[str, Any], title: str | None = None) -> None:
        for target in self._targets():
            payload: dict[str, Any] = {"message": message, "data": data}
            if title:
                payload["title"] = title
            try:
                await self.hass.services.async_call("notify", target, payload, blocking=False)
            except Exception as err:  # noqa: BLE001 - one phone must not stop the others
                _LOGGER.warning("intercom push to %s failed: %s", target, err)

    def _image(self) -> str | None:
        snap = self.entity("image", "doorbell_snapshot")
        return f"/api/image_proxy/{snap}?ts={int(time.time())}" if snap else None

    async def _push_ring(self) -> None:
        self._pushed = True
        data: dict[str, Any] = {
            "tag": self.tag,
            "group": "intercom",
            "url": self.dashboard,          # iOS: tap opens the intercom
            "clickAction": self.dashboard,  # Android
            "actions": [{"action": "URI", "title": "Ответить", "uri": self.dashboard}],
            "push": {
                "sound": {"name": self._opt(CONF_SOUND, "default"), "critical": 0, "volume": 1.0},
                "interruption-level": "time-sensitive",
            },
            # Android: its own loud channel, shown on top, gone when answered.
            "channel": "Домофон",
            "importance": "high",
            "priority": "high",
            "ttl": 0,
            "timeout": RING_S + 5,
        }
        img = self._image()
        if img:
            data["image"] = img
        await self._notify("Звонок у калитки — нажмите, чтобы ответить", data, f"🔔 {self.name}")

    async def _clear_push(self) -> None:
        self._pushed = False
        await self._notify("clear_notification", {"tag": self.tag})

    async def _push_missed(self) -> None:
        await self._clear_push()
        when = datetime.fromtimestamp(self.ring_at or time.time()).strftime("%H:%M")
        data: dict[str, Any] = {
            "tag": f"{self.tag}_missed",
            "group": "intercom",
            "url": self.dashboard,
            "clickAction": self.dashboard,
            "push": {"interruption-level": "passive", "sound": "none"},
            "channel": "Домофон — пропущенные",
            "importance": "low",
        }
        img = self._image()
        if img:
            data["image"] = img
        await self._notify(f"Пропущенный звонок в {when}", data, self.name)


class BusyError(Exception):
    def __init__(self, who: dict[str, Any]) -> None:
        super().__init__("busy")
        self.who = who


class IntercomSensor(SensorEntity):
    """sensor.<panel>_intercom: idle / ringing / talking, for automations
    (e.g. a gate relay or lights when somebody rings)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATES
    _attr_should_poll = False
    _attr_icon = "mdi:doorbell-video"
    _attr_has_entity_name = True

    def __init__(self, intercom: Intercom, device_info) -> None:
        self._ic = intercom
        self._attr_unique_id = f"{intercom.mac}_ch{intercom.channel}_intercom"
        self._attr_name = "Домофон"
        self._attr_device_info = device_info
        intercom.sensor = self

    @property
    def native_value(self) -> str:
        return self._ic.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        v = self._ic.view()
        who = v["answered_by"] or {}
        return {
            "answered_by": who.get("user"),
            "answered_on": who.get("label"),
            "ring_at": v["ring_at"],
            "last_missed": v["last_missed"],
            "dashboard": v["dashboard"],
        }


def setup_intercom(hass: HomeAssistant, entry, coordinator, mac: str) -> Intercom | None:
    """Create the intercom for the channel named as a door panel in the options."""
    from .const import CONF_TALK_CHANNEL, CONF_TALK_HOST  # noqa: PLC0415

    channel = int(entry.options.get(CONF_TALK_CHANNEL) or 0)
    if not channel or not entry.options.get(CONF_TALK_HOST):
        return None
    ic = Intercom(hass, entry, mac, channel, "Домофон")
    intercoms(hass)[ic.key] = ic
    entry.async_on_unload(hass.bus.async_listen(EVENT_DOORBELL, ic.on_doorbell))

    @callback
    def _unload() -> None:
        ic.shutdown()
        intercoms(hass).pop(ic.key, None)

    entry.async_on_unload(_unload)
    return ic

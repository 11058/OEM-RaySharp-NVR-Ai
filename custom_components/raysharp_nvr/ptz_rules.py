"""PTZ scenarios: when something happens in Home Assistant, turn a camera to a
preset and, if asked, let it follow the person there.

    «При нажатии «Шлагбаум» → CAM01 на «Шлагбаум», вести человека»

A rule: trigger entity (any HA entity), optional state it must change to,
camera, preset number, and whether to start the camera's own human tracking
once it has arrived. The camera's idle return (watch mode) brings it back to
its home preset afterwards. Rules live in HA storage and are edited in the
camera window of the «Видео» panel.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORE_KEY = f"{DOMAIN}.ptz_rules"
KEY = f"{DOMAIN}_ptz_rules"
ARRIVE_S = 3       # time for the camera to reach the preset before tracking starts
DEBOUNCE_S = 5     # one trigger burst = one move


class PtzRules:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.rules: list[dict[str, Any]] = []
        self._store: Store = Store(hass, 1, STORE_KEY)
        self._unsub = None
        self._last: dict[str, float] = {}
        self.log: list[dict[str, Any]] = []  # last runs, for the panel
        # camera -> {"no", "seconds"}: the idle-return preset we set (PTZ/Get does not report it)
        self.home: dict[str, dict[str, Any]] = {}
        self._timers: dict[str, Any] = {}

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.rules = [r for r in data.get("rules", []) if isinstance(r, dict) and r.get("id")]
        self.home = data.get("home") or {}
        self._watch()
        async_dispatcher_connect(self.hass, f"{DOMAIN}_ptz_activity", self._activity)

    def _save(self) -> None:
        self._store.async_delay_save(lambda: {"rules": self.rules, "home": self.home}, 2)

    @callback
    def _watch(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        ents = sorted({r["trigger"] for r in self.rules if r.get("enabled", True) and r.get("trigger")})
        if ents:
            self._unsub = async_track_state_change_event(self.hass, ents, self._changed)

    @callback
    def _changed(self, event: Event) -> None:
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        if new is None or new.state in ("unavailable", "unknown"):
            return
        if old is None or old.state == "unavailable":
            return  # an entity appearing / coming back (restart, reload) is not an event
        for rule in self.rules:
            if not rule.get("enabled", True) or rule.get("trigger") != new.entity_id:
                continue
            want = str(rule.get("to") or "").strip()
            if want and new.state != want:
                continue
            if not want and old is not None and old.state == new.state:
                continue
            now = time.monotonic()
            if now - self._last.get(rule["id"], 0) < DEBOUNCE_S:
                continue
            self._last[rule["id"]] = now
            self.hass.async_create_task(self._run(rule))

    async def _run(self, rule: dict[str, Any]) -> None:
        from .websocket_api import _raysharp_camera  # noqa: PLC0415

        cam = _raysharp_camera(self.hass, rule["camera"])
        entry = {"rule": rule["id"], "ts": time.time(), "ok": False, "text": ""}
        try:
            if cam is None:
                raise ValueError("камера не найдена")
            ch = cam._channel_num  # noqa: SLF001
            await cam.coordinator.async_preset(ch, "call", int(rule["preset"]))
            if rule.get("track"):
                await asyncio.sleep(ARRIVE_S)
                await cam.coordinator.async_ptz_extra(ch, "track_human")
            entry["ok"] = True
            entry["text"] = "камера повернута" + (", ведёт человека" if rule.get("track") else "")
        except Exception as err:  # noqa: BLE001 - shown in the panel
            entry["text"] = str(err)
            if "no_permission" in entry["text"]:
                entry["text"] = "нет права PTZ у учётки регистратора"
            _LOGGER.warning("PTZ scenario %s failed: %s", rule.get("id"), err)
        self.log = [entry, *self.log][:20]

    def set_home(self, camera: str, no: int | None, seconds: int) -> None:
        if no:
            self.home[camera] = {"no": int(no), "seconds": int(seconds),
                                 "paused": bool((self.home.get(camera) or {}).get("paused"))}
        else:
            self.home.pop(camera, None)
            self._cancel(camera)
        self._save()

    def set_paused(self, camera: str, paused: bool) -> None:
        """«Не возвращаться» / «Возвращать»."""
        if camera in self.home:
            self.home[camera]["paused"] = paused
            self._save()
            if paused:
                self._cancel(camera)

    # --- idle return done by HA itself ------------------------------------------
    # The camera's own watch mode is set too, but it did not bring CAM01 back on
    # Береговая; HA counts the idle time after every move it sent and calls the
    # home preset. Moves made from the NVR's own apps are not seen here.
    def _cancel(self, camera: str) -> None:
        t = self._timers.pop(camera, None)
        if t:
            t()

    @callback
    def _activity(self, coordinator, channel: int) -> None:
        from .websocket_api import _raysharp_camera  # noqa: PLC0415

        for camera, home in self.home.items():
            cam = _raysharp_camera(self.hass, camera)
            if cam is None or cam.coordinator is not coordinator or cam._channel_num != channel:  # noqa: SLF001
                continue
            self._cancel(camera)
            if home.get("paused") or not home.get("no"):
                return

            @callback
            def _fire(_now, camera=camera) -> None:
                self._timers.pop(camera, None)
                self.hass.async_create_task(self._go_home(camera))

            self._timers[camera] = async_call_later(self.hass, int(home.get("seconds", 60)), _fire)
            return

    async def _go_home(self, camera: str) -> None:
        from .websocket_api import _raysharp_camera  # noqa: PLC0415

        home = self.home.get(camera) or {}
        cam = _raysharp_camera(self.hass, camera)
        if cam is None or home.get("paused") or not home.get("no"):
            return
        try:
            await cam.coordinator.async_preset(cam._channel_num, "call", int(home["no"]), quiet=True)  # noqa: SLF001
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("PTZ idle return of %s failed: %s", camera, err)

    # --- editing ----------------------------------------------------------------
    def save_rule(self, rule: dict[str, Any]) -> dict[str, Any]:
        rule = {k: rule[k] for k in ("id", "camera", "trigger", "to", "preset", "track", "enabled") if k in rule}
        rule.setdefault("id", secrets.token_hex(4))
        rule.setdefault("enabled", True)
        self.rules = [r for r in self.rules if r["id"] != rule["id"]] + [rule]
        self._save()
        self._watch()
        return rule

    def delete_rule(self, rule_id: str) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r["id"] != rule_id]
        self._save()
        self._watch()
        return len(self.rules) != before

    async def test(self, rule_id: str) -> dict[str, Any]:
        rule = next((r for r in self.rules if r["id"] == rule_id), None)
        if rule is None:
            raise ValueError("сценарий не найден")
        await self._run(rule)
        return self.log[0]


async def async_setup_rules(hass: HomeAssistant) -> PtzRules:
    rules = hass.data.get(KEY)
    if rules is None:
        rules = hass.data[KEY] = PtzRules(hass)
        await rules.async_load()
    return rules

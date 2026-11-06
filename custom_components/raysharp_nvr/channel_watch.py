"""A notification in HA when a camera that was working drops off the NVR.

Only channels seen online during this run are watched, so the NVR's empty
channels never raise anything. Two polls in a row are needed either way, so a
single missed answer does not flap the notification.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

CONFIRM_POLLS = 2


def channel_states(channels: list[dict[str, Any]]) -> dict[int, tuple[str, bool]]:
    """num -> (name, ok). ok = online and with a picture."""
    out: dict[int, tuple[str, bool]] = {}
    for i, ch in enumerate(channels):
        digits = "".join(c for c in str(ch.get("channel", "")) if c.isdigit())
        num = int(digits) if digits else i + 1
        ok = str(ch.get("connect_status", "")).lower() == "online" and not ch.get("videoloss")
        out[num] = (str(ch.get("channel_name") or f"CH{num}"), ok)
    return out


class ChannelWatch:
    def __init__(self, hass: HomeAssistant, entry_id: str, title: str) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.title = title
        self.seen_ok: set[int] = set()
        self.streak: dict[int, int] = {}
        self.raised: set[int] = set()

    def _nid(self, num: int) -> str:
        return f"{DOMAIN}_{self.entry_id}_ch{num}"

    def update(self, states: dict[int, tuple[str, bool]]) -> None:
        for num, (name, ok) in states.items():
            if ok:
                self.seen_ok.add(num)
                self.streak[num] = 0
                if num in self.raised:
                    self.raised.discard(num)
                    persistent_notification.async_dismiss(self.hass, self._nid(num))
                continue
            if num not in self.seen_ok:
                continue  # never worked in this run: an empty channel
            self.streak[num] = self.streak.get(num, 0) + 1
            if self.streak[num] >= CONFIRM_POLLS and num not in self.raised:
                self.raised.add(num)
                persistent_notification.async_create(
                    self.hass,
                    f"Камера **{name}** (CH{num}) на регистраторе «{self.title}» не в сети "
                    "или потеряла видеосигнал. Запись с неё не идёт. Проверьте питание, "
                    "кабель или PoE-порт коммутатора.",
                    title=f"Видео: нет потока с {name}",
                    notification_id=self._nid(num),
                )


def attach(hass: HomeAssistant, entry, coordinator, get_channels: Callable) -> Callable:
    watch = ChannelWatch(hass, entry.entry_id, entry.title)

    @callback
    def _changed() -> None:
        if coordinator.last_update_success and coordinator.data:
            watch.update(channel_states(get_channels(coordinator.data)))

    return coordinator.async_add_listener(_changed)

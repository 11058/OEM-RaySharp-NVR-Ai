"""PTZ scenario trigger matching (no HA needed: states are simple objects)."""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "raysharp_nvr"


def _load():
    try:
        import homeassistant  # noqa: F401
    except ImportError:
        ha = types.ModuleType("homeassistant")
        core = types.ModuleType("homeassistant.core")
        core.Event = object
        core.HomeAssistant = object
        core.callback = lambda f: f
        helpers = types.ModuleType("homeassistant.helpers")
        ev = types.ModuleType("homeassistant.helpers.event")
        ev.async_track_state_change_event = lambda *a, **k: (lambda: None)
        stor = types.ModuleType("homeassistant.helpers.storage")
        stor.Store = lambda *a, **k: types.SimpleNamespace(async_delay_save=lambda *a: None)
        for n, m in {"homeassistant": ha, "homeassistant.core": core, "homeassistant.helpers": helpers,
                     "homeassistant.helpers.event": ev, "homeassistant.helpers.storage": stor}.items():
            sys.modules.setdefault(n, m)
    pkg = types.ModuleType("rsn")
    pkg.__path__ = [str(ROOT)]
    sys.modules["rsn"] = pkg
    const = types.ModuleType("rsn.const")
    const.DOMAIN = "raysharp_nvr"
    sys.modules["rsn.const"] = const
    spec = importlib.util.spec_from_file_location("rsn.ptz_rules", ROOT / "ptz_rules.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()


class _S:
    def __init__(self, eid, state):
        self.entity_id, self.state = eid, state


class _E:
    def __init__(self, old, new):
        self.data = {"old_state": old, "new_state": new}


def _rules(rule):
    ran = []
    hass = types.SimpleNamespace(async_create_task=lambda c: (ran.append(1), c.close()))
    r = M.PtzRules.__new__(M.PtzRules)
    r.hass, r.rules, r._last, r.log, r._unsub = hass, [rule], {}, [], None
    return r, ran


def test_button_press_fires_once_and_restart_does_not():
    r, ran = _rules({"id": "a", "trigger": "button.shlagbaum", "to": "", "camera": "camera.c", "preset": 2})
    r._changed(_E(None, _S("button.shlagbaum", "2026-11-08T10:00:00")))          # entity added
    r._changed(_E(_S("button.shlagbaum", "unavailable"), _S("button.shlagbaum", "x")))  # back after restart
    assert ran == []
    r._changed(_E(_S("button.shlagbaum", "unknown"), _S("button.shlagbaum", "t1")))  # first ever press
    r._changed(_E(_S("button.shlagbaum", "t1"), _S("button.shlagbaum", "t2")))       # within debounce
    assert ran == [1]


def test_binary_sensor_only_on():
    r, ran = _rules({"id": "b", "trigger": "binary_sensor.gate", "to": "on", "camera": "camera.c", "preset": 1})
    r._changed(_E(_S("binary_sensor.gate", "on"), _S("binary_sensor.gate", "off")))
    assert ran == []
    r._changed(_E(_S("binary_sensor.gate", "off"), _S("binary_sensor.gate", "on")))
    assert ran == [1]


def test_disabled_rule_ignored():
    r, ran = _rules({"id": "c", "trigger": "button.x", "to": "", "camera": "camera.c", "preset": 1, "enabled": False})
    r._changed(_E(_S("button.x", "t1"), _S("button.x", "t2")))
    assert ran == []
    assert asyncio  # keep import

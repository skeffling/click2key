"""Keyboard-mapping config: persistence + parsing + MyWhoosh presets.

A button's binding has three parts: the key it sends, how many times to
send it on a single press, and (globally) how long to wait between
repeats. Repeats let one puck press shift two or three gears in a row.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from pynput.keyboard import Key, KeyCode

from .click_v2 import Button

log = logging.getLogger(__name__)


# Names we accept (and emit) for special keys via the config file and the
# config dialog. Lowercase. Single characters are treated as KeyCode chars.
_SPECIAL_KEYS: dict[str, Key] = {
    "up": Key.up,
    "down": Key.down,
    "left": Key.left,
    "right": Key.right,
    "space": Key.space,
    "enter": Key.enter,
    "return": Key.enter,
    "tab": Key.tab,
    "esc": Key.esc,
    "escape": Key.esc,
    "backspace": Key.backspace,
    "shift": Key.shift,
    "ctrl": Key.ctrl,
    "alt": Key.alt,
    "cmd": Key.cmd,
    **{f"f{i}": getattr(Key, f"f{i}") for i in range(1, 13)},
}


MAX_REPEATS = 3
DEFAULT_DELAY_MS = 60


def parse_key(value: str) -> Key | KeyCode | None:
    s = (value or "").strip().lower()
    if not s:
        return None
    if s in _SPECIAL_KEYS:
        return _SPECIAL_KEYS[s]
    if len(s) == 1:
        return KeyCode.from_char(s)
    return None


def format_key(k: Key | KeyCode | None) -> str:
    if k is None:
        return ""
    if isinstance(k, KeyCode):
        return k.char or ""
    # Key enum — name is e.g. "up", "f1", "esc".
    name = getattr(k, "name", "")
    if name:
        return name
    # Some pynput Key reprs are like "Key.up"; fall back to repr split.
    return str(k).removeprefix("Key.")


# MyWhoosh known shortcuts shown as presets in the config dropdown.
# (Display label, key string accepted by parse_key()).
MYWHOOSH_PRESETS: list[tuple[str, str]] = [
    ("Shift up (K)",                    "k"),
    ("Shift down (I)",                  "i"),
    ("Navigate left (←)",               "left"),
    ("Navigate right (→)",              "right"),
    ("Navigate up (↑)",                 "up"),
    ("Navigate down (↓)",               "down"),
    ("Steer left (A)",                  "a"),
    ("Steer right (D)",                 "d"),
    ("Toggle minimal UI (U)",           "u"),
    ("Hide all controls — HD only (H)", "h"),
    ("Peace (1)",                       "1"),
    ("Wave (2)",                        "2"),
    ("Fist bump (3)",                   "3"),
    ("Dab (4)",                         "4"),
    ("Elbow flick (5)",                 "5"),
    ("Toast (6)",                       "6"),
    ("Thumbs up (7)",                   "7"),
]


DEFAULTS_BY_BUTTON: dict[Button, str] = {
    Button.SHIFT_UP:   "k",
    Button.SHIFT_DOWN: "i",
    Button.NAV_UP:     "up",
    Button.NAV_DOWN:   "down",
    Button.NAV_LEFT:   "left",
    Button.NAV_RIGHT:  "right",
    Button.A:          "a",
    Button.B:          "b",
    Button.Y:          "y",
    Button.Z:          "z",
}


@dataclass
class KeymapConfig:
    mapping: dict[Button, Key | KeyCode] = field(default_factory=dict)
    repeats: dict[Button, int] = field(default_factory=dict)
    delay_ms: int = DEFAULT_DELAY_MS

    def repeats_for(self, button: Button) -> int:
        return self.repeats.get(button, 1)


def _config_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "click2key"
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "click2key"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "click2key"


def config_path() -> Path:
    return _config_dir() / "keymap.json"


def _clamp_repeats(value: object) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1
    return max(1, min(MAX_REPEATS, n))


def load_keymap() -> KeymapConfig:
    """Load mapping + repeats + delay from disk. Falls back to defaults.

    Accepts two schemas for backwards compatibility:
        flat:        {"shift_up": "k", ...}
        structured:  {"delay_ms": 80,
                      "shift_up": {"key": "k", "repeats": 2}, ...}
    """
    path = config_path()
    raw: dict[str, object] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text())
        except Exception:
            log.exception("Failed to read keymap %s; using defaults", path)

    delay_ms = DEFAULT_DELAY_MS
    raw_delay = raw.get("delay_ms")
    if isinstance(raw_delay, (int, float)):
        delay_ms = max(0, int(raw_delay))

    mapping: dict[Button, Key | KeyCode] = {}
    repeats: dict[Button, int] = {}
    for button in Button:
        entry = raw.get(button.value, DEFAULTS_BY_BUTTON.get(button, ""))
        if isinstance(entry, dict):
            key_str = str(entry.get("key", ""))
            rep = _clamp_repeats(entry.get("repeats", 1))
        else:
            key_str = str(entry)
            rep = 1
        parsed = parse_key(key_str)
        if parsed is not None:
            mapping[button] = parsed
        repeats[button] = rep
    return KeymapConfig(mapping=mapping, repeats=repeats, delay_ms=delay_ms)


def save_keymap(config: KeymapConfig) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    out: dict[str, object] = {"delay_ms": int(config.delay_ms)}
    for button in Button:
        entry: dict[str, object] = {"key": format_key(config.mapping.get(button))}
        rep = _clamp_repeats(config.repeats.get(button, 1))
        if rep != 1:
            entry["repeats"] = rep
        out[button.value] = entry
    path.write_text(json.dumps(out, indent=2))
    log.info("Saved keymap to %s", path)

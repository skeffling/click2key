"""Keyboard-mapping config: persistence + parsing + MyWhoosh presets."""

from __future__ import annotations

import json
import logging
import os
import sys
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
    # Right puck arrows → keyboard arrows (matches MyWhoosh's nav defaults).
    Button.NAV_UP:     "up",
    Button.NAV_DOWN:   "down",
    Button.NAV_LEFT:   "left",
    Button.NAV_RIGHT:  "right",
    # Left puck colored buttons → reasonable starting points; user can remap.
    # MyWhoosh has H=toggle UI, A=steer left, D=steer right, T=tuck, U=u-turn,
    # 1-7=emotes. Defaulting to the colored letters themselves keeps things
    # predictable; rebind in Configure keys… to taste.
    Button.A:          "a",
    Button.B:          "b",
    Button.Y:          "y",
    Button.Z:          "z",
}


def _config_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "clickwhoosh"
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "clickwhoosh"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "clickwhoosh"


def config_path() -> Path:
    return _config_dir() / "keymap.json"


def load_keymap() -> dict[Button, Key | KeyCode]:
    """Load mapping from config file, falling back to MyWhoosh defaults."""
    path = config_path()
    raw: dict[str, str] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text())
        except Exception:
            log.exception("Failed to read keymap %s; using defaults", path)
    mapping: dict[Button, Key | KeyCode] = {}
    for button in Button:
        key_str = raw.get(button.value, DEFAULTS_BY_BUTTON.get(button, ""))
        parsed = parse_key(key_str)
        if parsed is not None:
            mapping[button] = parsed
    return mapping


def save_keymap(mapping: dict[Button, Key | KeyCode]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = {b.value: format_key(k) for b, k in mapping.items()}
    path.write_text(json.dumps(serializable, indent=2))
    log.info("Saved keymap to %s", path)

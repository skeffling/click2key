"""Keyboard-simulation output. Press/release keys when buttons are pressed.

Used as an alternative to the MyWhoosh Link TCP server: synthesizes keyboard
input to whichever app currently has focus. Requires the focused window to
be MyWhoosh for the shifts to count.

macOS: first run will prompt for Accessibility permission. Without it,
key events are silently dropped by the OS.
"""

from __future__ import annotations

import logging

from pynput.keyboard import Controller, Key, KeyCode

from .click_v2 import Button, ButtonEvent

log = logging.getLogger(__name__)


# Defaults derived from BikeControl's MyWhoosh mapping
# (lib/utils/keymap/apps/my_whoosh.dart): K = shift up, I = shift down.
# Arrows on the pucks map to the matching keyboard arrows.
DEFAULT_MAPPING: dict[Button, Key | KeyCode] = {
    Button.SHIFT_UP:    KeyCode.from_char("k"),
    Button.SHIFT_DOWN:  KeyCode.from_char("i"),
    Button.NAV_UP:      Key.up,
    Button.NAV_DOWN:    Key.down,
    Button.NAV_LEFT:    Key.left,
    Button.NAV_RIGHT:   Key.right,
}


class KeyboardOutput:
    def __init__(self, mapping: dict[Button, Key | KeyCode] | None = None) -> None:
        self._kb = Controller()
        self._mapping = mapping if mapping is not None else dict(DEFAULT_MAPPING)

    def send(self, event: ButtonEvent) -> None:
        if event.button is None:
            return
        key = self._mapping.get(event.button)
        if key is None:
            return
        try:
            if event.is_down:
                self._kb.press(key)
            else:
                self._kb.release(key)
        except Exception:
            log.exception("Keyboard send failed for %s", event.button)

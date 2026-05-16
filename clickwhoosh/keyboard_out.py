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
from .keymap import load_keymap

log = logging.getLogger(__name__)


class KeyboardOutput:
    def __init__(self, mapping: dict[Button, Key | KeyCode] | None = None) -> None:
        self._kb = Controller()
        self._mapping = mapping if mapping is not None else load_keymap()

    def set_mapping(self, mapping: dict[Button, Key | KeyCode]) -> None:
        self._mapping = dict(mapping)

    def get_mapping(self) -> dict[Button, Key | KeyCode]:
        return dict(self._mapping)

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

"""Keyboard-simulation output. Press/release keys when buttons are pressed.

Synthesizes keyboard input to whichever app currently has focus. macOS
requires Accessibility permission; without it, key events are silently
dropped by the OS.

Per-button "repeats" (1, 2, or 3) let a single puck press emit the key
multiple times — handy for shifting two gears in one click. With
repeats > 1 the down event triggers N press/release cycles separated
by KeymapConfig.delay_ms; the matching up event is ignored.
"""

from __future__ import annotations

import asyncio
import logging

from pynput.keyboard import Controller, Key, KeyCode

from .click_v2 import Button, ButtonEvent
from .keymap import KeymapConfig, load_keymap

log = logging.getLogger(__name__)


class KeyboardOutput:
    def __init__(self, config: KeymapConfig | None = None) -> None:
        self._kb = Controller()
        self._config = config if config is not None else load_keymap()

    def set_config(self, config: KeymapConfig) -> None:
        self._config = config

    def get_config(self) -> KeymapConfig:
        return self._config

    async def send(self, event: ButtonEvent) -> None:
        if event.button is None:
            return
        key = self._config.mapping.get(event.button)
        if key is None:
            return
        repeats = self._config.repeats_for(event.button)
        try:
            if repeats > 1:
                # Fire N press/release pairs as a background task so the
                # bridge loop keeps consuming events; release is ignored.
                if event.is_down:
                    asyncio.create_task(self._fire_repeats(key, repeats))
                return
            if event.is_down:
                self._kb.press(key)
            else:
                self._kb.release(key)
        except Exception:
            log.exception("Keyboard send failed for %s", event.button)

    async def _fire_repeats(self, key: Key | KeyCode, repeats: int) -> None:
        delay_s = max(0, self._config.delay_ms) / 1000
        for i in range(repeats):
            try:
                self._kb.press(key)
                self._kb.release(key)
            except Exception:
                log.exception("Keyboard repeat failed for %s", key)
                return
            if i < repeats - 1:
                await asyncio.sleep(delay_s)

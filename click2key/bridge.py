"""Glue: pull ButtonEvents from ClickV2.events, dedupe, send keystrokes.

Both Click V2 pucks broadcast each other's button presses, so when both
are BLE-connected each press surfaces twice (once per peripheral). The
EventDeduper collapses near-simultaneous duplicates so only one
keystroke fires per physical button press.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .click_v2 import ButtonEvent, ClickV2
from .keyboard_out import KeyboardOutput

log = logging.getLogger(__name__)

UiSink = Callable[[ButtonEvent], None]


class EventDeduper:
    """Drop a (bit, is_down) event if the same one was just seen.

    Window is generous enough to cover dual-puck cross-talk (sub-ms apart
    in practice) but well below any plausible human tap rate.
    """

    def __init__(self, window_seconds: float = 0.075) -> None:
        self._window = window_seconds
        self._last_seen: dict[tuple[int, bool], float] = {}

    def is_duplicate(self, event: ButtonEvent) -> bool:
        key = (event.bit, event.is_down)
        now = time.monotonic()
        last = self._last_seen.get(key)
        self._last_seen[key] = now
        return last is not None and (now - last) < self._window


class Bridge:
    """Shared state for per-puck bridge tasks: keyboard output, dedup, UI fanout."""

    def __init__(
        self,
        keyboard: KeyboardOutput,
        ui_sink: UiSink | None = None,
    ) -> None:
        self.keyboard = keyboard
        self.ui_sink = ui_sink
        self.deduper = EventDeduper()


async def run_bridge(click: ClickV2, bridge: Bridge) -> None:
    while True:
        event: ButtonEvent = await click.events.get()
        if bridge.deduper.is_duplicate(event):
            continue
        if bridge.ui_sink is not None:
            try:
                bridge.ui_sink(event)
            except Exception:
                log.exception("ui_sink failed")
        await bridge.keyboard.send(event)

"""Glue: pull ButtonEvents from ClickV2.events, dedupe, dispatch.

Both Click V2 pucks broadcast each other's button presses, so when both
are BLE-connected each press surfaces twice (once per peripheral). The
EventDeduper collapses near-simultaneous duplicates so the output only
sees one event per physical button press.

Outputs are runtime-selectable: "link" sends JSON over the MyWhoosh Link
TCP socket; "keyboard" synthesizes keystrokes for whatever app has focus.
"""

from __future__ import annotations

import enum
import logging
import time
from collections.abc import Callable

from .click_v2 import Button, ButtonEvent, ClickV2
from .keyboard_out import KeyboardOutput
from .whoosh_link import WhooshLinkServer

log = logging.getLogger(__name__)

UiSink = Callable[[ButtonEvent], None]


class OutputMode(enum.Enum):
    LINK = "link"
    KEYBOARD = "keyboard"


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
    """Shared mutable state for all per-puck bridge tasks.

    Holds the current output mode and dedup state. Per-puck `run_bridge`
    coroutines hold a reference and dispatch based on `mode` at event
    time, so flipping the radio takes effect immediately.
    """

    def __init__(
        self,
        link: WhooshLinkServer,
        keyboard: KeyboardOutput,
        ui_sink: UiSink | None = None,
    ) -> None:
        self.link = link
        self.keyboard = keyboard
        self.ui_sink = ui_sink
        self.deduper = EventDeduper()
        self.mode: OutputMode = OutputMode.LINK


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
        await _dispatch(bridge, event)


async def _dispatch(bridge: Bridge, event: ButtonEvent) -> None:
    if bridge.mode is OutputMode.KEYBOARD:
        bridge.keyboard.send(event)
        return
    # Link mode — only fire shift commands, on press.
    if not event.is_down:
        return
    if event.button is Button.SHIFT_UP:
        await bridge.link.shift_up()
    elif event.button is Button.SHIFT_DOWN:
        await bridge.link.shift_down()

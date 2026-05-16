"""Glue: pull ButtonEvents from ClickV2.events and call WhooshLinkServer.

Also fans out every event to an optional UI callback so the app can show
per-puck status and last-button-pressed labels.

Both Click V2 pucks broadcast each other's button presses, so when both
are BLE-connected each press surfaces twice (once per peripheral). The
EventDeduper collapses near-simultaneous duplicates so MyWhoosh only
sees one shift per button press.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from .click_v2 import Button, ButtonEvent, ClickV2
from .whoosh_link import WhooshLinkServer

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


async def run_bridge(
    click: ClickV2,
    link: WhooshLinkServer,
    ui_sink: UiSink | None = None,
    deduper: EventDeduper | None = None,
) -> None:
    while True:
        event: ButtonEvent = await click.events.get()
        if deduper is not None and deduper.is_duplicate(event):
            continue
        if ui_sink is not None:
            try:
                ui_sink(event)
            except Exception:
                log.exception("ui_sink failed")
        if not event.is_down:
            continue
        if event.button is Button.SHIFT_UP:
            await link.shift_up()
        elif event.button is Button.SHIFT_DOWN:
            await link.shift_down()

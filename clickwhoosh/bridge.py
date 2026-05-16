"""Glue: pull ButtonEvents from ClickV2.events and call WhooshLinkServer.

Also fans out every event to an optional UI callback so the app can show
per-puck status and last-button-pressed labels.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from .click_v2 import Button, ButtonEvent, ClickV2
from .whoosh_link import WhooshLinkServer

log = logging.getLogger(__name__)

UiSink = Callable[[ButtonEvent], None]


async def run_bridge(
    click: ClickV2, link: WhooshLinkServer, ui_sink: UiSink | None = None
) -> None:
    while True:
        event: ButtonEvent = await click.events.get()
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

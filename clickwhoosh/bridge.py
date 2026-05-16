"""Glue: pull ButtonEvents from ClickV2.events and call WhooshLinkServer."""

from __future__ import annotations

import asyncio
import logging

from .click_v2 import Button, ButtonEvent, ClickV2
from .whoosh_link import WhooshLinkServer

log = logging.getLogger(__name__)


async def run_bridge(click: ClickV2, link: WhooshLinkServer) -> None:
    while True:
        event: ButtonEvent = await click.events.get()
        if not event.is_down:
            continue
        if event.button is Button.SHIFT_UP:
            await link.shift_up()
        elif event.button is Button.SHIFT_DOWN:
            await link.shift_down()
        else:
            log.debug("Ignoring button %s", event.button)

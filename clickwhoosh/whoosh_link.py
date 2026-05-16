"""MyWhoosh "Link" TCP server.

MyWhoosh connects to this server as a client (port 21587) and reads
newline-delimited JSON control messages. We only have to *send*.

Protocol shape (from inspection of MyWhoosh Link traffic):

    {"MessageType":"Controls","InGameControls":{"GearShifting":"1"}}
    {"MessageType":"Controls","InGameControls":{"GearShifting":"-1"}}
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

LINK_PORT = 21587

log = logging.getLogger(__name__)

OnConnectionChange = Callable[[bool], None]


class WhooshLinkServer:
    def __init__(self, on_connection_change: OnConnectionChange | None = None) -> None:
        self._server: asyncio.base_events.Server | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._on_connection_change = on_connection_change

    @property
    def is_client_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def start(self, host: str = "127.0.0.1", port: int = LINK_PORT) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle_client, host, port)
        log.info("MyWhoosh Link server listening on %s:%d", host, port)

    async def stop(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._notify(False)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        log.info("MyWhoosh connected from %s", peer)

        if self._writer is not None and not self._writer.is_closing():
            log.warning("Replacing previous client")
            self._writer.close()

        self._writer = writer
        self._notify(True)

        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                # MyWhoosh occasionally sends keep-alive frames; log at debug.
                log.debug("RX from MyWhoosh: %r", data)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            log.info("MyWhoosh disconnected (%s)", peer)
            if self._writer is writer:
                self._writer = None
                self._notify(False)
            writer.close()

    def _notify(self, connected: bool) -> None:
        if self._on_connection_change is not None:
            try:
                self._on_connection_change(connected)
            except Exception:
                log.exception("on_connection_change callback failed")

    async def _send(self, payload: dict) -> bool:
        if self._writer is None or self._writer.is_closing():
            log.debug("Drop message, no client: %s", payload)
            return False
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        self._writer.write(line.encode("utf-8"))
        try:
            await self._writer.drain()
            return True
        except ConnectionError:
            return False

    async def shift_up(self) -> bool:
        return await self._send(
            {"MessageType": "Controls", "InGameControls": {"GearShifting": "1"}}
        )

    async def shift_down(self) -> bool:
        return await self._send(
            {"MessageType": "Controls", "InGameControls": {"GearShifting": "-1"}}
        )

import asyncio
import json

import pytest

from clickwhoosh.whoosh_link import LINK_PORT, WhooshLinkServer


@pytest.mark.asyncio
async def test_shift_up_and_down_round_trip():
    # Use an ephemeral port to avoid clashing with a real MyWhoosh.
    port = LINK_PORT + 1000
    events: list[bool] = []
    server = WhooshLinkServer(on_connection_change=events.append)
    await server.start(port=port)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.sleep(0.05)
        assert server.is_client_connected
        assert events[-1] is True

        assert await server.shift_up() is True
        assert await server.shift_down() is True

        line1 = await reader.readline()
        line2 = await reader.readline()
        assert json.loads(line1) == {
            "MessageType": "Controls",
            "InGameControls": {"GearShifting": "1"},
        }
        assert json.loads(line2) == {
            "MessageType": "Controls",
            "InGameControls": {"GearShifting": "-1"},
        }

        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)
        assert not server.is_client_connected
    finally:
        await server.stop()

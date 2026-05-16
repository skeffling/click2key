"""Zwift Click V2 BLE driver.

Click V2 uses the Zwift Ride/Play protocol family: a "RideOn" handshake
followed by an ECDH key exchange and AES-encrypted protobuf messages.

What's done here:
    - BLE scan + connect via bleak
    - Service/characteristic discovery
    - Stub `_handshake()` and `_decode_button_event()` that need to be ported
      from https://github.com/ajchellew/zwiftplay (Kotlin + .NET)
    - Public surface: `events: asyncio.Queue[ButtonEvent]` so the rest of the
      app doesn't care how decoding works.

Do this on the Windows machine (with the Click V2 present) — bleak on
macOS can't talk to Zwift devices reliably.
"""

from __future__ import annotations

import asyncio
import enum
import logging
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

log = logging.getLogger(__name__)

# Zwift Play/Ride/Click-V2 share these service + characteristic UUIDs.
ZWIFT_CUSTOM_SERVICE_UUID = "0000fc82-0000-1000-8000-00805f9b34fb"
ZWIFT_ASYNC_CHAR_UUID = "00000002-19ca-4651-86e5-fa29dcdd09d1"
ZWIFT_SYNC_TX_CHAR_UUID = "00000003-19ca-4651-86e5-fa29dcdd09d1"
ZWIFT_SYNC_RX_CHAR_UUID = "00000004-19ca-4651-86e5-fa29dcdd09d1"

# Bytes the device sends/expects during the unencrypted "hello" phase.
RIDE_ON = bytes([0x52, 0x69, 0x64, 0x65, 0x4F, 0x6E])  # b"RideOn"


class Button(enum.Enum):
    SHIFT_UP = "shift_up"
    SHIFT_DOWN = "shift_down"
    # V2 also has nav D-pad + A/B/Y/Z; add as needed.


@dataclass(frozen=True)
class ButtonEvent:
    button: Button
    is_down: bool


class ClickV2:
    def __init__(self) -> None:
        self.events: asyncio.Queue[ButtonEvent] = asyncio.Queue()
        self._client: BleakClient | None = None
        self._device: BLEDevice | None = None
        # Session state populated by the handshake (ECDH keys, AES context, counters).
        # Fill these in when porting from zwiftplay.
        self._session: dict = {}

    @staticmethod
    async def scan(timeout: float = 8.0) -> list[BLEDevice]:
        """Return BLE devices that advertise the Zwift custom service."""
        devices = await BleakScanner.discover(
            timeout=timeout, service_uuids=[ZWIFT_CUSTOM_SERVICE_UUID]
        )
        # Some adapters don't filter by service UUID in the scan call; filter again.
        return [d for d in devices if d.name and "click" in d.name.lower()]

    async def connect(self, device: BLEDevice) -> None:
        log.info("Connecting to %s (%s)", device.name, device.address)
        self._device = device
        self._client = BleakClient(device)
        await self._client.connect()
        await self._dump_gatt()

        # Subscribe BEFORE writing — otherwise we miss the handshake reply.
        await self._client.start_notify(ZWIFT_ASYNC_CHAR_UUID, self._on_async_notify)
        await self._client.start_notify(ZWIFT_SYNC_RX_CHAR_UUID, self._on_sync_notify)
        log.info("Subscribed to async + sync RX")

        await self._handshake()
        log.info("Click V2 ready (greeting sent, awaiting reply)")

    async def _dump_gatt(self) -> None:
        assert self._client is not None
        log.info("Enumerating GATT services on %s", self._device.address if self._device else "?")
        for service in self._client.services:
            log.info("Service %s", service.uuid)
            for char in service.characteristics:
                props = ",".join(char.properties)
                log.info("  Char %s  [%s]", char.uuid, props)

    async def disconnect(self) -> None:
        if self._client is not None and self._client.is_connected:
            await self._client.disconnect()
        self._client = None

    # ------------------------------------------------------------------
    # Handshake — TODO: port from ajchellew/zwiftplay
    # ------------------------------------------------------------------

    async def _handshake(self) -> None:
        """Perform the RideOn + ECDH handshake.

        Sketch (verify against zwiftplay source):
          1. Generate an ephemeral P-256 keypair.
          2. Write `RideOn || 0x01 || 0x03 || <our_pub_key_64B>` to SYNC_TX.
          3. Read device's response on SYNC_RX: `RideOn || 0x01 || 0x02 || <peer_pub_key_64B>`.
          4. Derive shared secret via ECDH; HKDF/sha256 → AES key + IV + counter seed.
          5. From here, all messages on ASYNC are AES-GCM-encrypted protobuf.

        For now we just send the RideOn greeting and log replies so we can
        confirm the BLE link is alive before tackling crypto.
        """
        assert self._client is not None
        # SYNC_TX is write-without-response on Click V2 — confirmed by GATT dump.
        # Greeting is "RideOn" + two-byte type code. zwiftplay docs show the
        # full handshake also appends a 64-byte ECDH public key; we send the
        # short greeting first to see what the device replies with.
        greeting = RIDE_ON + bytes([0x01, 0x03])
        log.warning("Handshake is a stub — sending greeting only (no pubkey yet)")
        log.info("TX → SYNC_TX (%d bytes): %s", len(greeting), greeting.hex())
        await self._client.write_gatt_char(
            ZWIFT_SYNC_TX_CHAR_UUID, greeting, response=False
        )
        # TODO post-handshake:
        #   - send [0xFF, 0x04, 0x00] to ASYNC (BikeControl does this for V2;
        #     likely the "stay awake past 60s" enable or a keepalive seed).
        #   - start a 30s background task that re-sends a keepalive so the
        #     Click does not stop emitting button events when its LED dims.
        # Note: users report that a single ride with the official Zwift app
        # also resolves the 60s notification-stop problem permanently. If
        # the keepalive turns out to be unnecessary post-Zwift-pairing,
        # we can leave the code in as belt-and-braces.

    # ------------------------------------------------------------------
    # Notification handlers
    # ------------------------------------------------------------------

    def _on_async_notify(self, _char, data: bytearray) -> None:
        # After the handshake completes, button events arrive here as
        # AES-encrypted protobuf. For now, log raw bytes so we can capture
        # them for protocol work.
        log.info("ASYNC RX (%d bytes): %s", len(data), bytes(data).hex())
        event = self._decode_button_event(bytes(data))
        if event is not None:
            self.events.put_nowait(event)

    def _on_sync_notify(self, _char, data: bytearray) -> None:
        log.info("SYNC RX (%d bytes): %s", len(data), bytes(data).hex())
        # Handshake response handling goes here.

    def _decode_button_event(self, payload: bytes) -> ButtonEvent | None:
        """Decode a decrypted protobuf button-state message.

        Stub. Once the handshake is done and `payload` is plaintext, parse
        the protobuf and emit shift_up / shift_down events. Until then,
        returns None.
        """
        return None

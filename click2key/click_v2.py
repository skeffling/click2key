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
import os
import time
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from .zap_crypto import (
    MAC_LENGTH,
    PUBKEY_RAW_LENGTH,
    REQUEST_START,
    RESPONSE_START,
    RIDE_ON as ZAP_RIDE_ON,
    ZapCipher,
    ZapKeyExchange,
)

log = logging.getLogger(__name__)

# Zwift Play/Ride/Click-V2 share these service + characteristic UUIDs.
ZWIFT_CUSTOM_SERVICE_UUID = "0000fc82-0000-1000-8000-00805f9b34fb"
ZWIFT_ASYNC_CHAR_UUID = "00000002-19ca-4651-86e5-fa29dcdd09d1"
ZWIFT_SYNC_TX_CHAR_UUID = "00000003-19ca-4651-86e5-fa29dcdd09d1"
ZWIFT_SYNC_RX_CHAR_UUID = "00000004-19ca-4651-86e5-fa29dcdd09d1"
# Standard Battery Service characteristic — readable, harmless to poll.
BATTERY_LEVEL_CHAR_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

# Re-export so callers don't have to know about zap_crypto. Same value;
# the wire format hasn't changed.
RIDE_ON = ZAP_RIDE_ON


def _read_varint(buf: bytes, start: int) -> tuple[int, int]:
    """Read a protobuf varint from `buf` at `start`. Returns (value, next_index)."""
    result = 0
    shift = 0
    i = start
    while i < len(buf):
        b = buf[i]
        result |= (b & 0x7F) << shift
        i += 1
        if not (b & 0x80):
            return result, i
        shift += 7
    raise ValueError("Truncated varint")


class Puck(enum.Enum):
    LEFT = "left"     # + and the 4 arrow buttons
    RIGHT = "right"   # − and the 4 arrow buttons
    UNKNOWN = "unknown"


class Button(enum.Enum):
    SHIFT_UP = "shift_up"      # + on left puck
    SHIFT_DOWN = "shift_down"  # − on right puck
    # Left puck colored corner buttons (Y top, A right, B bottom, Z left).
    A = "A"  # green, right of diamond
    B = "B"  # magenta, bottom of diamond
    Y = "Y"  # blue, top of diamond
    Z = "Z"  # orange, left of diamond
    # Right puck plain arrow icons.
    NAV_UP = "nav_up"
    NAV_DOWN = "nav_down"
    NAV_LEFT = "nav_left"
    NAV_RIGHT = "nav_right"


# Display label for each button. Identified empirically; press order on the
# left puck was +, ↑, →, ↓, ← and yielded bits 4, 5, 6, 7, 12 respectively.
# Right puck bits will be filled in once that puck is awake and we run the
# same mapping pass.
BUTTON_LABELS: dict[int, tuple[Puck, Button]] = {
    12: (Puck.LEFT, Button.SHIFT_UP),
    4:  (Puck.LEFT, Button.A),
    5:  (Puck.LEFT, Button.B),
    6:  (Puck.LEFT, Button.Y),
    7:  (Puck.LEFT, Button.Z),
    8:  (Puck.RIGHT, Button.SHIFT_DOWN),
    3:  (Puck.RIGHT, Button.NAV_UP),
    1:  (Puck.RIGHT, Button.NAV_DOWN),
    2:  (Puck.RIGHT, Button.NAV_LEFT),
    0:  (Puck.RIGHT, Button.NAV_RIGHT),
}


@dataclass(frozen=True)
class ButtonEvent:
    bit: int
    puck: Puck
    button: Button | None  # None when the bit is not yet mapped
    is_down: bool

    @property
    def label(self) -> str:
        if self.button is not None:
            return self.button.value
        return f"bit{self.bit}"


KEEPALIVE_INTERVAL_SECONDS = 30


class ClickV2:
    def __init__(self) -> None:
        self.events: asyncio.Queue[ButtonEvent] = asyncio.Queue()
        self._client: BleakClient | None = None
        self._device: BLEDevice | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._connected_at: float | None = None
        self._last_bitmap: int | None = None
        self._last_battery: int | None = None
        self._last_event_at: float | None = None
        # Set once we see a button event whose bit maps to a known puck.
        self.puck_identity: "Puck | None" = None
        # CLICK2KEY_PLAINTEXT=1 forces the old unencrypted handshake (sends a
        # short "RideOn" greeting, parses plaintext bitmap frames). Default
        # is the real ECDH handshake; the plaintext path is a temporary
        # escape hatch while Phase A captures V2 button payloads.
        self._encrypted: bool = os.environ.get("CLICK2KEY_PLAINTEXT", "0") != "1"
        self._key_exchange: ZapKeyExchange | None = None
        self._cipher: ZapCipher | None = None
        self._handshake_complete: bool = False

    @property
    def battery_percent(self) -> int | None:
        return self._last_battery

    # ~Time after connect with no button events that we call a puck "silent"
    # (the V2's well-known 60-second sleep). BikeControl uses 60s for the same
    # check; we go slightly higher so a slow first-press doesn't trip it.
    SILENT_THRESHOLD_SECONDS = 70

    @property
    def is_silent(self) -> bool:
        """True if the puck has gone quiet past the post-connect grace period.

        Catches both "never sent anything" and "sent only during the first
        ~minute then stopped" — the second is the classic V2 60s-sleep
        signature, where the puck releases the wake-press during connect
        then stops responding.
        """
        if self._connected_at is None:
            return False
        elapsed = time.monotonic() - self._connected_at
        if elapsed <= self.SILENT_THRESHOLD_SECONDS:
            return False
        if self._last_event_at is None:
            return True
        return (self._last_event_at - self._connected_at) <= self.SILENT_THRESHOLD_SECONDS

    @staticmethod
    async def scan(timeout: float = 8.0) -> list[BLEDevice]:
        """Return BLE devices that look like a Zwift Click V2.

        We deliberately don't pass `service_uuids=` to BleakScanner: on the
        Windows/WinRT backend that becomes a kernel-side advertisement filter,
        and Zwift pucks often only put their service UUID in the scan-response
        (not the primary advertisement), so the filtered scan returns nothing.
        Scan everything and match by name or advertised service UUID instead.
        """
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
        matches: list[BLEDevice] = []
        for device, adv in discovered.values():
            name = (device.name or adv.local_name or "").lower()
            uuids = {u.lower() for u in (adv.service_uuids or [])}
            if "click" in name or ZWIFT_CUSTOM_SERVICE_UUID.lower() in uuids:
                matches.append(device)
        return matches

    async def connect(self, device: BLEDevice) -> None:
        log.info("Connecting to %s (%s)", device.name, device.address)
        self._device = device
        self._client = BleakClient(device)
        await self._client.connect()
        self._connected_at = time.monotonic()
        await self._dump_gatt()

        # Subscribe BEFORE writing — otherwise we miss the handshake reply.
        await self._client.start_notify(ZWIFT_ASYNC_CHAR_UUID, self._on_async_notify)
        await self._client.start_notify(ZWIFT_SYNC_RX_CHAR_UUID, self._on_sync_notify)
        log.info("Subscribed to async + sync RX")

        await self._handshake()
        # Prime the battery reading now so the UI doesn't have to wait up to
        # 30s (or for an unsolicited notify) before showing a percentage.
        await self._read_battery_once()
        # Read-only keepalive: poll battery level every 30s. No GATT writes,
        # which previously silenced or hung the puck. The read value is also
        # used to populate battery_percent for pucks that don't push notifies.
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        log.info("Click V2 ready (greeting sent, read-only keepalive)")

    async def _read_battery_once(self) -> None:
        assert self._client is not None
        try:
            data = await self._client.read_gatt_char(BATTERY_LEVEL_CHAR_UUID)
        except Exception:
            log.exception("Battery read failed")
            return
        if data:
            pct = data[0]
            if pct != self._last_battery:
                log.info("Battery (read): %d%%", pct)
            self._last_battery = pct

    async def _keepalive_loop(self) -> None:
        assert self._client is not None
        try:
            while self._client.is_connected:
                await asyncio.sleep(KEEPALIVE_INTERVAL_SECONDS)
                try:
                    await self._read_battery_once()
                except Exception:
                    log.exception("Keepalive read failed; stopping loop")
                    return
        except asyncio.CancelledError:
            pass

    async def _dump_gatt(self) -> None:
        assert self._client is not None
        log.info("Enumerating GATT services on %s", self._device.address if self._device else "?")
        for service in self._client.services:
            log.info("Service %s", service.uuid)
            for char in service.characteristics:
                props = ",".join(char.properties)
                log.info("  Char %s  [%s]", char.uuid, props)

    async def disconnect(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        if self._client is not None and self._client.is_connected:
            await self._client.disconnect()
        self._client = None

    # ------------------------------------------------------------------
    # Handshake (ECDH P-256 + HKDF-SHA256 + AES-CCM, per ajchellew/zwiftplay)
    # ------------------------------------------------------------------

    async def _handshake(self) -> None:
        assert self._client is not None
        if self._encrypted:
            self._key_exchange = ZapKeyExchange()
            greeting = (
                RIDE_ON + REQUEST_START + self._key_exchange.local_pub_bytes()
            )
            log.info("Handshake TX (encrypted, %d bytes)", len(greeting))
        else:
            # Legacy plaintext bypass — short "RideOn" greeting puts the puck
            # into the unencrypted compatibility mode that emits 0x23 0x08
            # bitmap frames. Kept behind CLICK2KEY_PLAINTEXT=1 as a safety
            # hatch while we validate the encrypted path on real hardware.
            greeting = RIDE_ON + bytes([0x01, 0x03])
            log.warning("Handshake TX (plaintext bypass mode)")
        await self._client.write_gatt_char(
            ZWIFT_SYNC_TX_CHAR_UUID, greeting, response=False
        )

    # ------------------------------------------------------------------
    # Notification handlers
    # ------------------------------------------------------------------

    def _on_async_notify(self, _char, data: bytearray) -> None:
        payload = bytes(data)
        # Encrypted frame: counter(4) || ciphertext(N) || tag(4).
        if self._handshake_complete and self._cipher is not None \
                and len(payload) > 4 + MAC_LENGTH:
            try:
                plaintext = self._cipher.decrypt(payload[:4], payload[4:])
            except Exception as ex:
                log.warning("Decrypt failed (%s): %s", ex, payload.hex())
                return
            self._handle_encrypted_message(plaintext)
            return
        # Plaintext bypass mode — Click V2 sends: 0x23 0x08 <varint bitmap>.
        # 0x23 = message type (button-state poll). 0x08 = protobuf field 1.
        # Bitmap is active-low: bit cleared == that button is currently pressed.
        if len(payload) >= 3 and payload[0] == 0x23 and payload[1] == 0x08:
            bitmap, _ = _read_varint(payload, 2)
            self._handle_bitmap(bitmap)
            return
        # Plaintext battery: 0x19 0x10 <varint percent>. Pucks send this ~5s.
        if len(payload) >= 3 and payload[0] == 0x19 and payload[1] == 0x10:
            pct, _ = _read_varint(payload, 2)
            if pct != self._last_battery:
                log.info("Battery: %d%%", pct)
                self._last_battery = pct
            return
        log.debug("ASYNC RX (%d bytes, unhandled): %s", len(payload), payload.hex())

    # Type codes inside decrypted frames; see ajchellew/zwiftplay ZapConstants.
    _TYPE_CONTROLLER = 7
    _TYPE_EMPTY = 21
    _TYPE_BATTERY = 25

    def _handle_encrypted_message(self, plaintext: bytes) -> None:
        if not plaintext:
            return
        msg_type = plaintext[0]
        body = plaintext[1:]
        if msg_type == self._TYPE_EMPTY:
            return  # periodic keepalive
        if msg_type == self._TYPE_BATTERY:
            # protobuf: field 1 varint = level. Body starts with 0x08 tag.
            if len(body) >= 2 and body[0] == 0x08:
                pct, _ = _read_varint(body, 1)
                if pct != self._last_battery:
                    log.info("Battery (encrypted): %d%%", pct)
                    self._last_battery = pct
            return
        if msg_type == self._TYPE_CONTROLLER:
            # Phase A: log only so we can reverse-engineer the V2 button
            # protobuf layout. Phase B will decode and emit ButtonEvents.
            self._last_event_at = time.monotonic()
            log.info("Controller notification (encrypted): %s", body.hex())
            return
        log.info("Unknown encrypted type=%d body=%s", msg_type, body.hex())

    def _handle_bitmap(self, bitmap: int) -> None:
        last = self._last_bitmap
        self._last_bitmap = bitmap
        if last is None or last == bitmap:
            return
        # Used by is_silent: any bit transition counts as "puck is alive."
        self._last_event_at = time.monotonic()
        # Active-low: a 1→0 transition means "pressed"; 0→1 means "released".
        # Iterate only the bits that actually changed.
        changed = last ^ bitmap
        while changed:
            bit = (changed & -changed).bit_length() - 1
            changed &= changed - 1
            now_pressed = not (bitmap & (1 << bit))
            log.info(
                "Button bit %d %s   (bitmap 0x%X → 0x%X)",
                bit,
                "PRESSED" if now_pressed else "released",
                last,
                bitmap,
            )
            self.events.put_nowait(self._bit_to_event(bit, now_pressed))

    def _bit_to_event(self, bit: int, is_down: bool) -> ButtonEvent:
        info = BUTTON_LABELS.get(bit)
        if info is None:
            return ButtonEvent(bit=bit, puck=Puck.UNKNOWN, button=None, is_down=is_down)
        puck, button = info
        if self.puck_identity is None:
            self.puck_identity = puck
        return ButtonEvent(bit=bit, puck=puck, button=button, is_down=is_down)

    def _on_sync_notify(self, _char, data: bytearray) -> None:
        payload = bytes(data)
        log.debug("SYNC RX (%d bytes): %s", len(payload), payload.hex())
        # Encrypted handshake reply: RIDE_ON || RESPONSE_START || device_pub(64).
        prefix = RIDE_ON + RESPONSE_START
        if (
            self._encrypted
            and self._key_exchange is not None
            and not self._handshake_complete
            and payload.startswith(prefix)
            and len(payload) == len(prefix) + PUBKEY_RAW_LENGTH
        ):
            device_pub = payload[len(prefix):]
            try:
                aes_key, iv_prefix = self._key_exchange.derive(device_pub)
                self._cipher = ZapCipher(aes_key, iv_prefix)
            except Exception:
                log.exception("Failed to derive session keys; staying plaintext")
                return
            self._handshake_complete = True
            log.info("Handshake complete: encrypted session established")


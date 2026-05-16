# Whoosh Clicker

A lean bridge from a **Zwift Click V2** controller to **MyWhoosh**, using MyWhoosh's local "Link" TCP protocol. Runs on macOS and Windows.

(Python package is still `clickwhoosh`.)

Not affiliated with or derived from BikeControl. Built from scratch using public protocol documentation.

## Status

- [x] MyWhoosh Link TCP server (port 21587, newline-delimited JSON)
- [ ] Zwift Click V2 BLE driver — handshake porting in progress (see `clickwhoosh/click_v2.py`)
- [x] CustomTkinter UI shell
- [ ] PyInstaller `.exe` packaging

## Run (dev)

```
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e .
clickwhoosh
```

## How it works

1. `clickwhoosh` opens a TCP listener on `127.0.0.1:21587`.
2. MyWhoosh (running on the same machine) connects to it as if it were the official "MyWhoosh Link" companion app.
3. `clickwhoosh` connects to the Click V2 over BLE, decodes button presses, and sends JSON gear-change messages to MyWhoosh.

Message shapes (see `clickwhoosh/whoosh_link.py`):

```json
{"MessageType":"Controls","InGameControls":{"GearShifting":"1"}}   // shift up
{"MessageType":"Controls","InGameControls":{"GearShifting":"-1"}}  // shift down
```

## Click V2 "60-second sleep" notes

Out of the box, a Click V2 may stop sending button events to non-Zwift apps
about 60 seconds after connecting (when the solid blue LED turns off). The
BLE connection itself stays up — only notifications stop.

**One-time fix (recommended):** open the free Zwift app once with the Click
paired, ride 30 seconds, exit. Users report this permanently unlocks the
"stay awake" mode across all third-party apps. See
https://github.com/OpenBikeControl/bikecontrol/issues/68 for the thread.

**In-code mitigation:** after the handshake we plan to send BikeControl's
post-handshake byte sequence (`0xFF 0x04 0x00`) and a 30s keepalive ping —
see TODO in `clickwhoosh/click_v2.py`.

## Click V2 protocol references

Click V2 uses the Zwift Ride/Play encrypted protocol family. Useful references:

- https://github.com/ajchellew/zwiftplay — Kotlin/.NET implementation of the handshake
- https://www.makinolo.com/blog/2024/01/02/reverse-engineering-zwift-play-controllers/ — encryption write-up

# Whoosh Clicker

A lean bridge from a **Zwift Click V2** controller to **MyWhoosh**, with two
output modes:

- **Keyboard** — synthesizes keystrokes for whatever app has focus (default
  mapping matches MyWhoosh's published shortcuts; remappable).
- **Link** — opens a local TCP server (`127.0.0.1:21587`) speaking MyWhoosh's
  Link protocol. **Not yet working** end-to-end — MyWhoosh's discovery
  mechanism needs more investigation; Keyboard mode is the reliable path
  today.

Runs on macOS and Windows. (Python package is `clickwhoosh`; the user-facing
name is "Whoosh Clicker".)

Not affiliated with or derived from BikeControl. Built from scratch using
public protocol documentation and ground-truth BLE sniffing.

## Status

- [x] Two-puck BLE driver (plaintext bitmap protocol — no ECDH needed)
- [x] Cross-puck event dedup
- [x] Keyboard output mode + configurable per-button mappings
- [x] CustomTkinter UI with collapsible debug pane
- [x] macOS `.app` bundle build
- [x] Windows `.exe` bundle build
- [ ] MyWhoosh Link mode (TCP server runs; MyWhoosh-side discovery TBD)

## Run from source (dev)

```bash
# macOS
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/clickwhoosh

# Windows
py -m venv .venv
.venv\Scripts\activate
pip install -e .
clickwhoosh
```

## Build a distributable

PyInstaller produces a standalone `.app` (macOS) or `.exe` folder (Windows)
that doesn't need a Python install on the target machine. The
`clickwhoosh.spec` file works on both platforms.

### macOS

```bash
./build_macos.sh
# → dist/Whoosh Clicker.app

# Install + clear quarantine + launch
mv "dist/Whoosh Clicker.app" /Applications/
xattr -dr com.apple.quarantine "/Applications/Whoosh Clicker.app"
open "/Applications/Whoosh Clicker.app"
```

On first launch macOS will prompt for **Bluetooth** access (accept). For
Keyboard mode you also need to grant **Accessibility** in
*System Settings → Privacy & Security → Accessibility* — add
`/Applications/Whoosh Clicker.app` to the list. The bundle ID is stable
across rebuilds, so the grant persists.

### Windows

One-time setup (install Python 3.12+ from python.org, then from the repo root):

```cmd
py -m venv .venv
```

Build:

```cmd
build_windows.bat
```

Output: `dist\Whoosh Clicker\Whoosh Clicker.exe` plus an `_internal\` folder
of bundled libraries. **Distribute the entire `dist\Whoosh Clicker` folder**
(zip it for sharing) — the `.exe` won't run without `_internal\` next to it.

First launch: Windows Defender SmartScreen may say "Windows protected your
PC" since the exe isn't code-signed. Click **More info → Run anyway**.
You'll get a Bluetooth permission prompt the first time you scan; accept it.

## Using it

1. Wake both Click V2 pucks (long-press any button until the LED comes on).
2. Launch Whoosh Clicker. It auto-scans and connects to both pucks. Dots go
   grey → amber → green as each puck is discovered and identified by its
   first button press.
3. Pick the **Keyboard** output radio.
4. Focus MyWhoosh, ride.
5. + on the left puck = shift up (K). − on the right puck = shift down (I).
   Open *Configure keys…* in the debug pane to remap.

## Click V2 "60-second sleep"

A fresh Click V2 may stop sending button events to non-Zwift apps about 60s
after connecting (when the solid blue LED turns off). The BLE connection
itself stays up — only notifications stop.

**Fix (once, then forever):** pair the Click in the free Zwift app and ride
for ~30 seconds. After that, the puck stays awake for any app. See
<https://github.com/OpenBikeControl/bikecontrol/issues/68> for the thread.

## Protocol notes

- The Click V2 sends button state as `0x23 0x08 <protobuf-varint-bitmap>` on
  the async characteristic of service `0000fc82-…`. Bitmap is active-low.
- Both pucks broadcast each other's button events; `EventDeduper` collapses
  the duplicates inside a 75ms window.
- Left puck bits: `+`=12, `A`=4, `B`=5, `Y`=6, `Z`=7
- Right puck bits: `−`=8, `↑`=3, `↓`=1, `←`=2, `→`=0

MyWhoosh Link protocol (when/if we get discovery working):

```json
{"MessageType":"Controls","InGameControls":{"GearShifting":"1"}}   // shift up
{"MessageType":"Controls","InGameControls":{"GearShifting":"-1"}}  // shift down
```

## References

- <https://github.com/ajchellew/zwiftplay> — Kotlin/.NET reference for the
  encrypted Zwift Play/Ride/Click-V2 family (turns out V2 also speaks the
  plaintext bitmap protocol if you don't send the encrypted handshake).
- <https://www.makinolo.com/blog/2024/01/02/reverse-engineering-zwift-play-controllers/>
- <https://github.com/OpenBikeControl/bikecontrol> — the inspiration. Their
  paywall is server-side via Supabase; this project does not try to bypass
  anything they ship.

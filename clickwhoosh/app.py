"""CustomTkinter UI shell.

Three columns of state: MyWhoosh Link, Click V2, and a scrolling log.
The asyncio event loop runs in a background thread; UI callbacks marshal
work onto it via asyncio.run_coroutine_threadsafe.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import threading
from collections.abc import Coroutine
from typing import Any

import customtkinter as ctk

from .bridge import Bridge, OutputMode, run_bridge
from .click_v2 import Button, ButtonEvent, ClickV2, Puck
from .keyboard_out import KeyboardOutput
from .keymap_dialog import KeymapDialog
from .whoosh_link import LINK_PORT, WhooshLinkServer

DOT_OFF = "#888888"
DOT_PENDING = "#d8a200"  # connected but puck identity unknown until first press
DOT_ON = "#33aa55"


def _accessibility_trusted() -> bool | None:
    """Returns True/False if running on macOS and we can query the API.

    None if not on macOS or the API isn't reachable. Calls Apple's
    AXIsProcessTrusted() so this reflects the real, in-effect state of
    Accessibility permission for this exact process — independent of what
    appears in System Settings.
    """
    if sys.platform != "darwin":
        return None
    try:
        import ctypes
        import ctypes.util
        path = ctypes.util.find_library("ApplicationServices")
        if path is None:
            return None
        ax = ctypes.CDLL(path)
        ax.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(ax.AXIsProcessTrusted())
    except Exception:
        return None


def _accessibility_target() -> str:
    """Best macOS Accessibility target for the running process.

    - If we're inside a bundled .app (PyInstaller / py2app), return the .app
      path — this is what the user adds to System Settings, and the bundle ID
      makes the grant stick.
    - Otherwise (running under a venv / framework Python), return the
      framework's Python.app bundle.
    - Fallback: the resolved python binary.
    """
    import os
    real = os.path.realpath(sys.executable)

    # Bundled-app case: walk up until we find a dir ending in '.app'.
    head = real
    for _ in range(8):
        head = os.path.dirname(head)
        if head in ("", "/"):
            break
        if head.endswith(".app"):
            return head

    # Framework Python case.
    head = real
    for _ in range(8):
        head = os.path.dirname(head)
        if os.path.basename(head) == "Python.framework":
            versions_dir = os.path.join(head, "Versions")
            if os.path.isdir(versions_dir):
                for v in sorted(os.listdir(versions_dir), reverse=True):
                    candidate = os.path.join(versions_dir, v, "Resources", "Python.app")
                    if os.path.isdir(candidate):
                        return candidate
            break

    return real

# (display text, optional Button mapping) per glyph in the title.
_LEFT_LAYOUT: list[tuple[str, Button | None]] = [
    ("  ", None),
    ("+", Button.SHIFT_UP),
    (" ", None),
    ("A", Button.NAV_RIGHT),
    (" ", None),
    ("B", Button.NAV_DOWN),
    (" ", None),
    ("Y", Button.NAV_UP),
    (" ", None),
    ("Z", Button.NAV_LEFT),
]

_RIGHT_LAYOUT: list[tuple[str, Button | None]] = [
    ("  ", None),
    ("−", Button.SHIFT_DOWN),
    (" ", None),
    ("↑", Button.NAV_UP),
    (" ", None),
    ("↓", Button.NAV_DOWN),
    (" ", None),
    ("←", Button.NAV_LEFT),
    (" ", None),
    ("→", Button.NAV_RIGHT),
]

log = logging.getLogger(__name__)


class TkLogHandler(logging.Handler):
    def __init__(self, textbox: ctk.CTkTextbox) -> None:
        super().__init__()
        self._textbox = textbox

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record) + "\n"
        self._textbox.after(0, self._append, msg)

    def _append(self, msg: str) -> None:
        self._textbox.configure(state="normal")
        self._textbox.insert("end", msg)
        self._textbox.see("end")
        self._textbox.configure(state="disabled")


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Whoosh Clicker")
        self._collapsed_geometry = "640x320"
        self._expanded_geometry = "720x620"
        self.geometry(self._collapsed_geometry)

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._loop_thread.start()

        # One ClickV2 per BLE-connected puck. Keyed by device address.
        self._clicks: dict[str, ClickV2] = {}
        self._bridge_tasks: dict[str, asyncio.Task] = {}
        self._link = WhooshLinkServer(on_connection_change=self._on_link_state)
        self._bridge = Bridge(
            link=self._link,
            keyboard=KeyboardOutput(),
            ui_sink=self._on_button_event,
        )

        # Tri-state per puck: identified (green) > pending BLE (yellow) > none (grey).
        self._left_identified = False
        self._right_identified = False
        self._link_connected = False

        self._build_ui()
        self._submit(self._start_services())

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        header = ctk.CTkLabel(self, text="Whoosh Clicker", font=ctk.CTkFont(size=20, weight="bold"))
        header.pack(pady=(12, 4))
        self._subtitle = ctk.CTkLabel(self, text="")
        self._subtitle.pack()

        # Two columns of puck cards.
        pucks_row = ctk.CTkFrame(self)
        pucks_row.pack(fill="x", padx=12, pady=(8, 8))
        self._left_dot, self._left_glyphs, self._left_hint = self._build_puck_row(
            pucks_row, "Left puck", _LEFT_LAYOUT, column=0,
        )
        self._right_dot, self._right_glyphs, self._right_hint = self._build_puck_row(
            pucks_row, "Right puck", _RIGHT_LAYOUT, column=1,
        )
        pucks_row.grid_columnconfigure(0, weight=1)
        pucks_row.grid_columnconfigure(1, weight=1)

        # Status + hint area below the pucks.
        status_frame = ctk.CTkFrame(self, fg_color="transparent")
        status_frame.pack(fill="x", padx=12, pady=(0, 6))
        self._link_status = ctk.CTkLabel(
            status_frame, text="MyWhoosh: waiting…", anchor="w",
            font=ctk.CTkFont(weight="bold"),
        )
        self._link_status.pack(side="left", padx=8)
        self._scan_btn = ctk.CTkButton(
            status_frame, text="Scan + Connect", width=140,
            command=self._on_scan_click,
        )
        self._scan_btn.pack(side="right", padx=8)
        self._scan_spinner = ctk.CTkProgressBar(
            status_frame, mode="indeterminate", width=110,
        )

        mode_row = ctk.CTkFrame(self, fg_color="transparent")
        mode_row.pack(fill="x", padx=12, pady=(0, 2))
        ctk.CTkLabel(mode_row, text="Output:").pack(side="left", padx=(8, 8))
        self._mode_var = ctk.StringVar(value=OutputMode.LINK.value)
        ctk.CTkRadioButton(
            mode_row, text="Link (MyWhoosh TCP)",
            value=OutputMode.LINK.value, variable=self._mode_var,
            command=self._on_mode_change,
        ).pack(side="left", padx=4)
        ctk.CTkRadioButton(
            mode_row, text="Keyboard (focused window)",
            value=OutputMode.KEYBOARD.value, variable=self._mode_var,
            command=self._on_mode_change,
        ).pack(side="left", padx=4)

        self._hint_label = ctk.CTkLabel(
            self, text="", anchor="w", text_color="gray60",
        )
        self._hint_label.pack(fill="x", padx=20, pady=(0, 4))

        # Toggle row — the only thing visible from the debug pane when collapsed.
        toggle_row = ctk.CTkFrame(self, fg_color="transparent")
        toggle_row.pack(fill="x", padx=12, pady=(2, 8))
        self._debug_toggle = ctk.CTkButton(
            toggle_row, text="Show debug ▾", width=140, height=28,
            command=self._toggle_debug_pane,
        )
        self._debug_toggle.pack(side="right")

        # Debug pane — created but not packed; toggle pack/pack_forget below.
        self._debug_pane = ctk.CTkFrame(self)

        debug_buttons = ctk.CTkFrame(self._debug_pane, fg_color="transparent")
        debug_buttons.pack(fill="x", padx=8, pady=(8, 4))
        ctk.CTkLabel(debug_buttons, text="Send to MyWhoosh:").pack(side="left", padx=(4, 8))
        ctk.CTkButton(
            debug_buttons, text="Shift Down", width=110,
            command=lambda: self._submit(self._link.shift_down()),
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            debug_buttons, text="Shift Up", width=110,
            command=lambda: self._submit(self._link.shift_up()),
        ).pack(side="left", padx=4)

        perm_row = ctk.CTkFrame(self._debug_pane, fg_color="transparent")
        perm_row.pack(fill="x", padx=8, pady=(0, 4))
        is_mac = sys.platform == "darwin"
        ctk.CTkLabel(perm_row, text="Permissions:").pack(side="left", padx=(4, 8))
        ctk.CTkButton(
            perm_row, text="Accessibility…", width=130,
            command=self._open_accessibility_settings,
            state="normal" if is_mac else "disabled",
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            perm_row, text="Bluetooth…", width=110,
            command=self._open_bluetooth_settings,
            state="normal" if is_mac else "disabled",
        ).pack(side="left", padx=4)
        self._test_kb_btn = ctk.CTkButton(
            perm_row, text="Test keystroke (k)", width=150,
            command=self._test_keystroke,
        )
        self._test_kb_btn.pack(side="left", padx=4)
        ctk.CTkButton(
            perm_row, text="Configure keys…", width=140,
            command=self._open_keymap_dialog,
        ).pack(side="left", padx=4)

        log_box = ctk.CTkTextbox(self._debug_pane, state="disabled")
        log_box.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        handler = TkLogHandler(log_box)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s — %(message)s", "%H:%M:%S"))
        root_log = logging.getLogger()
        root_log.setLevel(logging.INFO)
        root_log.addHandler(handler)

        self._debug_visible = False
        self._refresh_state()

        # Now that the Tk log handler is in place, log the path so the user
        # can find it in the debug pane (not just terminal stderr).
        log.info(
            "macOS Accessibility target (add this in System Settings):\n    %s",
            _accessibility_target(),
        )
        trusted = _accessibility_trusted()
        if trusted is False:
            log.warning("Accessibility NOT granted to this Python process. "
                        "Keyboard mode will not work until you grant it.")
        elif trusted is True:
            log.info("Accessibility is granted — keyboard mode should work.")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_puck_row(
        self,
        parent: ctk.CTkFrame,
        name: str,
        layout: list[tuple[str, Button | None]],
        column: int,
    ) -> tuple[ctk.CTkLabel, dict[Button, ctk.CTkLabel], ctk.CTkLabel]:
        card = ctk.CTkFrame(parent, fg_color="transparent")
        card.grid(row=0, column=column, sticky="nw", padx=8, pady=6)

        title = ctk.CTkFrame(card, fg_color="transparent")
        title.pack(fill="x", anchor="w")

        dot = ctk.CTkLabel(
            title, text="●", text_color=DOT_OFF, font=ctk.CTkFont(size=16), width=18,
        )
        dot.pack(side="left")
        ctk.CTkLabel(
            title, text=name, font=ctk.CTkFont(weight="bold"),
        ).pack(side="left")

        glyphs: dict[Button, ctk.CTkLabel] = {}
        normal_font = ctk.CTkFont()
        for text, button in layout:
            lbl = ctk.CTkLabel(title, text=text, font=normal_font)
            lbl.pack(side="left", padx=0)
            if button is not None:
                glyphs[button] = lbl

        hint = ctk.CTkLabel(card, text="", anchor="w", text_color="gray60")
        hint.pack(fill="x", anchor="w", padx=(22, 0), pady=(0, 0))
        return dot, glyphs, hint

    def _refresh_state(self) -> None:
        """Recompute dot colors, subtitle and the hint line from current state."""
        ble_count = len(self._clicks)
        method = "Keyboard" if self._bridge.mode is OutputMode.KEYBOARD else "OpenBikeControl"
        self._subtitle.configure(
            text=f"Click 2  →  Whoosh Clicker  →  MyWhoosh  ({method})"
        )

        for dot, hint_lbl, identified in (
            (self._left_dot, self._left_hint, self._left_identified),
            (self._right_dot, self._right_hint, self._right_identified),
        ):
            if identified:
                color = DOT_ON
                hint_text = ""
            elif ble_count > 0:
                color = DOT_PENDING
                hint_text = "Confirm connection by pressing a button"
            else:
                color = DOT_OFF
                hint_text = "Connect puck by pressing a button"
            dot.configure(text_color=color)
            hint_lbl.configure(text=hint_text)

        mode = self._bridge.mode
        if mode is OutputMode.KEYBOARD:
            hint = "Keyboard mode — keep MyWhoosh focused while riding."
        elif not self._link_connected:
            hint = "Open MyWhoosh and start a ride — it will connect here."
        else:
            hint = "MyWhoosh connected. Pedal away."
        self._hint_label.configure(text=hint)

    def _flash_glyph(self, puck: Puck, button: Button | None) -> None:
        if button is None:
            return
        glyphs = self._left_glyphs if puck is Puck.LEFT else self._right_glyphs
        lbl = glyphs.get(button)
        if lbl is None:
            return
        lbl.configure(font=ctk.CTkFont(weight="bold"))
        self.after(180, lambda: lbl.configure(font=ctk.CTkFont(weight="normal")))

    def _set_scanning(self, scanning: bool) -> None:
        if scanning:
            self._scan_btn.configure(text="Scanning…", state="disabled")
            self._scan_spinner.pack(side="right", padx=(0, 8))
            self._scan_spinner.start()
        else:
            self._scan_spinner.stop()
            self._scan_spinner.pack_forget()
            self._scan_btn.configure(text="Scan + Connect", state="normal")

    def _toggle_debug_pane(self) -> None:
        if self._debug_visible:
            self._debug_pane.pack_forget()
            self._debug_toggle.configure(text="Show debug ▾")
            self.geometry(self._collapsed_geometry)
        else:
            self._debug_pane.pack(fill="both", expand=True, padx=12, pady=(0, 12))
            self._debug_toggle.configure(text="Hide debug ▴")
            self.geometry(self._expanded_geometry)
        self._debug_visible = not self._debug_visible

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_scan_click(self) -> None:
        self._submit(self._scan_and_connect())

    def _open_accessibility_settings(self) -> None:
        target = _accessibility_target()
        log.info(
            "Drag this into Accessibility (or open the parent folder and select it):\n    %s",
            target,
        )
        self._open_system_settings(
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            "Accessibility",
        )
        # Pop a Finder window at the target so the user can drag it in.
        try:
            subprocess.Popen(["open", "-R", target])
        except Exception:
            log.exception("Could not reveal %s in Finder", target)

    def _open_bluetooth_settings(self) -> None:
        self._open_system_settings(
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Bluetooth",
            "Bluetooth",
        )

    def _open_system_settings(self, url: str, name: str) -> None:
        if sys.platform != "darwin":
            log.warning("System Settings shortcuts are macOS-only")
            return
        log.info("Opening System Settings → %s", name)
        try:
            subprocess.Popen(["open", url])
        except Exception:
            log.exception("Failed to open System Settings")

    def _open_keymap_dialog(self) -> None:
        keyboard = self._bridge.keyboard
        KeymapDialog(
            self,
            current=keyboard.get_mapping(),
            on_apply=keyboard.set_mapping,
        )

    _TEST_COUNTDOWN_SECONDS = 4

    def _test_keystroke(self) -> None:
        # Clicking the button steals focus to our window, so we count down
        # to give the user time to click into MyWhoosh (or any text field).
        log.info(
            "Click into the app you want to test in (MyWhoosh, Notes, anywhere "
            "with a text field). Keystroke fires in %d seconds.",
            self._TEST_COUNTDOWN_SECONDS,
        )
        self._test_kb_btn.configure(state="disabled")
        self._test_keystroke_countdown(self._TEST_COUNTDOWN_SECONDS)

    def _test_keystroke_countdown(self, remaining: int) -> None:
        if remaining <= 0:
            self._fire_test_keystroke()
            return
        self._test_kb_btn.configure(text=f"Firing in {remaining}…")
        self.after(1000, self._test_keystroke_countdown, remaining - 1)

    def _fire_test_keystroke(self) -> None:
        from pynput.keyboard import Controller, KeyCode
        trusted = _accessibility_trusted()
        if trusted is False:
            log.warning(
                "Accessibility is NOT granted to this process. macOS will "
                "silently drop the keystroke. Grant the Python.app bundle in "
                "System Settings → Privacy → Accessibility, then fully quit "
                "and relaunch this app."
            )
        try:
            kb = Controller()
            kb.press(KeyCode.from_char("k"))
            kb.release(KeyCode.from_char("k"))
            if trusted is True:
                log.info("Test keystroke 'k' sent (Accessibility is granted).")
            else:
                log.info("Test keystroke 'k' attempted.")
        except Exception:
            log.exception("Test keystroke failed")
        finally:
            self._test_kb_btn.configure(text="Test keystroke (k)", state="normal")

    def _on_mode_change(self) -> None:
        new = OutputMode(self._mode_var.get())
        self._bridge.mode = new
        log.info("Output mode → %s", new.value)
        self._refresh_state()

    def _on_link_state(self, connected: bool) -> None:
        def apply() -> None:
            self._link_connected = connected
            self._link_status.configure(
                text="MyWhoosh: connected" if connected else "MyWhoosh: waiting…"
            )
            self._refresh_state()
        self.after(0, apply)

    def _on_close(self) -> None:
        async def shutdown() -> None:
            await self._link.stop()
            for task in self._bridge_tasks.values():
                task.cancel()
            for click in self._clicks.values():
                await click.disconnect()
        fut = asyncio.run_coroutine_threadsafe(shutdown(), self._loop)
        try:
            fut.result(timeout=3)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self.destroy()

    # ------------------------------------------------------------------
    # Async work
    # ------------------------------------------------------------------

    async def _start_services(self) -> None:
        await self._link.start()
        await self._scan_and_connect()

    def _on_button_event(self, event: ButtonEvent) -> None:
        # Runs on the asyncio thread. Marshal UI updates onto Tk's main loop.
        self.after(0, self._apply_button_event, event)

    def _apply_button_event(self, event: ButtonEvent) -> None:
        if event.puck is Puck.LEFT and not self._left_identified:
            self._left_identified = True
            self._refresh_state()
        elif event.puck is Puck.RIGHT and not self._right_identified:
            self._right_identified = True
            self._refresh_state()
        if event.is_down:
            self._flash_glyph(event.puck, event.button)

    async def _scan_and_connect(self) -> None:
        self.after(0, self._set_scanning, True)
        log.info("Scanning for Click V2…")
        try:
            devices = await ClickV2.scan()
        finally:
            self.after(0, self._set_scanning, False)
        if not devices:
            log.warning("No Click devices found")
            return
        log.info("Found %d device(s); connecting to all", len(devices))

        async def connect_one(dev) -> None:
            if dev.address in self._clicks:
                log.info("Already connected to %s; skipping", dev.address)
                return
            click = ClickV2()
            try:
                await click.connect(dev)
            except Exception:
                log.exception("Connect failed for %s", dev.address)
                return
            self._clicks[dev.address] = click
            self._bridge_tasks[dev.address] = asyncio.create_task(
                run_bridge(click, self._bridge)
            )
            self.after(0, self._refresh_state)

        await asyncio.gather(*(connect_one(d) for d in devices))

    # ------------------------------------------------------------------
    # Loop plumbing
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro: Coroutine[Any, Any, Any]) -> None:
        asyncio.run_coroutine_threadsafe(coro, self._loop)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()

"""CustomTkinter UI shell.

Top: header + Scan button, two puck cards (dots flip grey→amber→green;
per-glyph bold flash on press). Below: a hint line and a collapsible
debug pane (log, permission shortcuts, test box, keymap dialog).

The asyncio event loop runs in a background thread; UI callbacks marshal
work onto it via asyncio.run_coroutine_threadsafe.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import customtkinter as ctk
from PIL import Image

from .bridge import Bridge, run_bridge
from .click_v2 import Button, ButtonEvent, ClickV2, Puck
from .keyboard_out import KeyboardOutput
from .keymap import format_key
from .keymap_dialog import KeymapDialog

DOT_OFF = "#888888"
DOT_PENDING = "#d8a200"  # connected but puck identity unknown until first press
DOT_ON = "#33aa55"


def _asset(filename: str) -> Path | None:
    """Locate a bundled asset whether we're running from source or a .app.

    PyInstaller extracts data files to `sys._MEIPASS`; from source we look
    next to the project root.
    """
    candidates = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root is not None:
        candidates.append(Path(bundle_root) / "assets" / filename)
    candidates.append(Path(__file__).resolve().parent.parent / "assets" / filename)
    for p in candidates:
        if p.is_file():
            return p
    return None


def _bluetooth_authorized() -> bool | None:
    """True if macOS has granted Bluetooth permission to this process.

    Uses CoreBluetooth's CBManager.authorization (10.13+):
        0=NotDetermined, 1=Restricted, 2=Denied, 3=AllowedAlways.
    None on non-macOS or if the API isn't reachable.
    """
    if sys.platform != "darwin":
        return None
    try:
        from CoreBluetooth import CBManager  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        return CBManager.authorization() == 3
    except Exception:
        return None


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


class _PermissionCache:
    """Caches a permission check so we don't hammer native frameworks.

    Permission grants are rare (user action in System Settings). Once a
    permission resolves to True, we trust it forever; while it's False
    we keep polling so the UI lights up shortly after the user grants.
    """

    def __init__(self, probe):
        self._probe = probe
        self._granted: bool | None = None

    def is_granted(self) -> bool | None:
        if self._granted is True:
            return True
        self._granted = self._probe()
        return self._granted


def _accessibility_target() -> str:
    """Best macOS Accessibility target for the running process.

    - If we're inside a bundled .app (PyInstaller / py2app), return the .app
      path — this is what the user adds to System Settings, and the bundle ID
      makes the grant stick.
    - Otherwise (running under a venv / framework Python), return the
      framework's Python.app bundle.
    - Fallback: the resolved python binary.
    """
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
    ("A", Button.A),
    (" ", None),
    ("B", Button.B),
    (" ", None),
    ("Y", Button.Y),
    (" ", None),
    ("Z", Button.Z),
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

_LAYOUTS: dict[Puck, tuple[str, list[tuple[str, Button | None]]]] = {
    Puck.LEFT: ("Left puck", _LEFT_LAYOUT),
    Puck.RIGHT: ("Right puck", _RIGHT_LAYOUT),
}

# Physical button colors on the left puck (identified by eye on the device).
BUTTON_COLORS: dict[Button, str] = {
    Button.A: "#33aa55",  # green, right of diamond
    Button.B: "#cc33aa",  # magenta, bottom
    Button.Y: "#3366cc",  # blue, top
    Button.Z: "#cc6600",  # orange, left
}


@dataclass
class _PuckUi:
    dot: ctk.CTkLabel
    glyphs: dict[Button, ctk.CTkLabel]
    hint: ctk.CTkLabel
    battery: ctk.CTkLabel
    summary_frame: ctk.CTkFrame
    # button → (glyph label, key label) so we can repaint when keymap changes
    summary_entries: dict[Button, tuple[ctk.CTkLabel, ctk.CTkLabel]]
    identified: bool = False
    last_dot_color: str = DOT_OFF
    last_hint: str = ""
    last_battery: str = ""


log = logging.getLogger(__name__)


class TkLogHandler(logging.Handler):
    """Buffers log lines in a thread-safe queue; the Tk thread drains it.

    Calling Tk APIs (including after()) from a non-Tk thread can deadlock
    the Tcl interpreter, and logging fires from every thread.
    """

    def __init__(self, textbox: ctk.CTkTextbox) -> None:
        super().__init__()
        self._textbox = textbox
        self._pending: queue.Queue[str] = queue.Queue()
        textbox.after(50, self._drain)

    def emit(self, record: logging.LogRecord) -> None:
        self._pending.put(self.format(record) + "\n")

    def _drain(self) -> None:
        msgs: list[str] = []
        try:
            while True:
                msgs.append(self._pending.get_nowait())
        except queue.Empty:
            pass
        if msgs:
            self._textbox.configure(state="normal")
            self._textbox.insert("end", "".join(msgs))
            self._textbox.see("end")
            self._textbox.configure(state="disabled")
        self._textbox.after(50, self._drain)


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Click2Key")
        self._collapsed_geometry = "640x400"
        self._expanded_geometry = "720x700"
        self.geometry(self._collapsed_geometry)

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._loop_thread.start()

        # Cross-thread UI work goes through this queue and is drained on the
        # Tk thread by _drain_ui_queue. Calling Tk APIs (including after())
        # from the asyncio thread can deadlock the Tcl interpreter — observed
        # as both threads parked in __psynch_cvwait.
        self._ui_queue: queue.Queue = queue.Queue()

        # One ClickV2 per BLE-connected puck. Keyed by device address.
        self._clicks: dict[str, ClickV2] = {}
        self._bridge_tasks: dict[str, asyncio.Task] = {}
        self._bridge = Bridge(
            keyboard=KeyboardOutput(),
            ui_sink=self._on_button_event,
        )

        # Per-puck UI state (populated in _build_ui); tri-state dot logic
        # lives in _refresh_state.
        self._pucks: dict[Puck, _PuckUi] = {}
        self._last_hint = ""
        self._bt_perm = _PermissionCache(_bluetooth_authorized)
        self._ax_perm = _PermissionCache(_accessibility_trusted)
        # Reusable fonts so the per-press flash doesn't allocate.
        self._normal_font: ctk.CTkFont | None = None
        self._bold_font: ctk.CTkFont | None = None
        # Log handler captured here so we can detach it on close.
        self._log_handler: logging.Handler | None = None

        self._build_ui()
        self._submit(self._start_services())

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        # Top bar: icon + title on the left, Scan+Connect on the right.
        top_bar = ctk.CTkFrame(self, fg_color="transparent")
        top_bar.pack(fill="x", padx=12, pady=(10, 0))
        light_icon = _asset("title_icon.png")
        dark_icon = _asset("title_icon_dark.png") or light_icon
        if light_icon is not None:
            self._title_icon = ctk.CTkImage(
                light_image=Image.open(light_icon),
                dark_image=Image.open(dark_icon) if dark_icon else Image.open(light_icon),
                size=(28, 28),
            )
            ctk.CTkLabel(top_bar, text="", image=self._title_icon).pack(
                side="left", padx=(0, 8),
            )
        ctk.CTkLabel(
            top_bar, text="Click2Key",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(side="left")
        self._scan_btn = ctk.CTkButton(
            top_bar, text="Scan + Connect", width=140,
            command=self._on_scan_click,
        )
        self._scan_btn.pack(side="right")
        self._scan_spinner = ctk.CTkProgressBar(
            top_bar, mode="indeterminate", width=110,
        )
        # Packed only when at least one puck is BLE-connected. Disconnecting
        # lets the pucks fall back to their 60s sleep and stop draining battery.
        self._disconnect_btn = ctk.CTkButton(
            top_bar, text="Disconnect", width=110,
            command=self._on_disconnect_click,
            fg_color="gray40", hover_color="gray30",
        )

        ctk.CTkLabel(
            self, text="Convert Zwift Click2 buttons into keyboard keys",
            text_color="gray60",
        ).pack(pady=(2, 0))

        # Setup panel — permissions row + getting-started instructions.
        # Hidden once both permissions are granted AND both pucks identified.
        self._setup_panel = ctk.CTkFrame(self)
        self._perm_row = ctk.CTkFrame(self._setup_panel, fg_color="transparent")
        self._perm_row.pack(fill="x", padx=12, pady=(8, 0))
        ctk.CTkLabel(
            self._perm_row, text="Permissions:", font=ctk.CTkFont(weight="bold"),
        ).pack(side="left", padx=(0, 8))
        self._bt_dot, self._bt_fix_btn = self._build_permission_row(
            "Bluetooth", self._open_bluetooth_settings, leading_padx=0,
        )
        self._ax_dot, self._ax_fix_btn = self._build_permission_row(
            "Accessibility", self._open_accessibility_settings, leading_padx=12,
        )

        self._perm_hint = ctk.CTkLabel(
            self._setup_panel, text="", anchor="w", justify="left",
            text_color="#d8a200", wraplength=580,
        )
        self._perm_hint.pack(fill="x", padx=12, pady=(2, 0))
        # Re-wrap when the window is resized.
        self._setup_panel.bind(
            "<Configure>",
            lambda e: self._perm_hint.configure(wraplength=max(200, e.width - 28)),
        )

        ctk.CTkLabel(
            self._setup_panel,
            text=(
                "1. Wake both pucks (long-press any button until the LED is solid blue).\n"
                "2. Click 'Scan + Connect' at the top.\n"
                "3. Press a few buttons on each puck to pair them.\n\n"
                "If a puck stops responding after ~60 seconds, pair it once in the free Zwift\n"
                "app and ride briefly — this permanently fixes the silent-puck issue."
            ),
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=12, pady=(6, 8))
        self._setup_panel.pack(fill="x", padx=12, pady=(8, 0))

        # Two columns of puck cards.
        self._normal_font = ctk.CTkFont()
        self._bold_font = ctk.CTkFont(weight="bold")
        self._pucks_row = ctk.CTkFrame(self)
        self._pucks_row.pack(fill="x", padx=12, pady=(8, 8))
        for column, (puck, (name, layout)) in enumerate(_LAYOUTS.items()):
            self._pucks[puck] = self._build_puck_row(self._pucks_row, name, layout, column)
        self._pucks_row.grid_columnconfigure(0, weight=1)
        self._pucks_row.grid_columnconfigure(1, weight=1)
        self._refresh_summaries()

        self._hint_label = ctk.CTkLabel(
            self, text="", anchor="w", text_color="gray60",
        )
        self._hint_label.pack(fill="x", padx=20, pady=(0, 4))

        # Top-level button row: Configure keys + debug toggle.
        toggle_row = ctk.CTkFrame(self, fg_color="transparent")
        toggle_row.pack(fill="x", padx=12, pady=(2, 8))
        self._debug_toggle = ctk.CTkButton(
            toggle_row, text="Show debug ▾", width=140, height=28,
            command=self._toggle_debug_pane,
        )
        self._debug_toggle.pack(side="right")
        ctk.CTkButton(
            toggle_row, text="Configure keys…", width=140, height=28,
            command=self._open_keymap_dialog,
        ).pack(side="right", padx=(0, 8))

        # Debug pane — created but not packed; toggle pack/pack_forget below.
        self._debug_pane = ctk.CTkFrame(self)

        perm_row = ctk.CTkFrame(self._debug_pane, fg_color="transparent")
        perm_row.pack(fill="x", padx=8, pady=(8, 4))
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

        test_row = ctk.CTkFrame(self._debug_pane, fg_color="transparent")
        test_row.pack(fill="x", padx=8, pady=(0, 4))
        ctk.CTkLabel(test_row, text="Test box:").pack(side="left", padx=(4, 8))
        self._test_box = ctk.CTkEntry(test_row, placeholder_text="click here, then press a puck button")
        self._test_box.pack(side="left", fill="x", expand=True, padx=4)
        ctk.CTkButton(
            test_row, text="Clear", width=70,
            command=lambda: self._test_box.delete(0, "end"),
        ).pack(side="left", padx=4)

        log_box = ctk.CTkTextbox(self._debug_pane, state="disabled")
        log_box.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        handler = TkLogHandler(log_box)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s — %(message)s", "%H:%M:%S"))
        root_log = logging.getLogger()
        root_log.setLevel(logging.INFO)
        root_log.addHandler(handler)
        self._log_handler = handler

        self._debug_visible = False
        self._refresh_state()
        # Cheap periodic poll so permission grants show up without a restart.
        self.after(3000, self._tick_refresh)
        # Pump cross-thread UI work scheduled from the asyncio thread.
        self.after(30, self._drain_ui_queue)

        # Now that the Tk log handler is in place, log the path so the user
        # can find it in the debug pane (not just terminal stderr). macOS-only:
        # Windows has no Accessibility-style grant for synthetic input.
        if sys.platform == "darwin":
            log.info(
                "macOS Accessibility target (add this in System Settings):\n    %s",
                _accessibility_target(),
            )
            trusted = _accessibility_trusted()
            if trusted is False:
                log.warning("Accessibility NOT granted. Keystrokes will be "
                            "silently dropped by macOS until you grant it.")
            elif trusted is True:
                log.info("Accessibility is granted — keystrokes will fire.")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_permission_row(
        self, name: str, on_fix: Callable[[], None], leading_padx: int,
    ) -> tuple[ctk.CTkLabel, ctk.CTkButton]:
        """Dot + label + (visibility-controlled) fix button for one permission."""
        dot = ctk.CTkLabel(
            self._perm_row, text="●", font=ctk.CTkFont(size=14),
            text_color=DOT_OFF, width=14,
        )
        dot.pack(side="left", padx=(leading_padx, 0))
        ctk.CTkLabel(self._perm_row, text=name).pack(side="left", padx=(2, 4))
        # Packed later by _refresh_state when this permission is denied.
        fix_btn = ctk.CTkButton(
            self._perm_row, text=f"Fix {name}", width=130, height=22,
            command=on_fix,
        )
        return dot, fix_btn

    def _build_puck_row(
        self,
        parent: ctk.CTkFrame,
        name: str,
        layout: list[tuple[str, Button | None]],
        column: int,
    ) -> _PuckUi:
        card = ctk.CTkFrame(parent, fg_color="transparent")
        card.grid(row=0, column=column, sticky="nw", padx=8, pady=6)

        title = ctk.CTkFrame(card, fg_color="transparent")
        title.pack(fill="x", anchor="w")

        dot = ctk.CTkLabel(
            title, text="●", text_color=DOT_OFF, font=ctk.CTkFont(size=16), width=18,
        )
        dot.pack(side="left")
        ctk.CTkLabel(title, text=name, font=self._bold_font).pack(side="left")

        glyphs: dict[Button, ctk.CTkLabel] = {}
        for text, button in layout:
            lbl = ctk.CTkLabel(title, text=text, font=self._normal_font)
            lbl.pack(side="left", padx=0)
            if button is not None:
                glyphs[button] = lbl

        battery = ctk.CTkLabel(title, text="", text_color="gray60")
        battery.pack(side="left", padx=(8, 0))

        hint = ctk.CTkLabel(card, text="", anchor="w", text_color="gray60")
        hint.pack(fill="x", anchor="w", padx=(22, 0), pady=(0, 0))

        # Summary row: "A → k   B → b   …" with each button glyph in its
        # device color. Only the mapped buttons from this puck's layout appear.
        summary_frame = ctk.CTkFrame(card, fg_color="transparent")
        summary_frame.pack(fill="x", anchor="w", padx=(22, 0), pady=(2, 0))
        summary_entries: dict[Button, tuple[ctk.CTkLabel, ctk.CTkLabel]] = {}
        for text, button in layout:
            if button is None or text.strip() == "":
                continue
            color = BUTTON_COLORS.get(button, "gray70")
            glyph_lbl = ctk.CTkLabel(
                summary_frame, text=text, text_color=color, font=self._bold_font,
            )
            glyph_lbl.pack(side="left")
            key_lbl = ctk.CTkLabel(summary_frame, text="", text_color="gray60")
            key_lbl.pack(side="left", padx=(2, 10))
            summary_entries[button] = (glyph_lbl, key_lbl)

        return _PuckUi(
            dot=dot, glyphs=glyphs, hint=hint, battery=battery,
            summary_frame=summary_frame, summary_entries=summary_entries,
        )

    def _refresh_summaries(self) -> None:
        mapping = self._bridge.keyboard.get_mapping()
        for ui in self._pucks.values():
            for button, (_glyph, key_lbl) in ui.summary_entries.items():
                key = mapping.get(button)
                key_lbl.configure(text=f"→ {format_key(key)}" if key else "→ —")

    def _refresh_state(self) -> None:
        """Recompute dot colors and hint. Skips no-op .configure() calls."""
        ble_count = len(self._clicks)
        self._toggle_packed(
            self._disconnect_btn, ble_count > 0,
            side="right", padx=(0, 8),
        )

        # Permission status — cached so a granted permission isn't re-probed
        # via CoreBluetooth/AX on every 3s tick. The whole permissions concept
        # is macOS-only; on Windows BLE + keystroke synthesis need no user
        # grant, so treat both as granted to suppress the perm row/buttons.
        if sys.platform != "darwin":
            bt = ax = True
        else:
            bt = self._bt_perm.is_granted()
            ax = self._ax_perm.is_granted()
        perms_ok = bt is True and ax is True

        # Hide the whole permission row when everything's granted. Otherwise
        # show the row plus only the fix buttons for what's actually missing.
        if perms_ok and self._perm_row.winfo_ismapped():
            self._perm_row.pack_forget()
        elif not perms_ok and not self._perm_row.winfo_ismapped():
            self._perm_row.pack(fill="x", padx=12, pady=(8, 0), before=self._perm_hint)

        if not perms_ok:
            self._bt_dot.configure(text_color=DOT_ON if bt is True else DOT_OFF)
            self._ax_dot.configure(text_color=DOT_ON if ax is True else DOT_OFF)
            self._toggle_packed(self._bt_fix_btn, bt is not True)
            self._toggle_packed(self._ax_fix_btn, ax is not True)

        perm_msgs: list[str] = []
        if bt is False or bt is None:
            perm_msgs.append("• Bluetooth not granted — click 'Fix Bluetooth' to enable in System Settings.")
        if ax is False:
            perm_msgs.append(
                "• Accessibility not granted — click 'Fix Accessibility', add Click2Key, then relaunch."
            )
        silent_count = sum(1 for c in self._clicks.values() if c.is_silent)
        if silent_count > 0:
            perm_msgs.append(
                f"• {silent_count} puck(s) silent — connected but no button events for "
                "over a minute. Pair the puck once in the free Zwift app and ride briefly "
                "to permanently fix it."
            )
        self._perm_hint.configure(text="\n".join(perm_msgs))

        # Hide the setup panel once both pucks are identified AND both perms ok.
        all_identified = all(ui.identified for ui in self._pucks.values())
        perms_ok = bt is True and ax is True
        if all_identified and perms_ok and self._setup_panel.winfo_ismapped():
            self._setup_panel.pack_forget()
        elif (not all_identified or not perms_ok) and not self._setup_panel.winfo_ismapped():
            self._setup_panel.pack(fill="x", padx=12, pady=(8, 0), before=self._pucks_row)

        # Map each connected ClickV2 to its identified puck (if any) so we
        # can surface its battery reading on the matching card.
        battery_by_puck: dict[Puck, int] = {}
        for click in self._clicks.values():
            if click.puck_identity is not None and click.battery_percent is not None:
                battery_by_puck[click.puck_identity] = click.battery_percent

        for puck, ui in self._pucks.items():
            if ui.identified:
                color, hint_text = DOT_ON, ""
            elif ble_count > 0:
                color, hint_text = DOT_PENDING, "Confirm connection by pressing a button"
            else:
                color, hint_text = DOT_OFF, "Connect puck by pressing a button"
            if color != ui.last_dot_color:
                ui.dot.configure(text_color=color)
                ui.last_dot_color = color
            if hint_text != ui.last_hint:
                ui.hint.configure(text=hint_text)
                ui.last_hint = hint_text
            pct = battery_by_puck.get(puck)
            battery_text = f"battery {pct}%" if pct is not None else ""
            if battery_text != ui.last_battery:
                ui.battery.configure(text=battery_text)
                ui.last_battery = battery_text

        if all_identified:
            hint = "Ready. Keystrokes go to whichever app has focus."
        else:
            hint = "Bring your target app to focus before pressing puck buttons."
        if hint != self._last_hint:
            self._hint_label.configure(text=hint)
            self._last_hint = hint

    def _tick_refresh(self) -> None:
        self._refresh_state()
        self.after(3000, self._tick_refresh)

    def _post_to_ui(self, fn: Callable[[], None]) -> None:
        """Thread-safe: schedule fn to run on the Tk main thread."""
        self._ui_queue.put(fn)

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    log.exception("UI callback failed")
        except queue.Empty:
            pass
        self.after(30, self._drain_ui_queue)

    @staticmethod
    def _toggle_packed(widget, visible: bool, **pack_kw) -> None:
        """Pack or pack_forget idempotently. Default pack opts: side=left, small left pad."""
        if visible and not widget.winfo_ismapped():
            widget.pack(**(pack_kw or {"side": "left", "padx": (4, 0)}))
        elif not visible and widget.winfo_ismapped():
            widget.pack_forget()

    def _set_glyph_pressed(self, puck: Puck, button: Button | None, pressed: bool) -> None:
        if button is None:
            return
        ui = self._pucks.get(puck)
        if ui is None:
            return
        lbl = ui.glyphs.get(button)
        if lbl is None:
            return
        if pressed:
            lbl.configure(
                font=self._bold_font,
                fg_color=DOT_ON,
                text_color="white",
                corner_radius=4,
            )
        else:
            lbl.configure(
                font=self._normal_font,
                fg_color="transparent",
                text_color=("gray10", "gray90"),
            )

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

    def _on_disconnect_click(self) -> None:
        self._submit(self._disconnect_all())

    async def _disconnect_all(self) -> None:
        log.info("Disconnecting %d puck(s); they will sleep after ~60s.",
                 len(self._clicks))
        for task in self._bridge_tasks.values():
            task.cancel()
        self._bridge_tasks.clear()
        for click in list(self._clicks.values()):
            try:
                await click.disconnect()
            except Exception:
                log.exception("Disconnect failed")
        self._clicks.clear()

        def reset_ui() -> None:
            for ui in self._pucks.values():
                ui.identified = False
            self._refresh_state()
        self._post_to_ui(reset_ui)

    def _open_accessibility_settings(self) -> None:
        target = _accessibility_target()
        log.info(
            "Drag this into Accessibility (or open the parent folder and select it):\n    %s",
            target,
        )
        self._open_system_settings(
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            "Accessibility",
            reveal=target,
        )

    def _open_bluetooth_settings(self) -> None:
        self._open_system_settings(
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Bluetooth",
            "Bluetooth",
        )

    def _open_system_settings(self, url: str, name: str, reveal: str | None = None) -> None:
        if sys.platform != "darwin":
            log.warning("System Settings shortcuts are macOS-only")
            return
        log.info("Opening System Settings → %s", name)
        try:
            subprocess.Popen(["open", url])
        except Exception:
            log.exception("Failed to open System Settings")
        if reveal is not None:
            try:
                subprocess.Popen(["open", "-R", reveal])
            except Exception:
                log.exception("Could not reveal %s in Finder", reveal)

    def _open_keymap_dialog(self) -> None:
        keyboard = self._bridge.keyboard

        def apply(mapping):
            keyboard.set_mapping(mapping)
            self._refresh_summaries()

        KeymapDialog(
            self,
            current=keyboard.get_mapping(),
            on_apply=apply,
        )

    _TEST_COUNTDOWN_SECONDS = 4

    def _test_keystroke(self) -> None:
        # Clicking the button steals focus to our window, so we count down
        # to give the user time to click into their target app.
        log.info(
            "Click into the app you want to test in (Notes, anywhere with "
            "a text field). Keystroke fires in %d seconds.",
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
            key = KeyCode.from_char("k")
            kb = Controller()
            kb.press(key)
            kb.release(key)
            outcome = "sent (Accessibility is granted)" if trusted is True else "attempted"
            log.info("Test keystroke 'k' %s.", outcome)
        except Exception:
            log.exception("Test keystroke failed")
        finally:
            self._test_kb_btn.configure(text="Test keystroke (k)", state="normal")

    def _on_close(self) -> None:
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None

        async def shutdown() -> None:
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
        await self._scan_and_connect()

    def _on_button_event(self, event: ButtonEvent) -> None:
        # Runs on the asyncio thread. Marshal UI updates onto Tk's main loop
        # via a thread-safe queue; Tk's after() is not safe to call across
        # threads and will occasionally deadlock the Tcl interpreter.
        self._post_to_ui(lambda: self._apply_button_event(event))

    def _apply_button_event(self, event: ButtonEvent) -> None:
        ui = self._pucks.get(event.puck)
        log.info(
            "Event: puck=%s button=%s is_down=%s; ui=%s identified=%s",
            event.puck.value,
            event.label,
            event.is_down,
            "found" if ui else "MISSING",
            ui.identified if ui else "n/a",
        )
        if ui is not None and not ui.identified:
            ui.identified = True
            self._refresh_state()
        self._set_glyph_pressed(event.puck, event.button, event.is_down)

    async def _scan_and_connect(self) -> None:
        self._post_to_ui(lambda: self._set_scanning(True))
        log.info("Scanning for Click V2…")
        try:
            devices = await ClickV2.scan()
        finally:
            self._post_to_ui(lambda: self._set_scanning(False))
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
            self._post_to_ui(self._refresh_state)

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

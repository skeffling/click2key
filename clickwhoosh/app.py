"""CustomTkinter UI shell.

Three columns of state: MyWhoosh Link, Click V2, and a scrolling log.
The asyncio event loop runs in a background thread; UI callbacks marshal
work onto it via asyncio.run_coroutine_threadsafe.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Coroutine
from typing import Any

import customtkinter as ctk

from .bridge import EventDeduper, run_bridge
from .click_v2 import Button, ButtonEvent, ClickV2, Puck
from .whoosh_link import LINK_PORT, WhooshLinkServer

DOT_OFF = "#888888"
DOT_PENDING = "#d8a200"  # connected but puck identity unknown until first press
DOT_ON = "#33aa55"

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
        self.title("clickwhoosh")
        self._collapsed_geometry = "640x250"
        self._expanded_geometry = "720x560"
        self.geometry(self._collapsed_geometry)

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._loop_thread.start()

        # One ClickV2 per BLE-connected puck. Keyed by device address.
        self._clicks: dict[str, ClickV2] = {}
        self._bridge_tasks: dict[str, asyncio.Task] = {}
        self._deduper = EventDeduper()
        self._link = WhooshLinkServer(on_connection_change=self._on_link_state)

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

        header = ctk.CTkLabel(self, text="clickwhoosh", font=ctk.CTkFont(size=20, weight="bold"))
        header.pack(pady=(12, 4))
        sub = ctk.CTkLabel(self, text=f"Zwift Click V2 → MyWhoosh (port {LINK_PORT})")
        sub.pack()

        # Two columns of puck cards.
        pucks_row = ctk.CTkFrame(self)
        pucks_row.pack(fill="x", padx=12, pady=(8, 8))
        self._left_dot, self._left_glyphs = self._build_puck_row(
            pucks_row, "Left puck", _LEFT_LAYOUT, column=0,
        )
        self._right_dot, self._right_glyphs = self._build_puck_row(
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

        log_box = ctk.CTkTextbox(self._debug_pane, state="disabled")
        log_box.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        handler = TkLogHandler(log_box)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s — %(message)s", "%H:%M:%S"))
        root_log = logging.getLogger()
        root_log.setLevel(logging.INFO)
        root_log.addHandler(handler)

        self._debug_visible = False
        self._refresh_state()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_puck_row(
        self,
        parent: ctk.CTkFrame,
        name: str,
        layout: list[tuple[str, Button | None]],
        column: int,
    ) -> tuple[ctk.CTkLabel, dict[Button, ctk.CTkLabel]]:
        card = ctk.CTkFrame(parent, fg_color="transparent")
        card.grid(row=0, column=column, sticky="ew", padx=8, pady=6)

        dot = ctk.CTkLabel(
            card, text="●", text_color=DOT_OFF, font=ctk.CTkFont(size=16), width=18,
        )
        dot.pack(side="left")
        ctk.CTkLabel(
            card, text=name, font=ctk.CTkFont(weight="bold"),
        ).pack(side="left")

        glyphs: dict[Button, ctk.CTkLabel] = {}
        normal_font = ctk.CTkFont()
        for text, button in layout:
            lbl = ctk.CTkLabel(card, text=text, font=normal_font)
            lbl.pack(side="left", padx=0)
            if button is not None:
                glyphs[button] = lbl
        return dot, glyphs

    def _refresh_state(self) -> None:
        """Recompute dot colors and the hint line from current state."""
        ble_count = len(self._clicks)

        for dot, identified in (
            (self._left_dot, self._left_identified),
            (self._right_dot, self._right_identified),
        ):
            if identified:
                color = DOT_ON
            elif ble_count > 0:
                color = DOT_PENDING
            else:
                color = DOT_OFF
            dot.configure(text_color=color)

        if not self._link_connected:
            hint = "Open MyWhoosh and start a ride — it will connect here."
        elif not (self._left_identified and self._right_identified):
            hint = "Press a button on each puck to confirm pairing."
        else:
            hint = "Ready. Pedal away."
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
            self.after(0, lambda: self._click_status.configure(text="Click: not found"))
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
                run_bridge(
                    click, self._link,
                    ui_sink=self._on_button_event,
                    deduper=self._deduper,
                )
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

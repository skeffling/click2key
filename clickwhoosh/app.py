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
from .click_v2 import ButtonEvent, ClickV2, Puck
from .whoosh_link import LINK_PORT, WhooshLinkServer

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
        self._collapsed_geometry = "560x260"
        self._expanded_geometry = "640x560"
        self.geometry(self._collapsed_geometry)

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._loop_thread.start()

        # One ClickV2 per BLE-connected puck. Keyed by device address.
        self._clicks: dict[str, ClickV2] = {}
        self._bridge_tasks: dict[str, asyncio.Task] = {}
        self._deduper = EventDeduper()
        self._link = WhooshLinkServer(on_connection_change=self._on_link_state)

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

        row = ctk.CTkFrame(self)
        row.pack(fill="x", padx=12, pady=12)

        self._link_status = ctk.CTkLabel(row, text="MyWhoosh: waiting…")
        self._link_status.pack(side="left", padx=8)

        self._click_status = ctk.CTkLabel(row, text="Click: disconnected")
        self._click_status.pack(side="left", padx=8)

        self._scan_btn = ctk.CTkButton(
            row, text="Scan + Connect", width=160, command=self._on_scan_click
        )
        self._scan_btn.pack(side="right", padx=8)
        self._scan_spinner = ctk.CTkProgressBar(row, mode="indeterminate", width=120)
        # Spinner is created hidden; only pack when scanning.

        pucks_row = ctk.CTkFrame(self)
        pucks_row.pack(fill="x", padx=12, pady=(0, 8))

        self._left_title = ctk.CTkLabel(
            pucks_row, text="Left puck  (+ / A·B·Y·Z)",
            font=ctk.CTkFont(weight="bold"),
        )
        self._left_title.grid(row=0, column=0, sticky="w", padx=8, pady=(6, 0))
        self._left_status = ctk.CTkLabel(pucks_row, text="not seen yet", anchor="w")
        self._left_status.grid(row=1, column=0, sticky="w", padx=8, pady=(0, 6))
        self._left_last = ctk.CTkLabel(pucks_row, text="last button: —", anchor="w")
        self._left_last.grid(row=2, column=0, sticky="w", padx=8, pady=(0, 6))

        self._right_title = ctk.CTkLabel(
            pucks_row, text="Right puck  (− / ↑↓←→)",
            font=ctk.CTkFont(weight="bold"),
        )
        self._right_title.grid(row=0, column=1, sticky="w", padx=8, pady=(6, 0))
        self._right_status = ctk.CTkLabel(pucks_row, text="not seen yet", anchor="w")
        self._right_status.grid(row=1, column=1, sticky="w", padx=8, pady=(0, 6))
        self._right_last = ctk.CTkLabel(pucks_row, text="last button: —", anchor="w")
        self._right_last.grid(row=2, column=1, sticky="w", padx=8, pady=(0, 6))

        pucks_row.grid_columnconfigure(0, weight=1)
        pucks_row.grid_columnconfigure(1, weight=1)

        self._seen_pucks: set[Puck] = set()

        # Toggle row — the only thing visible from the debug pane when collapsed.
        toggle_row = ctk.CTkFrame(self, fg_color="transparent")
        toggle_row.pack(fill="x", padx=12, pady=(0, 8))
        self._debug_toggle = ctk.CTkButton(
            toggle_row, text="Show debug ▾", width=130, height=28,
            fg_color="transparent", border_width=1,
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

        self.protocol("WM_DELETE_WINDOW", self._on_close)

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
        text = "MyWhoosh: connected" if connected else "MyWhoosh: waiting…"
        self.after(0, lambda: self._link_status.configure(text=text))

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
        puck = event.puck
        self._seen_pucks.add(puck)
        verb = "pressed" if event.is_down else "released"
        line = f"last button: {event.label} ({verb})"
        if puck is Puck.LEFT:
            self._left_status.configure(text="connected")
            self._left_last.configure(text=line)
        elif puck is Puck.RIGHT:
            self._right_status.configure(text="connected")
            self._right_last.configure(text=line)
        # Unmapped bits: leave puck status alone (we can't tell which puck);
        # the raw "bitN (verb)" line still shows up in the log textbox.

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

        await asyncio.gather(*(connect_one(d) for d in devices))

        n = len(self._clicks)
        self.after(0, lambda: self._click_status.configure(
            text=f"Click: {n} puck(s) connected"
        ))

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

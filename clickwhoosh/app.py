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

from .bridge import run_bridge
from .click_v2 import ClickV2
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
        self.geometry("560x420")

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._loop_thread.start()

        self._click = ClickV2()
        self._link = WhooshLinkServer(on_connection_change=self._on_link_state)
        self._bridge_task: asyncio.Task | None = None

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

        scan_btn = ctk.CTkButton(row, text="Scan + Connect Click", command=self._on_scan_click)
        scan_btn.pack(side="right", padx=8)

        debug_row = ctk.CTkFrame(self)
        debug_row.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkLabel(debug_row, text="Debug:").pack(side="left", padx=(8, 4))
        ctk.CTkButton(
            debug_row, text="Shift Down", width=110,
            command=lambda: self._submit(self._link.shift_down()),
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            debug_row, text="Shift Up", width=110,
            command=lambda: self._submit(self._link.shift_up()),
        ).pack(side="left", padx=4)

        log_box = ctk.CTkTextbox(self, state="disabled")
        log_box.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        handler = TkLogHandler(log_box)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s — %(message)s", "%H:%M:%S"))
        root_log = logging.getLogger()
        root_log.setLevel(logging.INFO)
        root_log.addHandler(handler)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

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
            await self._click.disconnect()
            if self._bridge_task is not None:
                self._bridge_task.cancel()
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
        self._bridge_task = asyncio.create_task(run_bridge(self._click, self._link))

    async def _scan_and_connect(self) -> None:
        log.info("Scanning for Click V2…")
        devices = await ClickV2.scan()
        if not devices:
            log.warning("No Click devices found")
            self.after(0, lambda: self._click_status.configure(text="Click: not found"))
            return
        target = devices[0]
        log.info("Found %d device(s); using %s", len(devices), target.name)
        try:
            await self._click.connect(target)
        except Exception:
            log.exception("Connect failed")
            self.after(0, lambda: self._click_status.configure(text="Click: error"))
            return
        self.after(0, lambda: self._click_status.configure(text=f"Click: {target.name}"))

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

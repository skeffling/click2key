"""Tk dialog for editing the per-button keyboard mapping.

Each Click button (SHIFT_UP, SHIFT_DOWN, NAV_*) has:
  - a dropdown of MyWhoosh's known shortcuts (presets)
  - an editable Entry showing the current key (e.g. 'k', 'up', 'space')

Custom keys: type any single character or a special name from
clickwhoosh.keymap._SPECIAL_KEYS. Picking a preset fills the entry.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import customtkinter as ctk
from pynput.keyboard import Key, KeyCode

from .click_v2 import Button
from .keymap import (
    DEFAULTS_BY_BUTTON,
    MYWHOOSH_PRESETS,
    format_key,
    parse_key,
    save_keymap,
)

log = logging.getLogger(__name__)


_BUTTON_LABELS: dict[Button, str] = {
    Button.SHIFT_UP:   "Shift up  (+)",
    Button.SHIFT_DOWN: "Shift down  (−)",
    Button.Y:          "Y  (blue,    left puck, top)",
    Button.A:          "A  (green,   left puck, right)",
    Button.B:          "B  (magenta, left puck, bottom)",
    Button.Z:          "Z  (orange,  left puck, left)",
    Button.NAV_UP:     "↑  (right puck, up)",
    Button.NAV_DOWN:   "↓  (right puck, down)",
    Button.NAV_LEFT:   "←  (right puck, left)",
    Button.NAV_RIGHT:  "→  (right puck, right)",
}


class KeymapDialog(ctk.CTkToplevel):
    def __init__(
        self,
        parent: ctk.CTk,
        current: dict[Button, Key | KeyCode],
        on_apply: Callable[[dict[Button, Key | KeyCode]], None],
    ) -> None:
        super().__init__(parent)
        self.title("Configure keyboard mapping")
        self.geometry("560x520")
        self.transient(parent)

        self._on_apply = on_apply
        self._entries: dict[Button, ctk.CTkEntry] = {}
        self._dropdowns: dict[Button, ctk.CTkOptionMenu] = {}

        ctk.CTkLabel(
            self,
            text="Pick a MyWhoosh preset or type any single character / special key name.",
            wraplength=480, anchor="w", text_color="gray60",
        ).pack(fill="x", padx=16, pady=(12, 4))

        grid = ctk.CTkFrame(self, fg_color="transparent")
        grid.pack(fill="both", expand=True, padx=16, pady=8)

        preset_labels = [label for label, _ in MYWHOOSH_PRESETS]
        preset_labels_with_custom = ["Custom…", *preset_labels]

        for row, button in enumerate(_BUTTON_LABELS):
            ctk.CTkLabel(
                grid, text=_BUTTON_LABELS[button], anchor="w",
            ).grid(row=row, column=0, sticky="w", padx=4, pady=4)

            current_key_str = format_key(current.get(button))
            entry = ctk.CTkEntry(grid, width=80)
            entry.insert(0, current_key_str)
            entry.grid(row=row, column=1, padx=4, pady=4, sticky="w")
            self._entries[button] = entry

            dropdown = ctk.CTkOptionMenu(
                grid,
                values=preset_labels_with_custom,
                width=240,
                command=lambda choice, b=button: self._on_preset_picked(b, choice),
            )
            dropdown.set(self._initial_dropdown_value(current_key_str))
            dropdown.grid(row=row, column=2, padx=4, pady=4, sticky="w")
            self._dropdowns[button] = dropdown

        grid.grid_columnconfigure(2, weight=1)

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(fill="x", padx=16, pady=(8, 16))
        ctk.CTkButton(
            button_row, text="Reset to defaults",
            command=self._reset_defaults, width=140,
        ).pack(side="left")
        ctk.CTkButton(
            button_row, text="Cancel", command=self.destroy, width=80,
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            button_row, text="Save", command=self._save, width=80,
        ).pack(side="right")

    # ------------------------------------------------------------------

    def _initial_dropdown_value(self, key_str: str) -> str:
        for label, k in MYWHOOSH_PRESETS:
            if k == key_str:
                return label
        return "Custom…"

    @staticmethod
    def _set_entry(entry: ctk.CTkEntry, text: str) -> None:
        entry.delete(0, "end")
        entry.insert(0, text)

    def _on_preset_picked(self, button: Button, choice: str) -> None:
        if choice == "Custom…":
            return
        for label, k in MYWHOOSH_PRESETS:
            if label == choice:
                self._set_entry(self._entries[button], k)
                return

    def _reset_defaults(self) -> None:
        for button, key_str in DEFAULTS_BY_BUTTON.items():
            if button not in self._entries:
                continue
            self._set_entry(self._entries[button], key_str)
            self._dropdowns[button].set(self._initial_dropdown_value(key_str))

    def _save(self) -> None:
        mapping: dict[Button, Key | KeyCode] = {}
        errors: list[str] = []
        for button, entry in self._entries.items():
            raw = entry.get()
            parsed = parse_key(raw)
            if parsed is None:
                errors.append(f"{_BUTTON_LABELS[button]}: '{raw}' is not a valid key")
                continue
            mapping[button] = parsed
        if errors:
            log.warning("Keymap save aborted:\n  " + "\n  ".join(errors))
            # Re-color invalid entries; very lightweight error UX.
            for b, entry in self._entries.items():
                if parse_key(entry.get()) is None:
                    entry.configure(border_color="#cc3333")
            return
        save_keymap(mapping)
        self._on_apply(mapping)
        self.destroy()

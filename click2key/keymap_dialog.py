"""Tk dialog for editing the per-button keyboard mapping.

Each Click button (SHIFT_UP, SHIFT_DOWN, NAV_*) has:
  - a dropdown of MyWhoosh's known shortcuts (presets)
  - an editable Entry showing the current key (e.g. 'k', 'up', 'space')
  - a Repeat selector (1× / 2× / 3×)

A single global "Delay between repeats (ms)" controls the inter-tap
gap when Repeat > 1.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import customtkinter as ctk

from .click_v2 import Button
from .keymap import (
    DEFAULT_DELAY_MS,
    DEFAULTS_BY_BUTTON,
    MAX_REPEATS,
    MYWHOOSH_PRESETS,
    KeymapConfig,
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


_REPEAT_VALUES: list[str] = [f"{n}×" for n in range(1, MAX_REPEATS + 1)]


def _repeat_label(n: int) -> str:
    return f"{max(1, min(MAX_REPEATS, n))}×"


def _parse_repeat_label(label: str) -> int:
    try:
        return int(label.rstrip("×"))
    except ValueError:
        return 1


class KeymapDialog(ctk.CTkToplevel):
    def __init__(
        self,
        parent: ctk.CTk,
        current: KeymapConfig,
        on_apply: Callable[[KeymapConfig], None],
    ) -> None:
        super().__init__(parent)
        self.title("Configure keyboard mapping")
        self.geometry("700x560")
        self.transient(parent)

        self._on_apply = on_apply
        self._entries: dict[Button, ctk.CTkEntry] = {}
        self._dropdowns: dict[Button, ctk.CTkOptionMenu] = {}
        self._repeats: dict[Button, ctk.CTkOptionMenu] = {}

        ctk.CTkLabel(
            self,
            text=(
                "Pick a MyWhoosh preset or type any single character / "
                "special key name. Set Repeat to 2× or 3× to make one "
                "puck press fire the key multiple times — useful for "
                "shifting several gears at once."
            ),
            wraplength=620, anchor="w", justify="left", text_color="gray60",
        ).pack(fill="x", padx=16, pady=(12, 4))

        # Global delay row.
        delay_row = ctk.CTkFrame(self, fg_color="transparent")
        delay_row.pack(fill="x", padx=16, pady=(4, 0))
        ctk.CTkLabel(
            delay_row, text="Delay between repeats (ms):", anchor="w",
        ).pack(side="left")
        self._delay_entry = ctk.CTkEntry(delay_row, width=80)
        self._delay_entry.insert(0, str(current.delay_ms))
        self._delay_entry.pack(side="left", padx=(8, 0))

        grid = ctk.CTkFrame(self, fg_color="transparent")
        grid.pack(fill="both", expand=True, padx=16, pady=8)

        # Header.
        for col, text in enumerate(("Button", "Key", "Preset", "Repeat")):
            ctk.CTkLabel(
                grid, text=text, anchor="w",
                font=ctk.CTkFont(weight="bold"),
            ).grid(row=0, column=col, sticky="w", padx=4, pady=(0, 4))

        preset_labels = [label for label, _ in MYWHOOSH_PRESETS]
        preset_labels_with_custom = ["Custom…", *preset_labels]

        for row, button in enumerate(_BUTTON_LABELS, start=1):
            ctk.CTkLabel(
                grid, text=_BUTTON_LABELS[button], anchor="w",
            ).grid(row=row, column=0, sticky="w", padx=4, pady=4)

            current_key_str = format_key(current.mapping.get(button))
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

            repeat = ctk.CTkOptionMenu(grid, values=_REPEAT_VALUES, width=60)
            repeat.set(_repeat_label(current.repeats_for(button)))
            repeat.grid(row=row, column=3, padx=4, pady=4, sticky="w")
            self._repeats[button] = repeat

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
            self._repeats[button].set(_repeat_label(1))
        self._set_entry(self._delay_entry, str(DEFAULT_DELAY_MS))

    def _save(self) -> None:
        config = KeymapConfig()
        errors: list[str] = []
        for button, entry in self._entries.items():
            raw = entry.get()
            parsed = parse_key(raw)
            if parsed is None:
                errors.append(f"{_BUTTON_LABELS[button]}: '{raw}' is not a valid key")
                continue
            config.mapping[button] = parsed
            config.repeats[button] = _parse_repeat_label(self._repeats[button].get())
        try:
            delay = int(self._delay_entry.get())
            if delay < 0:
                raise ValueError
            config.delay_ms = delay
        except ValueError:
            errors.append("Delay between repeats must be a non-negative integer")
            self._delay_entry.configure(border_color="#cc3333")
        if errors:
            log.warning("Keymap save aborted:\n  " + "\n  ".join(errors))
            for b, entry in self._entries.items():
                if parse_key(entry.get()) is None:
                    entry.configure(border_color="#cc3333")
            return
        save_keymap(config)
        self._on_apply(config)
        self.destroy()

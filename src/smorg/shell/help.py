"""The `?` overlay: the active tab's key reference.

Shows only the active tab's rows — shell keys already live in the footer.
The caller builds the rows; this module only lays out what it is given.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Static

from smorg.shell.modal import ModalBox

Row = tuple[str, str]
Section = tuple[str, list[Row]]


class HelpOverlay(ModalBox):
    """A bordered key reference; with no tabs, the app's connect hint."""

    DEFAULT_CSS = """
    HelpOverlay > .box {
        max-height: 80%;
    }
    """

    # escape is reserved shell-wide for exactly this (see keys.RESERVED_KEYS).
    BINDINGS = [Binding("escape", "dismiss", "close", show=False)]

    def __init__(self, tab: Section | None, no_tabs_hint: str) -> None:
        super().__init__()
        self._tab = tab
        self._no_tabs_hint = no_tabs_hint

    def compose(self) -> ComposeResult:
        # markup=False: consistent with Panel's rule for server/manifest
        # text, kept even though these strings are all our own.
        body = VerticalScroll(Static(self.body_text(), markup=False), classes="box")
        body.border_title = "keys"
        yield body

    def body_text(self) -> str:
        """Public, like Panel.body_text(), so tests can assert on content directly."""
        if self._tab is None:
            return self._no_tabs_hint
        title, rows = self._tab
        return "\n".join([title, *_rendered(rows)])


def _rendered(rows: list[Row]) -> list[str]:
    if not rows:
        return []
    keys = [key for key, _ in rows]
    width = max(len(key) for key in keys)
    return [f"  {key.ljust(width)}  {label}" for key, (_, label) in zip(keys, rows, strict=True)]


def merge_key_display(existing: str, new: str) -> str:
    """One row's keys, a shared modifier stated once: "⇧ + ↑" + "⇧ + ↓"
    -> "⇧ + ↑/↓"; different modifiers stay fully spelled out. Inputs are
    already-symbolized displays, so factoring compares the " + " notation.
    """
    existing_prefix, _, existing_base = existing.rpartition(" + ")
    new_prefix, _, new_base = new.rpartition(" + ")
    if existing_prefix != new_prefix:
        return f"{existing}/{new}"
    if not existing_prefix:
        return f"{existing_base}/{new_base}"
    return f"{existing_prefix} + {existing_base}/{new_base}"


# Modifier words become glyphs with an explicit " + " separator. Only
# modifiers actually reachable through a real key binding are mapped here —
# add the next one as it shows up in a binding. ctrl is absent because
# Textual fuses it into a caret ("^p"), handled structurally in
# _symbolize_part.
SYMBOLS = {"shift+": "⇧ + ", "super+": "⌘ + "}


def symbolize_key_display(key: str) -> str:
    """Modifier prefixes become symbols joined with an explicit "+":
    "shift+x" -> "⇧ + x", "^p" -> "^ + p", "super+k" -> "⌘ + k"."""
    parts = key.split("/")
    symbolized = [_symbolize_part(part) for part in parts]
    return "/".join(symbolized)


def _symbolize_part(part: str) -> str:
    for word, symbol in SYMBOLS.items():
        if part.startswith(word):
            return symbol + _symbolize_part(part[len(word) :])
    if len(part) > 1 and part.startswith("^"):
        return f"^ + {part[1:]}"
    return part

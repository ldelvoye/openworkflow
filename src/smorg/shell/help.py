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
    keys = [symbolize_key_display(key) for key, _ in rows]
    width = max(len(key) for key in keys)
    return [f"  {key.ljust(width)}  {label}" for key, (_, label) in zip(keys, rows, strict=True)]


def merge_key_display(existing: str, new: str) -> str:
    """One row's keys, a shared modifier stated once: "shift+↑" + "shift+↓"
    -> "shift+↑/↓"; different modifiers stay fully spelled out. Must run
    before symbolize_key_display — factoring compares the "+"-notation.
    """
    existing_prefix, _, existing_base = existing.rpartition("+")
    new_prefix, _, new_base = new.rpartition("+")
    if existing_prefix != new_prefix:
        return f"{existing}/{new}"
    if not existing_prefix:
        return f"{existing_base}/{new_base}"
    return f"{existing_prefix}+{existing_base}/{new_base}"


# Only shift has a binding today; add modifiers here as they appear.
SYMBOLS = {"shift+": "⇧ + "}


def symbolize_key_display(key: str) -> str:
    """Modifier words become glyphs: "shift+↑/↓" -> "⇧ + ↑/↓"."""
    parts = key.split("/")
    for index, part in enumerate(parts):
        for word, symbol in SYMBOLS.items():
            if part.startswith(word):
                parts[index] = symbol + part[len(word) :]
                break
    return "/".join(parts)

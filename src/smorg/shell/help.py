"""The `?` overlay: the active tab's key reference."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Static

from smorg.shell.modal import ModalBox

Row = tuple[str, str]
Section = tuple[str, list[Row]]


_SYMBOLS = {"shift+": "⇧ + ", "super+": "⌘ + "}


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
        return "\n".join([title, *_format_rows(rows)])


def _format_rows(rows: list[Row]) -> list[str]:
    if not rows:
        return []
    width = max(len(key) for key, _ in rows)
    return [f"  {key.ljust(width)}  {label}" for key, label in rows]


def merge_key_display(existing: str, new: str) -> str:
    """One row's keys, a shared modifier stated once: "⇧ + ↑" + "⇧ + ↓" -> "⇧ + ↑/↓"."""
    existing_prefix, _, existing_base = existing.rpartition(" + ")
    new_prefix, _, new_base = new.rpartition(" + ")
    if existing_prefix != new_prefix:
        return f"{existing}/{new}"
    if not existing_prefix:
        return f"{existing_base}/{new_base}"
    return f"{existing_prefix} + {existing_base}/{new_base}"


def symbolize_key_display(key: str) -> str:
    """Modifier prefixes become symbols joined with an explicit "+": "shift+x" -> "⇧ + x",
    "^p" -> "^ + p", "super+k" -> "⌘ + k".
    """
    parts = key.split("/")
    symbolized = [_symbolize_part(part) for part in parts]
    return "/".join(symbolized)


def _symbolize_part(part: str) -> str:
    for word, symbol in _SYMBOLS.items():
        if part.startswith(word):
            return symbol + _symbolize_part(part[len(word) :])
    if len(part) > 1 and part.startswith("^"):
        return f"^ + {part[1:]}"
    return part

"""The `?` overlay: the active tab's integration key reference.

Replaces Textual's own help panel (the one listing every binding unsorted,
reachable only through the "Keys" command palette entry — see
OflowApp.get_system_commands, which drops it). Shell keys are not repeated
here — the footer already shows them — so this overlay carries only the
active tab's rows. Rows are built by the caller from live Binding/Action
objects (see OflowApp.action_help) so this module carries no
integration-specific knowledge; it only lays out what it is given.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

Row = tuple[str, str]
Section = tuple[str, list[Row]]


class HelpOverlay(ModalScreen[None]):
    """A bordered key reference for the active tab's integration.

    With no tabs configured there is no integration to reference, so the
    overlay shows the same connect hint the app's own empty state does.
    """

    DEFAULT_CSS = """
    HelpOverlay {
        align: center middle;
    }

    HelpOverlay > VerticalScroll {
        width: auto;
        height: auto;
        max-width: 64;
        max-height: 80%;
        border: round $primary;
        padding: 1 2;
        /* Solid, not alpha: ModalScreen's own 60%-alpha backdrop resolves to
         * transparent under :ansi (alpha can't blend over ANSI colors), so
         * this panel needs its own opaque background instead of relying on
         * that backdrop — the same background Screen itself falls back to
         * under :ansi.
         */
        background: $background;
        &:ansi {
            background: ansi_default;
        }
    }

    /* Static has no width of its own, so inside this auto-width parent it
     * falls back to filling a not-yet-known width — a circular dependency
     * that resolves to 0 and is why the box rendered empty. An explicit auto
     * width breaks the cycle by sizing the Static to its own content instead.
     */
    HelpOverlay Static {
        width: auto;
    }
    """

    # escape is reserved shell-wide for exactly this (see contract.RESERVED_KEYS).
    BINDINGS = [Binding("escape", "dismiss", "close", show=False)]

    def __init__(self, tab: Section | None, no_tabs_hint: str) -> None:
        super().__init__()
        self._tab = tab
        self._no_tabs_hint = no_tabs_hint

    def compose(self) -> ComposeResult:
        # markup=False: consistent with Panel's rule for server/manifest text,
        # even though every string rendered here is our own in-repo binding
        # description or manifest label.
        body = VerticalScroll(Static(self.body_text(), markup=False))
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
    width = max(len(key) for key, _ in rows)
    return [f"  {key.ljust(width)}  {label}" for key, label in rows]

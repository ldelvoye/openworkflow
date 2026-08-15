"""The shared chrome every tab renders inside.

Four states that must never look alike: a tab with nothing in it and a tab whose
fetch failed are different facts, and a dashboard that blurs them cannot be
trusted. Integrations override render_ready and inherit everything else.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from rich.text import Text
from textual.app import ComposeResult, RenderResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from oflow.core.contract import Item


class PanelState(StrEnum):
    LOADING = "loading"
    READY = "ready"
    EMPTY = "empty"
    ERROR = "error"
    STALE = "stale"


class _PanelBody(Static):
    """Draws the panel's list and state text; owns no state of its own."""

    def __init__(self, panel: Panel) -> None:
        # markup off: message carries server-controlled text (e.g. an
        # IntegrationError's str()), so a provider must not be able to style,
        # hide, or garble the panel by putting Rich markup in an error message.
        super().__init__(markup=False, id="body")
        self._panel = panel

    def render(self) -> RenderResult:
        if self._panel.state is PanelState.READY:
            return self._panel.render_ready()
        return self._panel.body_text()


class Panel(Vertical):
    DEFAULT_CSS = """
    Panel > #body { height: 1fr; }
    Panel > #detail { display: none; height: 40%; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.state = PanelState.LOADING
        self.items: tuple[Item, ...] = ()
        self.message = ""
        self.as_of: datetime | None = None

    def compose(self) -> ComposeResult:
        yield _PanelBody(self)
        detail = VerticalScroll(Static(markup=False, id="detail-content"), id="detail")
        # The panel keeps focus; the region is scrolled through panel actions,
        # never focused itself.
        detail.can_focus = False
        yield detail

    def render_ready(self) -> Text:
        """Overridden by an integration. The base draws identities only."""
        return Text("\n".join(item.id for item in self.items))

    def body_text(self) -> str:
        if self.state is PanelState.LOADING:
            return "loading…"
        if self.state is PanelState.EMPTY:
            return "nothing assigned to you"
        if self.state is PanelState.ERROR:
            return f"could not load: {self.message}"
        if self.state is PanelState.STALE:
            stamp = self.as_of.strftime("%H:%M") if self.as_of else "earlier"
            return (
                f"showing data as of {stamp} — {self.message}\n{self.render_ready().plain.strip()}"
            )
        return self.render_ready().plain.strip()

    def refresh(
        self, *regions, repaint: bool = True, layout: bool = False, recompose: bool = False
    ):
        # The shell and the cursor actions refresh the panel; what actually
        # needs repainting is the body child, which caches its own render.
        if self.is_mounted:
            self.query_one("#body", Static).refresh(repaint=repaint, layout=layout)
        return super().refresh(*regions, repaint=repaint, layout=layout, recompose=recompose)

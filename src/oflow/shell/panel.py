"""The shared chrome every tab renders inside.

Four states that must never look alike: a tab with nothing in it and a tab whose
fetch failed are different facts, and a dashboard that blurs them cannot be
trusted. Integrations override render_items and inherit everything else.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from textual.app import RenderResult
from textual.widgets import Static

from oflow.contract import Item


class PanelState(StrEnum):
    LOADING = "loading"
    READY = "ready"
    EMPTY = "empty"
    ERROR = "error"
    STALE = "stale"


class Panel(Static):
    def __init__(self) -> None:
        # message carries server-controlled text (e.g. an IntegrationError's
        # str()), so markup stays off: a provider must not be able to style,
        # hide, or garble the panel by putting Rich markup in an error message.
        super().__init__(markup=False)
        self.state = PanelState.LOADING
        self.items: tuple[Item, ...] = ()
        self.message = ""
        self.as_of: datetime | None = None

    def render_items(self) -> str:
        """Overridden by an integration. The base draws identities only."""
        return "\n".join(item.id for item in self.items)

    def body_text(self) -> str:
        if self.state is PanelState.LOADING:
            return "loading…"
        if self.state is PanelState.EMPTY:
            return "nothing assigned to you"
        if self.state is PanelState.ERROR:
            return f"could not load: {self.message}"
        if self.state is PanelState.STALE:
            stamp = self.as_of.strftime("%H:%M") if self.as_of else "earlier"
            return f"showing data as of {stamp} — {self.message}\n{self.render_items()}"
        return self.render_items()

    def render(self) -> RenderResult:
        # A wider return type than body_text()'s str: an integration's panel can
        # override this to return a styled rich.text.Text for its ready state
        # while body_text() itself stays str-returning for tests to assert on.
        return self.body_text()

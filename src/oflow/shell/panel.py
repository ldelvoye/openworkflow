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
from textual.message import Message
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
    class DetailRequested(Message):
        """A panel asked the shell to fetch one item's detail on its behalf —
        the panel itself never talks to the network."""

        def __init__(self, panel: Panel, item: Item) -> None:
            super().__init__()
            self.panel = panel
            self.item = item

    DEFAULT_CSS = """
    Panel > #body { height: 1fr; }
    Panel > #detail { display: none; height: 40%; border-top: solid $primary; }
    Panel > #detail.-open { display: block; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.state = PanelState.LOADING
        self.items: tuple[Item, ...] = ()
        self.message = ""
        self.as_of: datetime | None = None
        self.detail_open = False
        self._detail_target: tuple[str, str] | None = None
        self._detail_pending: tuple[str, str] | None = None
        self._details: dict[tuple[str, str], object] = {}
        self._detail_errors: dict[tuple[str, str], str] = {}
        self._detail_anchor: tuple[bool, tuple[str, str] | None] | None = None

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

    @staticmethod
    def detail_key(item: Item) -> tuple[str, str]:
        # updated_at is part of the key so an issue that moved on refetches
        # instead of showing a stale cached detail.
        return (item.id, item.updated_at.isoformat())

    def selected_item(self) -> Item | None:
        """Overridden by an integration with a selection; the base has none."""
        return None

    def render_detail(self, item: Item, detail: object) -> Text:
        """Overridden by an integration. The base names the item only."""
        return Text(item.id)

    def action_toggle_detail(self) -> None:
        item = self.selected_item()
        if item is None:
            return
        key = self.detail_key(item)
        if self.detail_open and key == self._detail_target:
            self.close_detail()
            return
        self.detail_open = True
        self._detail_target = key
        if key not in self._details:
            self._detail_errors.pop(key, None)
            self._detail_pending = key
            self.post_message(self.DetailRequested(self, item))
        self._refresh_detail()

    def close_detail(self) -> None:
        self.detail_open = False
        self._detail_target = None
        self._refresh_detail()

    def show_detail(self, key: tuple[str, str], detail: object) -> None:
        self._details[key] = detail
        self._detail_errors.pop(key, None)
        if self._detail_pending == key:
            self._detail_pending = None
        self._refresh_detail()

    def show_detail_error(self, key: tuple[str, str], message: str) -> None:
        self._detail_errors[key] = message
        if self._detail_pending == key:
            self._detail_pending = None
        self._refresh_detail()

    def action_scroll_detail_up(self) -> None:
        if self.detail_open and self.is_mounted:
            self.query_one("#detail", VerticalScroll).scroll_relative(y=-1, animate=False)

    def action_scroll_detail_down(self) -> None:
        if self.detail_open and self.is_mounted:
            self.query_one("#detail", VerticalScroll).scroll_relative(y=1, animate=False)

    def _detail_renderable(self) -> Text:
        item = self.selected_item()
        if item is None or not self.detail_open:
            return Text()
        key = self.detail_key(item)
        if key in self._details:
            return self.render_detail(item, self._details[key])
        if self._detail_pending == key:
            return Text("loading…")
        if key in self._detail_errors:
            return Text(f"could not load: {self._detail_errors[key]}")
        return Text("press enter to load")

    def _refresh_detail(self) -> None:
        if not self.is_mounted:
            return
        region = self.query_one("#detail", VerticalScroll)
        region.set_class(self.detail_open, "-open")
        self.query_one("#detail-content", Static).update(self._detail_renderable())
        # Panel.refresh() calls this for reasons unrelated to the detail
        # region (shell repaints, focus regain); only reset scroll when the
        # shown subject actually changes, or an unread mid-scroll gets wiped
        # on every unrelated repaint. Data arriving for the same key needs no
        # reset — the region was still showing the one-line loading/hint text.
        item = self.selected_item()
        anchor = (self.detail_open, self.detail_key(item) if item is not None else None)
        if anchor != self._detail_anchor:
            self._detail_anchor = anchor
            region.scroll_home(animate=False)

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
        # Cursor moves also repaint the detail region's cache/hint view.
        if self.is_mounted:
            self.query_one("#body", Static).refresh(repaint=repaint, layout=layout)
            self._refresh_detail()
        return super().refresh(*regions, repaint=repaint, layout=layout, recompose=recompose)

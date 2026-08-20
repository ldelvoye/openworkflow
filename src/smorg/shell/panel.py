"""The shared chrome every tab renders inside.

A tab with nothing in it and a tab whose fetch failed must never look alike, so the panel keeps
an explicit state for each situation. Integrations override render_ready and inherit everything
else.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from rich.console import RenderableType
from rich.text import Text
from textual import events
from textual.app import ComposeResult, RenderResult
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Static

from smorg.core.config import ConfigError
from smorg.core.contract import Item
from smorg.core.state import SeenState


class PanelState(StrEnum):
    LOADING = "loading"
    READY = "ready"
    EMPTY = "empty"
    ERROR = "error"
    STALE = "stale"


def _scroll_indicators(scroll_y: float, max_scroll_y: float) -> tuple[bool, bool]:
    return scroll_y > 0, scroll_y < max_scroll_y


def _format_gutter(scroll_y: float, max_scroll_y: float, height: int) -> Text:
    show_up, show_down = _scroll_indicators(scroll_y, max_scroll_y)
    lines = [" "] * max(height, 0)
    if lines and show_up:
        lines[0] = "↑"
    if lines and show_down:
        lines[-1] = "↓"
    return Text("\n".join(lines), style="dim")


class _PanelBody(Static):
    """Draws the panel's list and state text; owns no state of its own."""

    def __init__(self, panel: Panel) -> None:
        # markup off: message may carry server-controlled text, so a provider can't style, hide,
        # or garble the panel via Rich markup.
        super().__init__(markup=False, id="body")
        self._panel = panel

    def render(self) -> RenderResult:
        if self._panel.state is PanelState.READY:
            return self._panel.render_ready()
        return self._panel.body_text()


class _DetailGutter(Static):
    """1-width column on the right of the detail scroll container"""

    DEFAULT_CSS = """
    _DetailGutter { dock: right; width: 1; height: 1fr; }
    """

    def __init__(self) -> None:
        super().__init__(markup=False)

    def on_mount(self) -> None:
        region = self.parent
        if isinstance(region, VerticalScroll):
            self.watch(region, "scroll_y", self.refresh_arrows, init=False)
        self.refresh_arrows()

    def on_resize(self, event: events.Resize) -> None:
        self.refresh_arrows()

    def refresh_arrows(self, *_: object) -> None:
        region = self.parent
        if not isinstance(region, VerticalScroll):
            return
        self.update(_format_gutter(region.scroll_y, region.max_scroll_y, self.size.height))


class Panel(Vertical):
    class DetailRequested(Message):
        def __init__(self, panel: Panel, item: Item) -> None:
            super().__init__()
            self.panel = panel
            self.item = item

    DEFAULT_CSS = """
    Panel > #body { height: 1fr; }
    Panel > #detail {
        display: none;
        height: 60%;
        border-top: solid $primary;
        /* Hidden — the gutter widget shows scroll position instead; mouse
         * wheel and shift+up/down still work since both scroll the
         * container's offset directly rather than dragging the bar. */
        scrollbar-size-vertical: 0;
    }
    Panel > #detail.-open { display: block; }
    /* One blank row so the last line of detail content never sits flush
     * against the region's bottom edge. */
    Panel > #detail > #detail-content { padding-bottom: 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.state = PanelState.LOADING
        self.items: tuple[Item, ...] = ()
        self.message = ""
        self.as_of: datetime | None = None
        self.seen = SeenState({})
        self.integration_id = ""
        self.detail_open = False
        self._detail_target: tuple[str, str] | None = None
        self._detail_pending: tuple[str, str] | None = None
        self._details: dict[tuple[str, str], object] = {}
        self._detail_errors: dict[tuple[str, str], str] = {}
        self._detail_anchor: tuple[bool, tuple[str, str] | None] | None = None

    def compose(self) -> ComposeResult:
        yield _PanelBody(self)
        detail = VerticalScroll(
            Static(markup=False, id="detail-content"), _DetailGutter(), id="detail"
        )
        # The panel keeps focus; the region is scrolled through panel actions,
        # never focused itself.
        detail.can_focus = False
        yield detail

    def render_ready(self) -> RenderableType:
        """The tab's body in the READY state, for an integration to override."""
        return Text(self.ready_text())

    def ready_text(self) -> str:
        """Same view as `render_ready()` but flattened to plain text.

        Override it alongside any `render_ready()` that does not return a Text, such as a grid.
        """
        return "\n".join(item.id for item in self.items)

    @staticmethod
    def detail_key(item: Item) -> tuple[str, str]:
        return (item.id, item.updated_at.isoformat())

    def selected_item(self) -> Item | None:
        """Overridden by an integration with a selection; the base has none."""
        return None

    def render_detail(self, item: Item, detail: object) -> RenderableType:
        """Overridden by an integration. The base names the item only."""
        return Text(item.id)

    def is_detail_showing(self, item: Item) -> bool:
        return self.detail_open and self._detail_target == self.detail_key(item)

    def mark_seen(self, item: Item) -> None:
        self.seen.mark_seen(self.integration_id, item)
        self._save_seen()
        self.refresh()

    def mark_all_seen(self) -> None:
        """Mark every currently-shown item seen and persist them."""
        self.seen.mark_all_seen(self.integration_id, self.items)
        self._save_seen()
        self.refresh()

    def mark_unseen(self) -> None:
        item = self.selected_item()
        if item is None:
            return
        self.seen.mark_unseen(self.integration_id, item)
        self._save_seen()
        self.refresh()

    def _save_seen(self) -> None:
        # A save failure notifies instead of crashing: the marks already took effect in memory,
        # so the panel keeps running either way.
        try:
            self.seen.save()
        except (ConfigError, OSError) as error:
            self.notify(str(error), severity="error")

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

    def prune_detail_cache(self) -> None:
        """Drop cached detail/errors for items no longer in `self.items`.

        Call after assigning fresh items, so stale keys don't accumulate.
        """
        live_keys = {self.detail_key(item) for item in self.items}
        if self._detail_target is not None:
            live_keys.add(self._detail_target)
        self._details = {key: value for key, value in self._details.items() if key in live_keys}
        self._detail_errors = {
            key: message for key, message in self._detail_errors.items() if key in live_keys
        }

    def action_scroll_detail_up(self) -> None:
        if self.detail_open and self.is_mounted:
            self.query_one("#detail", VerticalScroll).scroll_relative(y=-1, animate=False)

    def action_scroll_detail_down(self) -> None:
        if self.detail_open and self.is_mounted:
            self.query_one("#detail", VerticalScroll).scroll_relative(y=1, animate=False)

    def _format_detail(self) -> RenderableType:
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
        self.query_one("#detail-content", Static).update(self._format_detail())
        # Only reset scroll when the shown subject changes. Panel.refresh() also runs for
        # unrelated reasons (shell repaints, focus regain), and resetting on every one would
        # throw away the reader's scroll position.
        item = self.selected_item()
        anchor = (self.detail_open, self.detail_key(item) if item is not None else None)
        if anchor != self._detail_anchor:
            self._detail_anchor = anchor
            region.scroll_home(animate=False)
        # New content can move max_scroll_y without moving scroll_y (for example "loading…"
        # replaced by the real detail), and the gutter only watches scroll_y, so its arrows are
        # refreshed explicitly. Deferred until after the next refresh because virtual_size is
        # only recomputed when layout runs.
        self.call_after_refresh(self.query_one(_DetailGutter).refresh_arrows)

    def body_text(self) -> str:
        if self.state is PanelState.LOADING:
            return "loading…"
        if self.state is PanelState.EMPTY:
            return "nothing assigned to you"
        if self.state is PanelState.ERROR:
            return f"could not load: {self.message}"
        if self.state is PanelState.STALE:
            stamp = self.as_of.strftime("%H:%M") if self.as_of else "earlier"
            return f"showing data as of {stamp} — {self.message}\n{self.ready_text()}"
        return self.ready_text()

    def refresh(
        self, *regions, repaint: bool = True, layout: bool = False, recompose: bool = False
    ):
        # Repaints the body child (which caches its own render) and the detail region's view.
        if self.is_mounted:
            self.query_one("#body", Static).refresh(repaint=repaint, layout=layout)
            self._refresh_detail()
        return super().refresh(*regions, repaint=repaint, layout=layout, recompose=recompose)

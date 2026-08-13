"""The dashboard shell: tabs, the global keymap, and nothing integration-specific."""

from __future__ import annotations

import webbrowser
from datetime import datetime

import httpx
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Static, TabbedContent, TabPane

from oflow.auth.store import CredentialStoreError, get_credentials, now
from oflow.config import ConfigError
from oflow.contract import IntegrationError, Item
from oflow.registry import UnknownIntegration, get_integration
from oflow.shell.panel import Panel, PanelState
from oflow.state import SeenState


class OflowApp(App[None]):
    CSS = """
    Screen { layout: vertical; }
    """

    # priority=True is checked ahead of the focused widget, so a panel cannot
    # capture these by binding the same key.
    BINDINGS = [
        Binding("q", "quit", "quit", priority=True),
        Binding("question_mark", "help", "help", priority=True),
        Binding("tab", "next_tab", "next tab", priority=True),
        Binding("shift+tab", "previous_tab", "previous tab", priority=True),
        Binding("r", "refresh", "refresh", priority=True),
        Binding("o", "open", "open in browser"),
    ]

    def __init__(self, tabs: tuple[str, ...]) -> None:
        super().__init__()
        self.tab_ids = tabs
        self.empty_hint = "no tabs configured — run: oflow connect <integration>"
        self.seen = SeenState({})
        self._fetched_at: dict[str, datetime] = {}

    @property
    def active_tab(self) -> str | None:
        if not self.tab_ids:
            return None
        return self.query_one(TabbedContent).active or None

    def compose(self) -> ComposeResult:
        if not self.tab_ids:
            yield Vertical(Static(self.empty_hint))
            yield Footer()
            return
        with TabbedContent(initial=self.tab_ids[0]):
            for tab in self.tab_ids:
                with TabPane(tab, id=tab):
                    yield self._panel_for(tab)
        yield Footer()

    def _panel_for(self, integration_id: str) -> Panel:
        try:
            integration = get_integration(integration_id)
        except UnknownIntegration:
            # A config naming an integration this build dropped still opens; the
            # tab says so rather than the app refusing to start.
            panel = Panel()
            panel.state = PanelState.ERROR
            panel.message = f"{integration_id} is not supported by this build"
            return panel
        return integration.panel_class()

    def on_mount(self) -> None:
        # Loaded once and handed to every panel that tracks it; a panel with no
        # such attribute (the base Panel) is left alone. setattr rather than a
        # direct assignment: the base Panel type does not declare `seen`, so a
        # direct `panel.seen = ...` does not type-check against it.
        self.seen = SeenState.load()
        for panel in self.query(Panel):
            if hasattr(panel, "seen"):
                setattr(panel, "seen", self.seen)  # noqa: B010

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        # TabbedContent posts this for the initial pane during mount as well as
        # on every later switch, so this one handler is the sole fetch trigger —
        # an explicit on_mount() refresh alongside it would double-fetch the
        # startup tab. It doubles as the focus handoff so a panel's own arrow
        # bindings work the moment its tab becomes visible, startup included.
        panel = event.pane.query_one(Panel)
        self.refresh_tab(event.pane.id or "")
        panel.focus()

    def on_app_focus(self) -> None:
        """The terminal regained focus; refresh whatever has gone stale.

        Fires only where the terminal reports focus. Where it does not, this
        degrades to tab-switch and manual refresh, which is enough.
        """
        if self.active_tab:
            self.refresh_tab(self.active_tab)

    def _shift_tab(self, offset: int) -> None:
        if not self.tab_ids:
            return
        tabs = self.query_one(TabbedContent)
        index = self.tab_ids.index(tabs.active)
        tabs.active = self.tab_ids[(index + offset) % len(self.tab_ids)]

    def action_next_tab(self) -> None:
        self._shift_tab(1)

    def action_previous_tab(self) -> None:
        self._shift_tab(-1)

    def action_help(self) -> None:
        self.notify("tab/shift+tab switch tabs · r refresh · o open · q quit")

    def action_refresh(self) -> None:
        if self.active_tab:
            self.refresh_tab(self.active_tab, force=True)

    def action_open(self) -> None:
        integration_id = self.active_tab or ""
        panel = self._panel_of(integration_id)
        if panel is None:
            return
        url = getattr(panel, "selected_url", lambda: None)()
        if not url:
            return
        webbrowser.open(url)

        item = next((entry for entry in panel.items if entry.url == url), None)
        if item is None:
            return
        self.seen.mark_seen(integration_id, item)
        try:
            self.seen.save()
        except (ConfigError, CredentialStoreError) as error:
            # The in-memory mark above still stands; only persisting it failed,
            # so this degrades to a notice rather than losing the open action.
            self.notify(str(error), severity="error")
        panel.refresh()

    @work(thread=True)
    def refresh_tab(self, integration_id: str, force: bool = False) -> None:
        panel = self._panel_of(integration_id)
        if panel is None:
            return
        try:
            integration = get_integration(integration_id)
        except UnknownIntegration:
            # _panel_for already put this tab in its own error state; there is
            # nothing this integration id could fetch.
            return

        fetched_at = self._fetched_at.get(integration_id)
        if not force and fetched_at is not None:
            if now() - fetched_at < integration.manifest.stale_after:
                return

        try:
            credentials = get_credentials(integration_id)
        except CredentialStoreError as error:
            self.call_from_thread(self._show_error, panel, str(error), keep_items=True)
            return
        if credentials is None:
            self.call_from_thread(self._show_error, panel, "not connected")
            return
        try:
            with httpx.Client(timeout=30) as http:
                items = tuple(integration.fetch(credentials, http))
        except IntegrationError as error:
            self.call_from_thread(self._show_error, panel, str(error), keep_items=True)
            return

        self._fetched_at[integration_id] = now()
        self.call_from_thread(self._show_items, panel, items)

    def _show_items(self, panel: Panel, items: tuple[Item, ...]) -> None:
        panel.items = items
        panel.state = PanelState.EMPTY if not items else PanelState.READY
        panel.as_of = now()
        # layout=True: Static is auto-height, and a plain refresh repaints the
        # panel at its existing size — a panel that started 1 line tall showing
        # "loading…" would otherwise never grow to fit real content.
        panel.refresh(layout=True)

    def _show_error(self, panel: Panel, message: str, keep_items: bool = False) -> None:
        panel.message = message
        # Last-good data is kept and marked stale rather than blanked: a tab that
        # empties on a network blip reads as "nothing to do", which is a lie.
        panel.state = PanelState.STALE if keep_items and panel.items else PanelState.ERROR
        panel.refresh(layout=True)

    def _panel_of(self, integration_id: str) -> Panel | None:
        for pane in self.query(TabPane):
            if pane.id == integration_id:
                return pane.query_one(Panel)
        return None

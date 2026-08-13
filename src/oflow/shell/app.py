"""The dashboard shell: tabs, the global keymap, and nothing integration-specific."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Static, TabbedContent, TabPane

from oflow.registry import UnknownIntegration, get_integration
from oflow.shell.panel import Panel, PanelState


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
    ]

    def __init__(self, tabs: tuple[str, ...]) -> None:
        super().__init__()
        self.tab_ids = tabs
        self.empty_hint = "no tabs configured — run: oflow connect <integration>"

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
        self.notify("tab/shift+tab switch tabs · r refresh · q quit")

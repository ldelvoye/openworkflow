"""The dashboard shell: tabs, the global keymap, and nothing integration-specific."""

from __future__ import annotations

import io
from collections.abc import Iterable
from datetime import datetime

import httpx
from rich.console import Console
from rich.terminal_theme import TerminalTheme
from textual import work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Static, TabbedContent, TabPane

from oflow.auth.refresh import fresh_credentials
from oflow.auth.store import CredentialStoreError, now
from oflow.core.config import TabConfig
from oflow.core.contract import (
    SHELL_KEYS,
    Action,
    AuthExpired,
    IntegrationError,
    Item,
    Malformed,
)
from oflow.core.registry import UnknownIntegration, get_integration
from oflow.core.state import SeenState
from oflow.shell.help import HelpOverlay, Row, Section
from oflow.shell.panel import Panel, PanelState
from oflow.shell.terminal_palette import TerminalPalette


def _rows_from_bindings(app: App[None], bindings: Iterable[object]) -> list[Row]:
    """One row per description; adjacent bindings that share one (e.g. up/down
    both "select issue") merge onto a single row with their keys joined, since
    they read as one action to the user rather than two.
    """
    rows: list[Row] = []
    for binding in bindings:
        if not isinstance(binding, Binding):
            continue
        key = app.get_key_display(binding)
        if rows and rows[-1][1] == binding.description:
            rows[-1] = (f"{rows[-1][0]} / {key}", binding.description)
        else:
            rows.append((key, binding.description))
    return rows


def _rows_from_actions(app: App[None], actions: Iterable[Action]) -> list[Row]:
    """One row per action, keyed exactly as the manifest declares it.

    Routed through get_key_display, same as bindings above, so a future
    action keyed by a named key (e.g. "enter") still renders consistently.
    """
    rows: list[Row] = []
    for action in actions:
        key = app.get_key_display(Binding(action.key, "", action.label))
        rows.append((key, action.label))
    return rows


class OflowApp(App[None]):
    CSS = """
    Screen { layout: vertical; }
    """

    # Built from SHELL_KEYS (see core.contract, the single source for the
    # shell's keymap) so this list and RESERVED_KEYS cannot drift apart.
    # priority=True is checked ahead of the focused widget, so a panel cannot
    # capture these by binding the same key.
    BINDINGS = [
        Binding(
            shell_key.key,
            shell_key.action,
            shell_key.description,
            show=shell_key.show,
            key_display=shell_key.key_display,
            priority=True,
        )
        for shell_key in SHELL_KEYS
    ]

    def __init__(self, tabs: tuple[TabConfig, ...], palette: TerminalPalette | None = None) -> None:
        super().__init__()
        # Adopts the terminal's own palette instead of imposing one: unlike
        # every other built-in theme, ansi-dark resolves through the terminal's
        # native ANSI colors. Named ANSI styles elsewhere depend on this being on.
        self.theme = "ansi-dark"
        self.tab_ids = tuple[str, ...](tab.integration for tab in tabs)
        self._client_ids = {tab.integration: tab.client_id for tab in tabs}
        self.empty_hint = "no tabs configured — run: oflow connect <integration>"
        self.seen = SeenState({})
        self._fetched_at: dict[str, datetime] = {}
        # Learned before this app existed (see cli._run) — None if the
        # terminal couldn't be queried in time. Feeds export_screenshot.
        self._palette = palette

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
            panel = Panel()
            panel.state = PanelState.ERROR
            panel.message = f"{integration_id} is not supported by this build"
            panel.integration_id = integration_id
            return panel
        panel = integration.panel_class()
        panel.integration_id = integration_id
        return panel

    def on_mount(self) -> None:
        """Load the seen-state once and hand it to every panel."""
        self.seen = SeenState.load()
        for panel in self.query(Panel):
            panel.seen = self.seen

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Fetch the newly active tab and focus its panel.

        TabbedContent posts this for the initial pane during mount as well as
        on every later switch, so this one handler is the sole fetch trigger.
        It doubles as the focus handoff so a panel's own arrow bindings work
        the moment its tab becomes visible, startup included.
        """
        panel = event.pane.query_one(Panel)
        self.refresh_tab(event.pane.id or "", panel)
        panel.focus()

    def on_app_focus(self) -> None:
        """The terminal regained focus; refresh whatever has gone stale.

        Fires only where the terminal reports focus. Where it does not, this
        degrades to tab-switch and manual refresh, which is enough.
        """
        if not self.active_tab:
            return
        panel = self._panel_of(self.active_tab)
        if panel is not None:
            self.refresh_tab(self.active_tab, panel)

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
        """Toggle the help overlay for the active tab."""
        if isinstance(self.screen, HelpOverlay):
            self.pop_screen()
            return
        self.push_screen(HelpOverlay(self._help_tab_section(), self.empty_hint))

    def _help_tab_section(self) -> Section | None:
        active = self.active_tab
        if active is None:
            return None
        panel = self._panel_of(active)
        binding_rows = _rows_from_bindings(self, type(panel).BINDINGS if panel is not None else ())
        try:
            action_rows = _rows_from_actions(self, get_integration(active).manifest.actions)
        except UnknownIntegration:
            # _panel_for already put this tab in its own error state; there is
            # no manifest to draw actions from.
            action_rows = []
        # A manifest action's label wins over a same-keyed panel binding.
        action_keys = {key for key, _ in action_rows}
        unshadowed_bindings = [row for row in binding_rows if row[0] not in action_keys]
        rows = unshadowed_bindings + action_rows
        return (active, rows)

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        """Yield Textual's system commands minus the ones this app doesn't offer."""
        dropped = {
            self.action_change_theme,
            self.action_hide_help_panel,
            self.action_show_help_panel,
            screen.action_maximize,
            screen.action_minimize,
        }
        for command in super().get_system_commands(screen):
            if command.callback not in dropped:
                yield command

    def _screenshot_theme(self) -> TerminalTheme | None:
        """None preserves today's fallback: Console.export_svg's own default."""
        if self._palette is None:
            return None
        return self._palette.to_terminal_theme()

    def export_screenshot(self, *, title: str | None = None, simplify: bool = False) -> str:
        """Render the current screen to SVG using the learned terminal palette.

        Near-verbatim copy of App.export_screenshot (Textual 8.2.8) plus a
        `theme=` argument — no hook exists to add just that. Recheck on upgrade.
        """
        assert self._driver is not None, "App must be running"
        width, height = self.size

        console = Console(
            width=width,
            height=height,
            file=io.StringIO(),
            force_terminal=True,
            color_system="truecolor",
            record=True,
            legacy_windows=False,
            safe_box=False,
        )
        screen_render = self.screen._compositor.render_update(
            full=True, screen_stack=self._background_screens, simplify=simplify
        )
        console.print(screen_render)
        return console.export_svg(title=title or self.title, theme=self._screenshot_theme())

    def action_refresh(self) -> None:
        if not self.active_tab:
            return
        panel = self._panel_of(self.active_tab)
        if panel is not None:
            self.refresh_tab(self.active_tab, panel, force=True)

    def on_panel_detail_requested(self, message: Panel.DetailRequested) -> None:
        # Only the focused panel of the visible tab can post this, so the
        # active tab names the integration that owns the item.
        if self.active_tab:
            self.fetch_detail(self.active_tab, message.panel, message.item)

    @work(thread=True)
    def fetch_detail(self, integration_id: str, panel: Panel, item: Item) -> None:
        """Fetch one item's detail off the UI thread; results and errors land
        in the panel's detail region and never touch the list's state."""
        key = Panel.detail_key(item)
        try:
            integration = get_integration(integration_id)
        except UnknownIntegration:
            return
        try:
            with httpx.Client(timeout=30) as http:
                credentials = fresh_credentials(
                    integration_id,
                    integration.manifest.provider,
                    self._client_ids.get(integration_id),
                    http,
                )
                if credentials is None:
                    self.call_from_thread(panel.show_detail_error, key, "not connected")
                    return
                detail = integration.fetch_detail(credentials, http, item)
        except CredentialStoreError as error:
            self.call_from_thread(panel.show_detail_error, key, str(error))
            return
        except AuthExpired as error:
            self.call_from_thread(
                panel.show_detail_error, key, f"{error} — run: oflow connect {integration_id}"
            )
            return
        except IntegrationError as error:
            self.call_from_thread(panel.show_detail_error, key, str(error))
            return
        self.call_from_thread(panel.show_detail, key, detail)

    @work(thread=True)
    def refresh_tab(self, integration_id: str, panel: Panel, force: bool = False) -> None:
        """Fetch integration_id's items off the UI thread and hand results to panel.

        panel is passed in rather than queried here since this body runs off
        the UI thread once @work(thread=True) dispatches it, and Textual
        widgets are not thread-safe to touch from anywhere else. Every
        mutation below crosses back via call_from_thread.
        """
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
            with httpx.Client(timeout=30) as http:
                credentials = fresh_credentials(
                    integration_id,
                    integration.manifest.provider,
                    self._client_ids.get(integration_id),
                    http,
                )
                if credentials is None:
                    self.call_from_thread(self._show_error, panel, "not connected")
                    return
                items = tuple[Item, ...](integration.fetch(credentials, http))
        except CredentialStoreError as error:
            self.call_from_thread(self._show_error, panel, str(error), keep_items=True)
            return
        except Malformed as error:
            # The tab itself is broken, not just momentarily unreachable — stale
            # data would promise a recovery that a shape mismatch cannot deliver.
            self.call_from_thread(self._show_error, panel, str(error))
            return
        except AuthExpired as error:
            self.call_from_thread(
                self._show_error, panel, f"{error} — run: oflow connect {integration_id}"
            )
            return
        except IntegrationError as error:
            self.call_from_thread(self._show_error, panel, str(error), keep_items=True)
            return

        self._fetched_at[integration_id] = now()
        self.call_from_thread(self._show_items, panel, items)

    def _show_items(self, panel: Panel, items: tuple[Item, ...]) -> None:
        panel.items = items
        panel.prune_detail_cache()
        panel.state = PanelState.EMPTY if not items else PanelState.READY
        panel.as_of = now()
        panel.refresh(layout=True)

    def _show_error(self, panel: Panel, message: str, keep_items: bool = False) -> None:
        panel.message = message
        # Last-good data is kept and marked stale rather than blanked: a tab that
        # empties on a network blip reads as "nothing to do", which is a lie.
        if keep_items and panel.items:
            panel.state = PanelState.STALE
        else:
            panel.state = PanelState.ERROR
        panel.refresh(layout=True)

    def _panel_of(self, integration_id: str) -> Panel | None:
        for pane in self.query(TabPane):
            if pane.id == integration_id:
                return pane.query_one(Panel)
        return None

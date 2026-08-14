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

from oflow.auth.store import CredentialStoreError, get_credentials, now
from oflow.contract import Action, AuthExpired, IntegrationError, Item, Malformed
from oflow.registry import UnknownIntegration, get_integration
from oflow.shell.help import HelpOverlay, Row, Section
from oflow.shell.panel import Panel, PanelState
from oflow.shell.terminal_palette import TerminalPalette
from oflow.state import SeenState


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
    # Routed through get_key_display, same as bindings above, so a future
    # action keyed by a named key (e.g. "enter") still renders consistently.
    rows: list[Row] = []
    for action in actions:
        key = app.get_key_display(Binding(action.key, "", action.label))
        rows.append((key, action.label))
    return rows


class OflowApp(App[None]):
    CSS = """
    Screen { layout: vertical; }
    """

    # priority=True is checked ahead of the focused widget, so a panel cannot
    # capture these by binding the same key. The footer groups entries by
    # action, not by key, so shift+left and shift+right — different actions —
    # would otherwise show as two separate entries; shift+right carries the
    # merged key_display for both directions and shift+left stays hidden
    # (show=False) so the footer shows a single "switch tab" entry.
    BINDINGS = [
        Binding("shift+left", "previous_tab", "switch tab", priority=True, show=False),
        Binding(
            "shift+right",
            "next_tab",
            "switch tab",
            priority=True,
            key_display="⇧ + ← / ⇧ + →",
        ),
        Binding("r", "refresh", "refresh", priority=True),
        Binding("question_mark", "help", "help", priority=True),
        Binding("q", "quit", "quit", priority=True),
    ]

    def __init__(self, tabs: tuple[str, ...], palette: TerminalPalette | None = None) -> None:
        super().__init__()
        # Adopts the terminal's own palette instead of imposing one: unlike
        # every other built-in theme, ansi-dark resolves through the terminal's
        # native ANSI colors. Named ANSI styles elsewhere depend on this being on.
        self.theme = "ansi-dark"
        self.tab_ids = tabs
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
        # "?" is itself a priority binding, so it is checked ahead of the modal
        # screen's own bindings even while the overlay is open — that is what
        # makes pressing it again a cheap toggle rather than a no-op. The
        # footer already shows the shell keys, so the overlay carries only the
        # active tab's integration section.
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
        # A manifest action's label wins over a same-keyed panel binding (e.g.
        # both declaring "o") since it is the user-facing name for that action.
        action_keys = {key for key, _ in action_rows}
        rows = [row for row in binding_rows if row[0] not in action_keys] + action_rows
        return (active, rows)

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        # Filtered by callback identity, not title, so a future Textual title
        # rename can't silently stop these from being dropped. "Keys" has two
        # possible callbacks depending on whether its panel is already open.
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
        # None preserves today's fallback: Console.export_svg's own default.
        return None if self._palette is None else self._palette.to_terminal_theme()

    def export_screenshot(self, *, title: str | None = None, simplify: bool = False) -> str:
        # Near-verbatim copy of App.export_screenshot (Textual 8.2.8) plus a
        # `theme=` argument — no hook exists to add just that. Recheck on upgrade.
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

    @work(thread=True)
    def refresh_tab(self, integration_id: str, panel: Panel, force: bool = False) -> None:
        # panel is resolved by the caller on the UI thread and passed in rather
        # than queried here: this method's body runs off the UI thread once
        # @work(thread=True) dispatches it, and Textual widgets are not
        # thread-safe to query or mutate from anywhere but the UI thread. Every
        # mutation below still crosses back via call_from_thread.
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

"""The dashboard shell: tabs, the global keymap, and nothing integration-specific."""

from __future__ import annotations

import io
from collections.abc import Callable, Iterable
from datetime import datetime

import httpx
from rich.console import Console
from rich.terminal_theme import TerminalTheme
from textual import work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.command import CommandPalette
from textual.screen import Screen
from textual.widgets import Footer, Static, TabbedContent, TabPane

from smorg.auth.refresh import fresh_credentials
from smorg.auth.store import CredentialStoreError, now
from smorg.core.config import TabConfig, resolve_connection
from smorg.core.contract import (
    Action,
    AuthExpired,
    IntegrationError,
    Item,
    Malformed,
)
from smorg.core.keys import SHELL_KEYS
from smorg.core.registry import UnknownIntegration, get_integration
from smorg.core.state import SeenState
from smorg.shell.help import HelpOverlay, Row, Section, merge_key_display, symbolize_key_display
from smorg.shell.menu import ManagementScreen, MenuCommands
from smorg.shell.panel import Panel, PanelState
from smorg.shell.refresh_indicator import RefreshIndicator, RefreshStage
from smorg.shell.terminal_palette import TerminalPalette, readable_theme


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
            rows[-1] = (merge_key_display(rows[-1][0], key), binding.description)
        else:
            rows.append((key, binding.description))
    return rows


def _rows_from_actions(app: App[None], actions: Iterable[Action]) -> list[Row]:
    """One row per action, keyed exactly as the manifest declares it.

    Routed through get_key_display, same as bindings above, so a future
    action keyed by a named key (e.g. "enter") still renders consistently.
    A manifest writes its label in whatever form reads naturally elsewhere
    (e.g. a command palette); only here, next to lowercase binding
    descriptions, is the leading letter lowered to match.
    """
    rows: list[Row] = []
    for action in actions:
        key = app.get_key_display(Binding(action.key, "", action.label))
        rows.append((key, _lowercase_leading_letter(action.label)))
    return rows


def _lowercase_leading_letter(text: str) -> str:
    """Turns "Open in Linear" into "open in Linear": only the first
    character moves, so a proper noun anywhere else in the label is never
    touched."""
    return text[:1].lower() + text[1:]


class SmorgApp(App[None]):
    CSS = """
    Screen { layout: vertical; layers: base refresh-indicator; }

    /* Textual pins Toast's :ansi background to literal ansi_black via
     * $ansi-background — a dark box on light terminals. ansi_default tracks the
     * terminal (same idiom as ManagementScreen); covers every notify() toast,
     * screenshot notification included. A full round border keeps the box
     * visible against terminal content now that the fill blends in. */
    Toast {
        &:ansi {
            background: ansi_default;
            color: ansi_default;
        }
    }

    Toast.-warning {
        border: round ansi_yellow;
    }

    Toast.-warning .toast--title {
        color: ansi_yellow;
    }

    Toast.-error {
        border: round ansi_red;
    }

    Toast.-error .toast--title {
        color: ansi_red;
    }

    /* Information toasts stay accent-free: no severity to flag, so the
     * border/title match the box instead of borrowing the built-in green.
     */
    Toast.-information {
        border: round ansi_default;
    }

    Toast.-information .toast--title {
        color: ansi_default;
    }
    """

    # Adds this app's management commands (add/remove integration) alongside
    # Textual's own system commands (screenshot, quit, ...); both surface in
    # the same ctrl+p menu.
    COMMANDS = App.COMMANDS | {MenuCommands}

    # Built from SHELL_KEYS (see core.keys, the single source for the
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
        self._tab_configs = {tab.integration: tab for tab in tabs}
        self.empty_hint = 'no tabs configured — press ^ + p and pick "Add integration"'
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

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Block every shell-level action while a management screen is on
        top (see shell.menu.ManagementScreen); HelpOverlay is a plain
        ModalScreen, so this check does not affect it.
        """
        if isinstance(self.screen, ManagementScreen):
            return False
        return super().check_action(action, parameters)

    def get_key_display(self, binding: Binding) -> str:
        """Routes every key display through symbolize_key_display, so the
        shell's modifiers render as symbols with an explicit "+" ("^p" -> "^ + p")."""
        default_display = super().get_key_display(binding)
        return symbolize_key_display(default_display)

    def compose(self) -> ComposeResult:
        """One layout, always: TabbedContent and the empty hint both exist,
        and only one is displayed at a time (see _sync_tab_visibility). This
        lets drop_tab toggle between them live without recomposing.
        """
        hint = Static(self.empty_hint, id="empty-hint")
        hint.display = not self.tab_ids
        with TabbedContent() as tabs:
            tabs.display = bool(self.tab_ids)
            for tab in self.tab_ids:
                with TabPane(tab, id=tab):
                    yield self._panel_for(tab)
        yield hint
        yield RefreshIndicator()
        yield Footer()

    def _sync_tab_visibility(self) -> None:
        """Show TabbedContent or the empty hint, never both — call after
        anything that changes tab_ids."""
        self.query_one(TabbedContent).display = bool(self.tab_ids)
        self.query_one("#empty-hint", Static).display = not self.tab_ids

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
        if event.pane.id:
            pane_id = event.pane.id
        else:
            pane_id = ""
        self.refresh_tab(pane_id, panel)
        panel.focus()

    def on_app_focus(self) -> None:
        """The terminal regained focus; refresh whatever has gone stale.

        Fires only where the terminal reports focus. Where it does not, this
        degrades to tab-switch and manual refresh, which is enough.
        """
        # Skip outright rather than queue behind a management screen — it
        # would otherwise fire the moment that screen closes.
        if isinstance(self.screen, ManagementScreen):
            return
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

    def action_command_palette(self) -> None:
        """Open the menu — this app's name for the command palette.

        Copy of App.action_command_palette (Textual 8.2.8) with a custom
        placeholder, which has no other hook. is_open's check is inlined
        since App[None] can't pass its App[object] parameter (invariant
        generic). Recheck on upgrade.
        """
        already_open = self.screen.has_class("--textual-command-palette")
        if self.use_command_palette and not already_open:
            self.push_screen(CommandPalette(placeholder="search the menu…", id="--command-palette"))

    def _help_tab_section(self) -> Section | None:
        active = self.active_tab
        if active is None:
            return None
        panel = self._panel_of(active)
        if panel is not None:
            bindings = type(panel).BINDINGS
        else:
            bindings = ()
        binding_rows = _rows_from_bindings(self, bindings)
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

    def _screenshot_theme(self) -> TerminalTheme:
        if self._palette is None:
            source = self.ansi_theme
        else:
            source = self._palette.to_terminal_theme()
        return readable_theme(source)

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
        if panel is None:
            return
        indicator = self.query_one(RefreshIndicator)
        indicator.show_stage(RefreshStage.CONNECTING)

        def report(stage: RefreshStage) -> None:
            # Runs on the worker thread; the indicator is UI-thread-only.
            self.call_from_thread(indicator.show_stage, stage)

        self.refresh_tab(self.active_tab, panel, force=True, on_stage=report)

    def action_mark_all_seen(self) -> None:
        """Clear the active tab's change marks in one stroke.

        Shell-level (not a panel binding) so every future integration gets
        it for free; a tab with nothing shown yet just marks nothing.
        """
        if not self.active_tab:
            return
        panel = self._panel_of(self.active_tab)
        if panel is not None:
            panel.mark_all_seen()

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
            path, client_id = resolve_connection(
                integration.manifest, self._tab_configs.get(integration_id)
            )
        except ValueError as error:
            self.call_from_thread(panel.show_detail_error, key, str(error))
            return
        try:
            with httpx.Client(timeout=30) as http:
                credentials = fresh_credentials(integration_id, path.provider, client_id, http)
                if credentials is None:
                    self.call_from_thread(panel.show_detail_error, key, "not connected")
                    return
                detail = integration.fetch_detail(credentials, http, item)
        except CredentialStoreError as error:
            self.call_from_thread(panel.show_detail_error, key, str(error))
            return
        except AuthExpired as error:
            self.call_from_thread(
                panel.show_detail_error, key, f"{error} — run: smorg connect {integration_id}"
            )
            return
        except IntegrationError as error:
            self.call_from_thread(panel.show_detail_error, key, str(error))
            return
        self.call_from_thread(panel.show_detail, key, detail)

    @work(thread=True)
    def refresh_tab(
        self,
        integration_id: str,
        panel: Panel,
        force: bool = False,
        on_stage: Callable[[RefreshStage], None] | None = None,
    ) -> None:
        """Fetch integration_id's items off the UI thread and hand results to panel.

        panel is passed in rather than queried here since the body runs off
        the UI thread once @work(thread=True) dispatches it, and Textual
        widgets are not thread-safe to touch from anywhere else. Every
        mutation crosses back via call_from_thread.

        on_stage, when given, receives the refresh's key stages (CONNECTING is
        shown by the caller before dispatch): _fetch_tab reports FETCHING, and
        this wrapper reports the terminal stage — DONE only when fresh items
        landed, FAILED otherwise — so the indicator can never hang mid-bar.
        Calls to on_stage happen on the worker thread; a callback that
        touches widgets must marshal through call_from_thread itself.
        """
        completed = False
        try:
            completed = self._fetch_tab(integration_id, panel, force, on_stage)
        finally:
            if on_stage is not None:
                if completed:
                    terminal_stage = RefreshStage.DONE
                else:
                    terminal_stage = RefreshStage.FAILED
                on_stage(terminal_stage)

    def _fetch_tab(
        self,
        integration_id: str,
        panel: Panel,
        force: bool,
        on_stage: Callable[[RefreshStage], None] | None,
    ) -> bool:
        """The fetch behind refresh_tab, on the worker thread: resolve the
        connection, fetch, and hand results or errors to panel. Returns
        whether fresh items landed."""
        try:
            integration = get_integration(integration_id)
        except UnknownIntegration:
            # _panel_for already put this tab in its own error state; there is
            # nothing this integration id could fetch.
            return False

        fetched_at = self._fetched_at.get(integration_id)
        if not force and fetched_at is not None:
            if now() - fetched_at < integration.manifest.stale_after:
                return False

        try:
            path, client_id = resolve_connection(
                integration.manifest, self._tab_configs.get(integration_id)
            )
        except ValueError as error:
            self.call_from_thread(self._show_error, panel, str(error))
            return False

        try:
            with httpx.Client(timeout=30) as http:
                credentials = fresh_credentials(integration_id, path.provider, client_id, http)
                if credentials is None:
                    self.call_from_thread(self._show_error, panel, "not connected")
                    return False
                # The bar's connecting→fetching boundary: credentials are
                # settled, the service call is next.
                if on_stage is not None:
                    on_stage(RefreshStage.FETCHING)
                items = tuple[Item, ...](integration.fetch(credentials, http))
        except CredentialStoreError as error:
            self.call_from_thread(self._show_error, panel, str(error), keep_items=True)
            return False
        except Malformed as error:
            # The tab itself is broken, not just momentarily unreachable — stale
            # data would promise a recovery that a shape mismatch cannot deliver.
            self.call_from_thread(self._show_error, panel, str(error))
            return False
        except AuthExpired as error:
            self.call_from_thread(
                self._show_error, panel, f"{error} — run: smorg connect {integration_id}"
            )
            return False
        except IntegrationError as error:
            self.call_from_thread(self._show_error, panel, str(error), keep_items=True)
            return False

        self._fetched_at[integration_id] = now()
        self.call_from_thread(self._show_items, panel, items)
        return True

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

    async def add_tab_live(self, tab_config: TabConfig) -> None:
        """Mount a freshly connected integration's tab and make it active.

        Works from the empty state too — compose() always yields TabbedContent,
        just hidden (see _sync_tab_visibility). Activating the new pane fires
        on_tabbed_content_tab_activated, so the fresh credentials get used on
        the very next fetch.
        """
        integration_id = tab_config.integration
        self.tab_ids = self.tab_ids + (integration_id,)
        self._tab_configs[integration_id] = tab_config
        panel = self._panel_for(integration_id)
        panel.seen = self.seen
        tabbed = self.query_one(TabbedContent)
        await tabbed.add_pane(TabPane(integration_id, panel, id=integration_id))
        self._sync_tab_visibility()
        tabbed.active = integration_id

    async def drop_tab(self, integration_id: str) -> None:
        """Remove integration_id's tab and drop it from tab_ids,
        _tab_configs, and _fetched_at. A no-op if the tab is already gone.

        query_one searches every mounted screen, not just the active one —
        load-bearing here, since a management modal covers the default
        screen while removal runs.
        """
        if integration_id not in self.tab_ids:
            return
        self.tab_ids = tuple(tab_id for tab_id in self.tab_ids if tab_id != integration_id)
        self._tab_configs.pop(integration_id, None)
        self._fetched_at.pop(integration_id, None)
        await self.query_one(TabbedContent).remove_pane(integration_id)
        self._sync_tab_visibility()

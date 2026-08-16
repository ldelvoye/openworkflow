from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from oflow.auth.store import Credentials
from oflow.core.config import TabConfig
from oflow.core.contract import SHELL_KEYS, AuthExpired, Item, Malformed, Unavailable
from oflow.integrations.linear.panel import LinearPanel
from oflow.integrations.linear.source import Issue
from oflow.shell.app import OflowApp
from oflow.shell.help import HelpOverlay
from oflow.shell.panel import Panel, PanelState
from oflow.shell.terminal_palette import TerminalPalette

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
CREDENTIALS = Credentials("token-abc", None, None, "read")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OFLOW_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("OFLOW_CREDENTIAL_STORE", "file")


def item(identifier: str = "ENG-1") -> Item:
    return Item(id=identifier, updated_at=NOW, url="https://example.invalid/1")


def issue(identifier: str = "ENG-1") -> Issue:
    return Issue(
        id=identifier,
        updated_at=NOW,
        url=f"https://linear.app/x/issue/{identifier}",
        title=f"title of {identifier}",
        status="In Review",
        status_type="started",
        team="Infra",
        priority="High",
    )


def test_an_empty_panel_says_so_rather_than_looking_broken():
    panel = Panel()
    panel.state = PanelState.EMPTY
    assert "nothing" in panel.body_text().lower()


def test_an_error_panel_shows_the_reason():
    panel = Panel()
    panel.state = PanelState.ERROR
    panel.message = "Linear is unreachable"
    assert "Linear is unreachable" in panel.body_text()


def test_a_stale_panel_marks_when_the_data_is_from():
    panel = Panel()
    panel.state = PanelState.STALE
    panel.as_of = NOW
    panel.items = (item(),)
    assert "12:00" in panel.body_text()


def test_empty_and_error_never_render_alike():
    empty, error = Panel(), Panel()
    empty.state = PanelState.EMPTY
    error.state = PanelState.ERROR
    error.message = "boom"
    assert empty.body_text() != error.body_text()


class _PanelHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield Panel()


@pytest.mark.asyncio
async def test_panel_message_disables_markup_so_server_text_cannot_style_the_panel():
    async with _PanelHarness().run_test() as pilot:
        panel = pilot.app.query_one(Panel)
        panel.state = PanelState.ERROR
        panel.message = "[red]boom[/red]"
        panel.refresh()
        await pilot.pause()
        body = panel.query_one("#body", Static)
        rendered = "".join(body.render_line(y).text for y in range(body.size.height))

    # A styled server string would come out as "boom" in red with the tags
    # consumed; markup off keeps the bracket text literal in what's drawn.
    assert "[red]boom[/red]" in rendered


@pytest.mark.asyncio
async def test_the_app_defaults_to_the_terminal_native_ansi_theme():
    """The dashboard must not impose its own palette over the terminal's.

    "ansi-dark" is Textual's built-in theme that resolves background,
    foreground, and chrome colors to the terminal's own ANSI palette instead of
    fixed truecolor hex values, and it is what makes native_ansi_color true —
    the flag that keeps named ANSI colors (e.g. CHANGE_STYLE) from being
    approximated to RGB.
    """
    app = OflowApp(tabs=(TabConfig("alpha"),))
    async with app.run_test():
        assert app.theme == "ansi-dark"
        assert app.native_ansi_color is True


@pytest.mark.asyncio
async def test_an_empty_app_renders_the_connect_hint():
    app = OflowApp(tabs=())
    async with app.run_test() as pilot:
        await pilot.pause()
        static = app.query_one(Static)
        rendered = "".join(static.render_line(y).text for y in range(static.size.height))

    assert "connect" in rendered.lower()


@pytest.mark.asyncio
async def test_q_quits():
    app = OflowApp(tabs=(TabConfig("alpha"),))
    async with app.run_test() as pilot:
        await pilot.press("q")
        await pilot.pause()
    assert not app.is_running


@pytest.mark.asyncio
async def test_shift_right_switches_to_the_next_tab():
    app = OflowApp(tabs=(TabConfig("alpha"), TabConfig("beta")))
    async with app.run_test() as pilot:
        assert app.active_tab == "alpha"
        await pilot.press("shift+right")
        assert app.active_tab == "beta"


def test_app_bindings_are_derived_from_shell_keys():
    """OflowApp.BINDINGS is built from SHELL_KEYS (see core.contract); this
    pins that derivation so the two cannot drift apart again.
    """
    keys = {binding.key for binding in OflowApp.BINDINGS if isinstance(binding, Binding)}
    assert keys == {shell_key.key for shell_key in SHELL_KEYS}


# --- Task 6: fetching and refresh ---


@pytest.mark.asyncio
async def test_only_the_visible_tab_fetches_on_startup(monkeypatch):
    fetched: list[str] = []
    monkeypatch.setattr(
        "oflow.shell.app.OflowApp.refresh_tab",
        lambda self, integration_id, panel, force=False: fetched.append(integration_id),
    )
    async with OflowApp(tabs=(TabConfig("alpha"), TabConfig("beta"))).run_test():
        pass
    assert fetched == ["alpha"]


@pytest.mark.asyncio
async def test_r_forces_a_refresh_of_the_active_tab(monkeypatch):
    fetched: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        "oflow.shell.app.OflowApp.refresh_tab",
        lambda self, integration_id, panel, force=False: fetched.append((integration_id, force)),
    )
    async with OflowApp(tabs=(TabConfig("alpha"),)).run_test() as pilot:
        fetched.clear()
        await pilot.press("r")
    assert fetched == [("alpha", True)]


@pytest.mark.asyncio
async def test_switching_to_a_tab_fetches_it(monkeypatch):
    fetched: list[str] = []
    monkeypatch.setattr(
        "oflow.shell.app.OflowApp.refresh_tab",
        lambda self, integration_id, panel, force=False: fetched.append(integration_id),
    )
    async with OflowApp(tabs=(TabConfig("alpha"), TabConfig("beta"))).run_test() as pilot:
        fetched.clear()
        await pilot.press("shift+right")
    assert fetched == ["beta"]


@pytest.mark.asyncio
async def test_the_app_never_schedules_a_timer(monkeypatch):
    """Zero background work is a design constraint, so it gets a test.

    Asserted by trapping the scheduling calls rather than inspecting Textual's
    internals, which would break on any refactor of theirs. refresh_tab runs for
    real here (unmonkeypatched) against two unregistered tabs, which is also the
    proof that an unsupported tab's worker returns quietly instead of crashing.
    """
    scheduled: list[str] = []
    monkeypatch.setattr(
        "textual.app.App.set_interval",
        lambda self, *args, **kwargs: scheduled.append("interval"),
    )
    monkeypatch.setattr(
        "textual.app.App.set_timer",
        lambda self, *args, **kwargs: scheduled.append("timer"),
    )

    async with OflowApp(tabs=(TabConfig("alpha"), TabConfig("beta"))).run_test() as pilot:
        await pilot.press("shift+right")
        await pilot.app.workers.wait_for_complete()

    assert scheduled == []


@pytest.mark.asyncio
async def test_refreshing_an_unsupported_tab_leaves_its_error_state_alone():
    app = OflowApp(tabs=(TabConfig("alpha"),))
    async with app.run_test() as pilot:
        await pilot.app.workers.wait_for_complete()
        await pilot.press("r")
        await pilot.app.workers.wait_for_complete()
        panel = app.query_one(Panel)

    assert panel.state is PanelState.ERROR
    assert "not supported" in panel.message


@pytest.mark.asyncio
async def test_app_regaining_focus_refreshes_the_active_tab(monkeypatch):
    fetched: list[str] = []
    monkeypatch.setattr(
        "oflow.shell.app.OflowApp.refresh_tab",
        lambda self, integration_id, panel, force=False: fetched.append(integration_id),
    )
    async with OflowApp(tabs=(TabConfig("alpha"),)).run_test() as pilot:
        fetched.clear()
        pilot.app.post_message(events.AppFocus())
        await pilot.pause()
    assert fetched == ["alpha"]


@pytest.mark.asyncio
async def test_switching_tabs_focuses_the_panel_so_arrow_keys_work(monkeypatch):
    """The end-to-end proof: no test-only panel.focus() call anywhere here."""
    issues = (issue("ENG-1"), issue("ENG-2"))

    def fake_refresh(self, integration_id, panel, force=False):
        panel.items = issues
        panel.state = PanelState.READY

    monkeypatch.setattr("oflow.shell.app.OflowApp.refresh_tab", fake_refresh)

    app = OflowApp(tabs=(TabConfig("alpha"), TabConfig("linear")))
    async with app.run_test() as pilot:
        await pilot.press("shift+right")
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        panel = app.query_one(LinearPanel)

    assert panel.selected_url() == issues[1].url


@pytest.mark.asyncio
async def test_j_and_k_do_nothing_in_the_shell_today(monkeypatch):
    """j and k are no longer reserved — an integration may bind them — but the
    shell itself still doesn't, so pressing them here is a no-op either way.
    """
    issues = (issue("ENG-1"), issue("ENG-2"))

    def fake_refresh(self, integration_id, panel, force=False):
        panel.items = issues
        panel.state = PanelState.READY

    monkeypatch.setattr("oflow.shell.app.OflowApp.refresh_tab", fake_refresh)

    app = OflowApp(tabs=(TabConfig("linear"), TabConfig("alpha")))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.active_tab == "linear"
        await pilot.press("j")
        await pilot.press("k")
        await pilot.pause()
        panel = app.query_one(LinearPanel)
        active_tab_after = app.active_tab

    assert active_tab_after == "linear"
    assert panel.selected_url() == issues[0].url


@pytest.mark.asyncio
async def test_the_app_injects_its_seen_state_into_every_panel_that_tracks_it():
    app = OflowApp(tabs=(TabConfig("linear"),))
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one(LinearPanel)
        assert panel.seen is app.seen


@pytest.mark.asyncio
async def test_opening_an_item_clears_its_change_mark(monkeypatch):
    """The "o" key is now LinearPanel's own binding (see action_open_selected),
    so this stays pilot-driven through the full app — the panel is focused as
    soon as its tab is active — but the patch target moves with the import.
    """
    issues = (issue("ENG-1"), issue("ENG-2"))
    opened: list[str] = []
    monkeypatch.setattr(
        "oflow.integrations.linear.panel.webbrowser.open", lambda url: opened.append(url)
    )

    def fake_refresh(self, integration_id, panel, force=False):
        panel.items = issues
        panel.state = PanelState.READY

    monkeypatch.setattr("oflow.shell.app.OflowApp.refresh_tab", fake_refresh)

    app = OflowApp(tabs=(TabConfig("linear"),))
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one(LinearPanel)
        assert panel.seen.is_changed("linear", issues[0]) is True

        await pilot.press("o")
        await pilot.pause()

    assert opened == [issues[0].url]
    assert panel.seen.is_changed("linear", issues[0]) is False


# --- Task 7: the error taxonomy drives distinct panel states ---


class _RaisingIntegration:
    """A fake integration whose fetch always fails with a given IntegrationError."""

    def __init__(self, error: Exception) -> None:
        self.manifest = SimpleNamespace(stale_after=timedelta(minutes=5), provider=None)
        self.panel_class = Panel
        self._error = error

    def fetch(self, credentials, http):
        raise self._error


def _stub_credentials(monkeypatch) -> None:
    monkeypatch.setattr(
        "oflow.shell.app.fresh_credentials",
        lambda integration_id, provider, client_id, http: CREDENTIALS,
    )


@pytest.mark.asyncio
async def test_malformed_is_always_error_even_when_items_exist(monkeypatch):
    _stub_credentials(monkeypatch)
    monkeypatch.setattr(
        "oflow.shell.app.get_integration",
        lambda integration_id: _RaisingIntegration(Malformed("issue shape changed")),
    )

    app = OflowApp(tabs=(TabConfig("linear"),))
    async with app.run_test() as pilot:
        await pilot.app.workers.wait_for_complete()
        panel = app.query_one(Panel)
        panel.items = (item(),)  # simulate previously-good data
        await pilot.press("r")
        await pilot.app.workers.wait_for_complete()

    assert panel.state is PanelState.ERROR
    assert "issue shape changed" in panel.message


@pytest.mark.asyncio
async def test_auth_expired_is_always_error_with_a_reconnect_hint(monkeypatch):
    _stub_credentials(monkeypatch)
    monkeypatch.setattr(
        "oflow.shell.app.get_integration",
        lambda integration_id: _RaisingIntegration(AuthExpired("token rejected")),
    )

    app = OflowApp(tabs=(TabConfig("linear"),))
    async with app.run_test() as pilot:
        await pilot.app.workers.wait_for_complete()
        panel = app.query_one(Panel)
        panel.items = (item(),)  # simulate previously-good data
        await pilot.press("r")
        await pilot.app.workers.wait_for_complete()

    assert panel.state is PanelState.ERROR
    assert "run: oflow connect linear" in panel.message


@pytest.mark.asyncio
async def test_unavailable_keeps_stale_items_but_errors_when_empty(monkeypatch):
    _stub_credentials(monkeypatch)
    monkeypatch.setattr(
        "oflow.shell.app.get_integration",
        lambda integration_id: _RaisingIntegration(Unavailable("linear is down")),
    )

    app = OflowApp(tabs=(TabConfig("linear"),))
    async with app.run_test() as pilot:
        await pilot.app.workers.wait_for_complete()
        panel = app.query_one(Panel)
        assert panel.state is PanelState.ERROR  # no prior items to fall back on

        panel.items = (item(),)
        await pilot.press("r")
        await pilot.app.workers.wait_for_complete()

    assert panel.state is PanelState.STALE


# --- The `?` help overlay ---


def _line_with(text: str, needle: str) -> str:
    return next(line for line in text.splitlines() if needle in line)


@pytest.mark.asyncio
async def test_question_mark_opens_the_active_tabs_deduped_key_reference():
    """The footer already shows the shell keys, so the overlay carries only
    the active tab's section: title = integration id, rows from the panel's
    own BINDINGS plus the manifest's actions, deduped by key.
    """
    app = OflowApp(tabs=(TabConfig("linear"),))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()

        assert isinstance(app.screen, HelpOverlay)
        text = app.screen.body_text()

    assert "linear" in text
    # The manifest-declared action (Action(id="open", label="Open in Linear",
    # key="o", ...) in linear/manifest.py), not a hardcoded string.
    assert _line_with(text, "Open in Linear").strip().startswith("o")
    # LinearPanel also binds "o" (action_open_selected, label "open in
    # browser") — the manifest's label wins, so the panel's own label for
    # that key never shows up as a second row.
    assert "open in browser" not in text
    # The panel's own up/down BINDINGS, merged onto one row (see LinearPanel.BINDINGS).
    assert _line_with(text, "select issue").strip().startswith("↑ / ↓")


@pytest.mark.asyncio
async def test_help_overlay_content_is_actually_rendered_at_a_real_size():
    """Regression guard for the overlay rendering as a tiny empty box.

    body_text() alone is not proof of anything visible: it returned this exact
    string even when the content widget's composed region was 0x0 (Static has
    no width of its own inside an auto-width parent — see help.py's
    DEFAULT_CSS). This measures the actual composed widget and its rendered
    lines instead, with a floor tied to the real content rather than an
    arbitrary constant.
    """
    app = OflowApp(tabs=(TabConfig("linear"),))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()

        overlay = app.screen
        assert isinstance(overlay, HelpOverlay)
        content = overlay.query_one(Static)
        body_lines = overlay.body_text().splitlines()

        assert content.size.height >= len(body_lines)
        assert content.size.width >= max(len(line) for line in body_lines)

        rendered = [content.render_line(y).text for y in range(content.size.height)]

    title_line = next(line for line in rendered if line.strip() == "linear")
    assert title_line.strip() == "linear"
    select_issue_line = next(line for line in rendered if "select issue" in line)
    assert select_issue_line.strip().startswith("↑ / ↓")


@pytest.mark.asyncio
async def test_escape_closes_the_help_overlay():
    app = OflowApp(tabs=(TabConfig("linear"),))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()
        assert isinstance(app.screen, HelpOverlay)

        await pilot.press("escape")
        await pilot.pause()

        assert not isinstance(app.screen, HelpOverlay)


@pytest.mark.asyncio
async def test_question_mark_again_also_closes_the_overlay():
    app = OflowApp(tabs=(TabConfig("linear"),))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()
        assert isinstance(app.screen, HelpOverlay)

        await pilot.press("?")
        await pilot.pause()

        assert not isinstance(app.screen, HelpOverlay)


@pytest.mark.asyncio
async def test_shell_keys_still_work_after_the_overlay_closes(monkeypatch):
    fetched: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        "oflow.shell.app.OflowApp.refresh_tab",
        lambda self, integration_id, panel, force=False: fetched.append((integration_id, force)),
    )
    app = OflowApp(tabs=(TabConfig("alpha"), TabConfig("linear")))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        fetched.clear()
        await pilot.press("shift+right")
        await pilot.press("r")
        await pilot.pause()

    assert fetched == [("linear", False), ("linear", True)]


@pytest.mark.asyncio
async def test_no_tabs_help_overlay_shows_the_connect_hint():
    app = OflowApp(tabs=())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()

        assert isinstance(app.screen, HelpOverlay)
        text = app.screen.body_text()

    # With no integration to reference, the overlay falls back to the same
    # connect hint the app's own empty state shows — a single line.
    assert text == app.empty_hint


@pytest.mark.asyncio
async def test_question_mark_on_a_tab_with_no_registered_integration_does_not_crash():
    # "alpha" has no integration (see _panel_for's UnknownIntegration handling
    # elsewhere in this file); _help_tab_section's own except UnknownIntegration
    # branch must produce an empty tab section rather than raising.
    app = OflowApp(tabs=(TabConfig("alpha"),))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()

        assert isinstance(app.screen, HelpOverlay)
        text = app.screen.body_text()

    assert "alpha" in text


# --- Trimmed system commands (Change 2) ---


@pytest.mark.asyncio
async def test_system_commands_drop_maximize_and_theme_but_keep_the_rest():
    app = OflowApp(tabs=(TabConfig("linear"),))
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        commands = list(app.get_system_commands(screen))

    # Pins the actual mechanism (callback identity — see get_system_commands)
    # rather than title strings, so a future Textual rename of these titles
    # cannot silently stop them from being dropped without this test noticing.
    dropped_callbacks = {
        app.action_change_theme,
        app.action_hide_help_panel,
        app.action_show_help_panel,
        screen.action_maximize,
        screen.action_minimize,
    }
    assert {command.callback for command in commands}.isdisjoint(dropped_callbacks)

    titles = {command.title for command in commands}
    assert titles.isdisjoint({"Theme", "Keys", "Maximize", "Minimize"})
    # The command palette itself, copy-to-clipboard, and screenshot all stay;
    # Quit and Screenshot are the two that surface as system commands.
    assert {"Quit", "Screenshot"} <= titles


# --- Screenshot export uses the learned terminal palette ---


PALETTE = TerminalPalette(
    background=(10, 20, 30),
    foreground=(200, 210, 220),
    ansi=tuple((index, index, index) for index in range(16)),
)


@pytest.mark.asyncio
async def test_screenshot_with_a_learned_palette_uses_its_real_colors():
    app = OflowApp(tabs=(TabConfig("linear"),), palette=PALETTE)
    async with app.run_test() as pilot:
        await pilot.pause()
        svg = app.export_screenshot()

    assert "#0a141e" in svg  # PALETTE.background, hex
    assert "#292929" not in svg  # Rich's generic SVG_EXPORT_THEME fallback background


@pytest.mark.asyncio
async def test_screenshot_without_a_palette_keeps_the_current_fallback_mapping():
    app = OflowApp(tabs=(TabConfig("linear"),))  # no palette — the query found nothing
    async with app.run_test() as pilot:
        await pilot.pause()
        svg = app.export_screenshot()

    # Unchanged from Textual's own App.export_screenshot: Rich's generic
    # SVG_EXPORT_THEME background, since no theme is passed to export_svg.
    assert "#292929" in svg


# --- Task 4: wiring token refresh into the shell ---


@pytest.mark.asyncio
async def test_the_tab_client_id_reaches_the_refresh_layer(monkeypatch):
    seen: list[tuple[str, str | None]] = []

    def fake_fresh(integration_id, provider, client_id, http):
        seen.append((integration_id, client_id))
        return None

    monkeypatch.setattr("oflow.shell.app.fresh_credentials", fake_fresh)
    app = OflowApp(tabs=(TabConfig("linear", client_id="client-42"),))
    async with app.run_test() as pilot:
        await pilot.app.workers.wait_for_complete()

    assert seen == [("linear", "client-42")]


@pytest.mark.asyncio
async def test_a_failed_refresh_shows_the_reconnect_hint(monkeypatch):
    def fake_fresh(integration_id, provider, client_id, http):
        raise AuthExpired("token refresh failed (invalid_grant)")

    monkeypatch.setattr("oflow.shell.app.fresh_credentials", fake_fresh)
    app = OflowApp(tabs=(TabConfig("linear"),))
    async with app.run_test() as pilot:
        await pilot.app.workers.wait_for_complete()
        panel = app.query_one(Panel)

    assert panel.state is PanelState.ERROR
    assert "run: oflow connect linear" in panel.message


# --- Task 9: the shell brokers detail fetches ---


class _DetailIntegration:
    def __init__(self) -> None:
        self.manifest = SimpleNamespace(stale_after=timedelta(minutes=5), provider=None)
        self.panel_class = LinearPanel

    def fetch(self, credentials, http):
        return ()

    def fetch_detail(self, credentials, http, item):
        return f"detail of {item.id}"


@pytest.mark.asyncio
async def test_a_detail_request_round_trips_through_the_worker(monkeypatch):
    _stub_credentials(monkeypatch)
    monkeypatch.setattr(
        "oflow.shell.app.get_integration", lambda integration_id: _DetailIntegration()
    )
    app = OflowApp(tabs=(TabConfig("linear"),))
    async with app.run_test() as pilot:
        await pilot.app.workers.wait_for_complete()
        panel = app.query_one(LinearPanel)
        panel.items = (issue("ENG-1"),)
        panel.state = PanelState.READY
        await pilot.pause()
        await pilot.press("enter")
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()

    key = panel.detail_key(issue("ENG-1"))
    assert panel._details[key] == "detail of ENG-1"


@pytest.mark.asyncio
async def test_a_failed_detail_fetch_lands_in_the_region_not_the_list(monkeypatch):
    _stub_credentials(monkeypatch)

    class _FailingDetail(_DetailIntegration):
        def fetch_detail(self, credentials, http, item):
            raise Unavailable("linear is down")

    monkeypatch.setattr("oflow.shell.app.get_integration", lambda integration_id: _FailingDetail())
    app = OflowApp(tabs=(TabConfig("linear"),))
    async with app.run_test() as pilot:
        await pilot.app.workers.wait_for_complete()
        panel = app.query_one(LinearPanel)
        panel.items = (issue("ENG-1"),)
        panel.state = PanelState.READY
        await pilot.pause()
        await pilot.press("enter")
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()

    assert panel._detail_errors[panel.detail_key(issue("ENG-1"))] == "linear is down"
    assert panel.state is PanelState.READY  # the list never notices

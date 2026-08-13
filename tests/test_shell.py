from datetime import UTC, datetime

import pytest
from textual import events
from textual.app import App, ComposeResult

from oflow.config import ConfigError
from oflow.contract import Item
from oflow.integrations.linear.panel import LinearPanel
from oflow.integrations.linear.source import Issue
from oflow.shell.app import OflowApp
from oflow.shell.panel import Panel, PanelState

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


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


def test_panel_states_are_the_four_the_design_names():
    assert {member.value for member in PanelState} >= {
        "loading",
        "empty",
        "error",
        "stale",
    }


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
        rendered = "".join(panel.render_line(y).text for y in range(panel.size.height))

    # A styled server string would come out as "boom" in red with the tags
    # consumed; markup off keeps the bracket text literal in what's drawn.
    assert "[red]boom[/red]" in rendered


@pytest.mark.asyncio
async def test_the_app_opens_with_a_tab_per_configured_integration():
    # Pilot.app is typed as App[ReturnType], not the subclass, so a locally
    # typed reference is what gives pyright OflowApp's own attributes.
    app = OflowApp(tabs=("alpha", "beta"))
    async with app.run_test():
        assert app.tab_ids == ("alpha", "beta")


@pytest.mark.asyncio
async def test_no_tabs_shows_the_connect_hint():
    app = OflowApp(tabs=())
    async with app.run_test():
        assert "connect" in app.empty_hint.lower()


@pytest.mark.asyncio
async def test_q_quits():
    app = OflowApp(tabs=("alpha",))
    async with app.run_test() as pilot:
        await pilot.press("q")
        await pilot.pause()
    assert not app.is_running


@pytest.mark.asyncio
async def test_tab_switches_between_tabs():
    app = OflowApp(tabs=("alpha", "beta"))
    async with app.run_test() as pilot:
        assert app.active_tab == "alpha"
        await pilot.press("tab")
        assert app.active_tab == "beta"


# --- Task 6: fetching and refresh ---


@pytest.mark.asyncio
async def test_only_the_visible_tab_fetches_on_startup(monkeypatch):
    fetched: list[str] = []
    monkeypatch.setattr(
        "oflow.shell.app.OflowApp.refresh_tab",
        lambda self, integration_id, force=False: fetched.append(integration_id),
    )
    async with OflowApp(tabs=("alpha", "beta")).run_test():
        pass
    assert fetched == ["alpha"]


@pytest.mark.asyncio
async def test_r_forces_a_refresh_of_the_active_tab(monkeypatch):
    fetched: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        "oflow.shell.app.OflowApp.refresh_tab",
        lambda self, integration_id, force=False: fetched.append((integration_id, force)),
    )
    async with OflowApp(tabs=("alpha",)).run_test() as pilot:
        fetched.clear()
        await pilot.press("r")
    assert fetched == [("alpha", True)]


@pytest.mark.asyncio
async def test_switching_to_a_tab_fetches_it(monkeypatch):
    fetched: list[str] = []
    monkeypatch.setattr(
        "oflow.shell.app.OflowApp.refresh_tab",
        lambda self, integration_id, force=False: fetched.append(integration_id),
    )
    async with OflowApp(tabs=("alpha", "beta")).run_test() as pilot:
        fetched.clear()
        await pilot.press("tab")
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

    async with OflowApp(tabs=("alpha", "beta")).run_test() as pilot:
        await pilot.press("tab")
        await pilot.app.workers.wait_for_complete()

    assert scheduled == []


@pytest.mark.asyncio
async def test_refreshing_an_unsupported_tab_leaves_its_error_state_alone():
    app = OflowApp(tabs=("alpha",))
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
        lambda self, integration_id, force=False: fetched.append(integration_id),
    )
    async with OflowApp(tabs=("alpha",)).run_test() as pilot:
        fetched.clear()
        pilot.app.post_message(events.AppFocus())
        await pilot.pause()
    assert fetched == ["alpha"]


@pytest.mark.asyncio
async def test_switching_tabs_focuses_the_panel_so_arrow_keys_work(monkeypatch):
    """The end-to-end proof: no test-only panel.focus() call anywhere here."""
    issues = (issue("ENG-1"), issue("ENG-2"))

    def fake_refresh(self, integration_id, force=False):
        panel = self._panel_of(integration_id)
        if panel is not None:
            panel.items = issues
            panel.state = PanelState.READY

    monkeypatch.setattr("oflow.shell.app.OflowApp.refresh_tab", fake_refresh)

    app = OflowApp(tabs=("alpha", "linear"))
    async with app.run_test() as pilot:
        await pilot.press("tab")
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        panel = app.query_one(LinearPanel)

    assert panel.selected_url() == issues[1].url


@pytest.mark.asyncio
async def test_j_and_k_are_reserved_but_unbound(monkeypatch):
    issues = (issue("ENG-1"), issue("ENG-2"))

    def fake_refresh(self, integration_id, force=False):
        panel = self._panel_of(integration_id)
        if panel is not None:
            panel.items = issues
            panel.state = PanelState.READY

    monkeypatch.setattr("oflow.shell.app.OflowApp.refresh_tab", fake_refresh)

    app = OflowApp(tabs=("linear",))
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
    app = OflowApp(tabs=("linear",))
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one(LinearPanel)
        assert panel.seen is app.seen


@pytest.mark.asyncio
async def test_opening_an_item_clears_its_change_mark(monkeypatch):
    issues = (issue("ENG-1"), issue("ENG-2"))
    opened: list[str] = []
    monkeypatch.setattr("oflow.shell.app.webbrowser.open", lambda url: opened.append(url))

    def fake_refresh(self, integration_id, force=False):
        panel = self._panel_of(integration_id)
        if panel is not None:
            panel.items = issues
            panel.state = PanelState.READY

    monkeypatch.setattr("oflow.shell.app.OflowApp.refresh_tab", fake_refresh)

    app = OflowApp(tabs=("linear",))
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one(LinearPanel)
        assert panel.seen.is_changed("linear", issues[0]) is True

        await pilot.press("o")
        await pilot.pause()

    assert opened == [issues[0].url]
    assert panel.seen.is_changed("linear", issues[0]) is False


@pytest.mark.asyncio
async def test_a_failed_seen_save_does_not_crash_the_app_and_notifies_instead(monkeypatch):
    issues = (issue("ENG-1"),)
    monkeypatch.setattr("oflow.shell.app.webbrowser.open", lambda url: None)

    def fake_refresh(self, integration_id, force=False):
        panel = self._panel_of(integration_id)
        if panel is not None:
            panel.items = issues
            panel.state = PanelState.READY

    monkeypatch.setattr("oflow.shell.app.OflowApp.refresh_tab", fake_refresh)

    def refuse_save(self):
        raise ConfigError("disk is full")

    monkeypatch.setattr("oflow.state.SeenState.save", refuse_save)

    notified: list[str] = []
    monkeypatch.setattr(
        "oflow.shell.app.OflowApp.notify",
        lambda self, message, **kwargs: notified.append(message),
    )

    app = OflowApp(tabs=("linear",))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        panel = app.query_one(LinearPanel)

    assert notified == ["disk is full"]
    # The in-memory mark clears even though persisting it to disk failed.
    assert panel.seen.is_changed("linear", issues[0]) is False

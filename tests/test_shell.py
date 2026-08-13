from datetime import UTC, datetime

import pytest
from textual.app import App, ComposeResult

from oflow.contract import Item
from oflow.shell.app import OflowApp
from oflow.shell.panel import Panel, PanelState

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def item(identifier: str = "ENG-1") -> Item:
    return Item(id=identifier, updated_at=NOW, url="https://example.invalid/1")


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

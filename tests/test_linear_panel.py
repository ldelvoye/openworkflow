from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.app import App, ComposeResult

from oflow.integrations.linear.panel import LinearPanel
from oflow.integrations.linear.source import Issue
from oflow.shell.panel import PanelState
from oflow.state import SeenState

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def issue(identifier: str = "ENG-1", status: str = "In Review") -> Issue:
    return Issue(
        id=identifier,
        updated_at=NOW,
        url=f"https://linear.app/x/issue/{identifier}",
        title=f"title of {identifier}",
        status=status,
        status_type="started",
        team="Infra",
        priority="High",
    )


def panel_with(*issues: Issue, seen: SeenState | None = None) -> LinearPanel:
    panel = LinearPanel()
    panel.state = PanelState.READY
    panel.items = issues
    panel.seen = seen or SeenState({})
    panel.integration_id = "linear"
    return panel


def test_issues_are_grouped_by_status():
    text = panel_with(issue("ENG-1", "In Review"), issue("ENG-2", "Todo")).body_text()
    assert "In Review" in text
    assert "Todo" in text


def test_the_identifier_and_title_both_appear():
    text = panel_with(issue("ENG-1")).body_text()
    assert "ENG-1" in text
    assert "title of ENG-1" in text


def test_a_changed_issue_is_marked_and_a_seen_one_is_not():
    seen = SeenState({})
    unchanged = issue("ENG-2")
    seen.mark_seen("linear", unchanged)

    text = panel_with(issue("ENG-1"), unchanged, seen=seen).body_text()
    marked = [line for line in text.splitlines() if "●" in line]

    assert any("ENG-1" in line for line in marked)
    assert not any("ENG-2" in line for line in marked)


def test_the_open_action_returns_the_url_of_the_selected_issue():
    panel = panel_with(issue("ENG-1"), issue("ENG-2"))
    panel.cursor = 1
    assert panel.selected_url() == "https://linear.app/x/issue/ENG-2"


def test_the_panel_never_fetches():
    """The seam the whole design rests on, enforced rather than trusted."""
    source = (Path("src") / "oflow" / "integrations" / "linear" / "panel.py").read_text()
    assert "httpx" not in source
    assert "McpClient" not in source
    assert "fetch" not in source


# --- Owner-decision extensions: priority glyph, selection cursor, safe styling ---


@pytest.mark.parametrize(
    ("priority", "glyph"),
    [
        ("Urgent", "!!!"),
        ("High", "!!"),
        ("Medium", "!"),
        ("Low", "·"),
        ("", "·"),
    ],
)
def test_priority_glyph_scale(priority: str, glyph: str) -> None:
    panel = panel_with(replace(issue("ENG-1"), priority=priority))
    text = panel.body_text()
    assert glyph.ljust(3) in text


def test_priority_glyphs_are_padded_to_a_common_width_so_titles_align():
    urgent_text = panel_with(replace(issue("ENG-1"), priority="Urgent")).body_text()
    low_text = panel_with(replace(issue("ENG-2"), priority="Low")).body_text()
    urgent_line = next(line for line in urgent_text.splitlines() if "ENG-1" in line)
    low_line = next(line for line in low_text.splitlines() if "ENG-2" in line)

    # "!!!" and "·" differ in width; the title must still start at the same
    # column in both rows.
    assert urgent_line.index("title of ENG-1") == low_line.index("title of ENG-2")


def test_no_issue_is_selected_when_the_panel_is_empty():
    panel = panel_with()
    assert panel.selected_url() is None


def test_cursor_starts_at_the_first_issue():
    panel = panel_with(issue("ENG-1"), issue("ENG-2"))
    assert panel.selected_url() == "https://linear.app/x/issue/ENG-1"


def test_pressing_down_moves_the_selection_to_the_next_issue():
    panel = panel_with(issue("ENG-1"), issue("ENG-2"), issue("ENG-3"))
    panel.action_cursor_down()
    assert panel.selected_url() == "https://linear.app/x/issue/ENG-2"


def test_pressing_down_wraps_from_the_last_issue_to_the_first():
    panel = panel_with(issue("ENG-1"), issue("ENG-2"))
    panel.cursor = 1
    panel.action_cursor_down()
    assert panel.selected_url() == "https://linear.app/x/issue/ENG-1"


def test_pressing_up_wraps_from_the_first_issue_to_the_last():
    panel = panel_with(issue("ENG-1"), issue("ENG-2"))
    panel.action_cursor_up()
    assert panel.selected_url() == "https://linear.app/x/issue/ENG-2"


def test_the_cursor_clamps_when_items_shrink():
    panel = panel_with(issue("ENG-1"), issue("ENG-2"), issue("ENG-3"))
    panel.cursor = 2
    panel.items = (issue("ENG-1"),)
    assert panel.selected_url() == "https://linear.app/x/issue/ENG-1"


def test_the_selected_row_carries_the_selection_marker():
    panel = panel_with(issue("ENG-1"), issue("ENG-2"))
    panel.cursor = 1
    text = panel.body_text()
    marked = [line for line in text.splitlines() if "▸" in line]
    assert any("ENG-2" in line for line in marked)
    assert not any("ENG-1" in line for line in marked)


class _LinearPanelHarness(App[None]):
    def __init__(self, panel: LinearPanel) -> None:
        super().__init__()
        self._panel = panel

    def compose(self) -> ComposeResult:
        yield self._panel


@pytest.mark.asyncio
async def test_pressing_the_down_key_moves_the_selection_through_the_real_binding():
    panel = panel_with(issue("ENG-1"), issue("ENG-2"))
    async with _LinearPanelHarness(panel).run_test() as pilot:
        panel.focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()

    assert panel.selected_url() == "https://linear.app/x/issue/ENG-2"


@pytest.mark.asyncio
async def test_a_hostile_title_is_never_interpreted_as_markup_in_the_real_render():
    hostile = replace(issue("ENG-1"), title="[red]x[/red]")
    panel = panel_with(hostile)
    async with _LinearPanelHarness(panel).run_test() as pilot:
        panel.refresh()
        await pilot.pause()
        rendered = "".join(panel.render_line(y).text for y in range(panel.size.height))

    # Styled output is built as rich.text.Text with literal appends, so a title
    # that looks like markup must come out unparsed rather than styled/consumed.
    assert "[red]x[/red]" in rendered


@pytest.mark.asyncio
async def test_a_hostile_status_is_never_interpreted_as_markup_in_the_real_render():
    hostile = replace(issue("ENG-1", status="[blue]Weird[/blue]"))
    panel = panel_with(hostile)
    async with _LinearPanelHarness(panel).run_test() as pilot:
        panel.refresh()
        await pilot.pause()
        rendered = "".join(panel.render_line(y).text for y in range(panel.size.height))

    assert "[blue]Weird[/blue]" in rendered

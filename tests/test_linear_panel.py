import io
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.console import Console
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from smorg.core.state import SeenState
from smorg.integrations.linear.panel import LinearPanel
from smorg.integrations.linear.source import Comment, Issue, IssueDetail
from smorg.shell.markdown import is_local_path
from smorg.shell.panel import PanelState

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
    source = (Path("src") / "smorg" / "integrations" / "linear" / "panel.py").read_text()
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


def test_ids_are_padded_to_a_common_width_so_titles_align():
    text = panel_with(issue("CTRL-3", "In Progress"), issue("INFRENG-415", "Todo")).body_text()
    starts = {line.index("title of") for line in text.splitlines() if "title of" in line}
    assert len(starts) == 1


def _style_at(rendered: Text, substring: str) -> str | Style | None:
    """The style of the span covering `substring`'s position in `rendered`, if any."""
    start = rendered.plain.index(substring)
    end = start + len(substring)
    for span in rendered.spans:
        if span.start < end and span.end > start:
            return span.style
    return None


def test_the_in_progress_header_is_bold_yellow():
    rendered = panel_with(issue("ENG-1", "In Progress")).render_ready()
    assert _style_at(rendered, "In Progress") == "bold yellow"


def test_an_unknown_started_status_falls_back_to_bold_yellow():
    rendered = panel_with(issue("ENG-1", "Doing The Work")).render_ready()
    assert _style_at(rendered, "Doing The Work") == "bold yellow"


def test_an_unknown_unstarted_status_falls_back_to_bold():
    unstarted = replace(issue("ENG-1", "Someday"), status_type="unstarted")
    rendered = panel_with(unstarted).render_ready()
    assert _style_at(rendered, "Someday") == "bold"


@pytest.mark.parametrize(
    ("status", "style"),
    [
        ("Blocked", "bold red"),
        ("In Review", "bold green"),
    ],
)
def test_known_status_labels_get_their_mapped_style(status: str, style: str) -> None:
    rendered = panel_with(issue("ENG-1", status)).render_ready()
    assert _style_at(rendered, status) == style


def test_status_style_lookup_is_case_insensitive():
    rendered = panel_with(issue("ENG-1", "IN PROGRESS")).render_ready()
    assert _style_at(rendered, "IN PROGRESS") == "bold yellow"


def test_the_urgent_glyph_is_bold_red():
    # The tested priority sits on the second (unselected) issue: the cursor's
    # own row carries a "bold" base style of its own, which would otherwise
    # mask the glyph's specific color in _style_at's first-match lookup.
    rendered = panel_with(
        replace(issue("ENG-1"), priority="Low"), replace(issue("ENG-2"), priority="Urgent")
    ).render_ready()
    assert _style_at(rendered, "!!!") == "bold red"


def test_the_high_glyph_is_yellow():
    rendered = panel_with(
        replace(issue("ENG-1"), priority="Low"), replace(issue("ENG-2"), priority="High")
    ).render_ready()
    assert _style_at(rendered, "!!") == "yellow"


def test_the_medium_glyph_carries_no_style():
    rendered = panel_with(
        replace(issue("ENG-1"), priority="Low"), replace(issue("ENG-2"), priority="Medium")
    ).render_ready()
    assert _style_at(rendered, "!") is None


def test_the_fallback_glyph_is_dim():
    rendered = panel_with(
        replace(issue("ENG-1"), priority="Medium"), replace(issue("ENG-2"), priority="Low")
    ).render_ready()
    assert _style_at(rendered, "·") == "dim"


# --- Stable status-group ordering ---


def test_status_groups_render_in_a_fixed_actionability_order_regardless_of_input_order():
    text = (
        panel_with(
            issue("ENG-1", "Blocked"),
            issue("ENG-2", "Todo"),
            issue("ENG-3", "In Review"),
            issue("ENG-4", "In Progress"),
        )
        .render_ready()
        .plain
    )
    positions = [text.index(status) for status in ("In Progress", "In Review", "Todo", "Blocked")]
    assert positions == sorted(positions)


def test_an_unknown_started_status_group_sorts_between_in_review_and_todo():
    unknown = replace(issue("ENG-5", "Doing The Work"), status_type="started")
    text = (
        panel_with(issue("ENG-1", "Todo"), unknown, issue("ENG-2", "In Review"))
        .render_ready()
        .plain
    )
    assert text.index("In Review") < text.index("Doing The Work") < text.index("Todo")


def test_an_unknown_unstarted_status_group_sorts_between_todo_and_blocked():
    unknown = replace(issue("ENG-5", "Someday"), status_type="unstarted")
    text = (
        panel_with(issue("ENG-1", "Blocked"), unknown, issue("ENG-2", "Todo")).render_ready().plain
    )
    assert text.index("Todo") < text.index("Someday") < text.index("Blocked")


def test_two_unknown_same_type_status_groups_sort_alphabetically():
    zebra = replace(issue("ENG-5", "Zebra Work"), status_type="started")
    alpha = replace(issue("ENG-6", "Alpha Work"), status_type="started")
    text = panel_with(zebra, alpha).render_ready().plain
    assert text.index("Alpha Work") < text.index("Zebra Work")


def test_rows_truncate_instead_of_wrapping():
    rendered = panel_with(issue("ENG-1")).render_ready()
    assert rendered.no_wrap is True
    assert rendered.overflow == "ellipsis"


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
async def test_pressing_o_opens_the_selected_issue_and_clears_its_change_mark(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(
        "smorg.integrations.linear.panel.webbrowser.open", lambda url: opened.append(url)
    )
    monkeypatch.setattr("smorg.core.state.SeenState.save", lambda self: None)

    panel = panel_with(issue("ENG-1"), issue("ENG-2"))
    async with _LinearPanelHarness(panel).run_test() as pilot:
        panel.focus()
        await pilot.pause()
        assert panel.seen.is_changed("linear", issue("ENG-1")) is True

        await pilot.press("o")
        await pilot.pause()

    assert opened == ["https://linear.app/x/issue/ENG-1"]
    assert panel.seen.is_changed("linear", issue("ENG-1")) is False


def test_a_failed_seen_save_does_not_crash_and_notifies_instead(monkeypatch):
    """write_private_file/ensure_config_dir raise OSError directly on a real disk
    failure (full disk, revoked permissions, read-only filesystem) — this is not
    wrapped in a ConfigError, so the guard around seen.save() has to catch the
    unwrapped type to actually survive one.
    """
    monkeypatch.setattr("smorg.integrations.linear.panel.webbrowser.open", lambda url: None)

    def refuse_save(self):
        raise OSError("No space left on device")

    monkeypatch.setattr("smorg.core.state.SeenState.save", refuse_save)

    notified: list[str] = []
    monkeypatch.setattr(
        "smorg.integrations.linear.panel.LinearPanel.notify",
        lambda self, message, **kwargs: notified.append(message),
    )

    panel = panel_with(issue("ENG-1"))
    panel.action_open_selected()

    assert notified == ["No space left on device"]
    # The in-memory mark clears even though persisting it to disk failed.
    assert panel.seen.is_changed("linear", issue("ENG-1")) is False


# --- Opening the detail pane also counts as "having looked" ---


@pytest.mark.asyncio
async def test_enter_marks_the_selected_issue_seen(monkeypatch):
    monkeypatch.setattr("smorg.core.state.SeenState.save", lambda self: None)

    panel = panel_with(issue("ENG-1"))
    async with _LinearPanelHarness(panel).run_test() as pilot:
        panel.focus()
        await pilot.pause()
        assert panel.seen.is_changed("linear", issue("ENG-1")) is True

        await pilot.press("enter")
        await pilot.pause()

    assert panel.seen.is_changed("linear", issue("ENG-1")) is False


@pytest.mark.asyncio
async def test_a_second_enter_that_closes_the_pane_does_not_remark_it(monkeypatch):
    saves: list[None] = []
    monkeypatch.setattr("smorg.core.state.SeenState.save", lambda self: saves.append(None))

    panel = panel_with(issue("ENG-1"))
    async with _LinearPanelHarness(panel).run_test() as pilot:
        panel.focus()
        await pilot.pause()
        await pilot.press("enter")  # opens and marks
        await pilot.pause()
        assert len(saves) == 1

        await pilot.press("enter")  # closes
        await pilot.pause()
        assert panel.detail_open is False

    assert len(saves) == 1


@pytest.mark.asyncio
async def test_enter_on_a_different_issue_while_the_pane_is_open_marks_that_issue(monkeypatch):
    monkeypatch.setattr("smorg.core.state.SeenState.save", lambda self: None)

    panel = panel_with(issue("ENG-1"), issue("ENG-2"))
    async with _LinearPanelHarness(panel).run_test() as pilot:
        panel.focus()
        await pilot.pause()
        await pilot.press("enter")  # opens on ENG-1, marks ENG-1
        await pilot.pause()
        assert panel.seen.is_changed("linear", issue("ENG-2")) is True

        await pilot.press("down")  # cursor moves; pane still showing ENG-1
        await pilot.pause()
        await pilot.press("enter")  # switches the pane to ENG-2, marks ENG-2
        await pilot.pause()

    assert panel.seen.is_changed("linear", issue("ENG-2")) is False


@pytest.mark.asyncio
async def test_a_hostile_title_is_never_interpreted_as_markup_in_the_real_render():
    hostile = replace(issue("ENG-1"), title="[red]x[/red]")
    panel = panel_with(hostile)
    async with _LinearPanelHarness(panel).run_test() as pilot:
        panel.refresh()
        await pilot.pause()
        body = panel.query_one("#body", Static)
        rendered = "".join(body.render_line(y).text for y in range(body.size.height))

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
        body = panel.query_one("#body", Static)
        rendered = "".join(body.render_line(y).text for y in range(body.size.height))

    assert "[blue]Weird[/blue]" in rendered


def test_plain_output_is_derived_from_the_styled_render():
    """One row builder: the plain path must be the styled Text's own .plain."""
    panel = panel_with(issue("ENG-1", "In Review"), issue("ENG-2", "Todo"))
    assert panel.body_text() == panel.render_ready().plain.strip()


# --- Detail region ---


def detail(description: str = "the description", *bodies: str) -> IssueDetail:
    return IssueDetail(
        description=description,
        assignee="Lucas",
        comments=tuple(Comment(author="alice", body=body, created_at=NOW) for body in bodies),
    )


def _overflowing_description(lines: int = 80) -> str:
    """`lines` paragraphs, blank-line-separated so each stays its own Markdown
    paragraph instead of reflowing into one soft-wrapped block — this keeps
    the rendered row count real regardless of viewport height.
    """
    return "\n\n".join(f"line {index}" for index in range(lines))


def region_text(panel: LinearPanel) -> str:
    content = panel.query_one("#detail-content", Static)
    # Static in this Textual version exposes the raw value passed to update()
    # via `.content` (`.renderable` no longer exists). The hint/error states
    # are a bare Text; loaded issue detail is a rich.console.Group (header
    # Text plus rich.markdown.Markdown), which has no `.plain` of its own —
    # rendering it through a real Console gets the string a reader would see.
    value = content.content
    if isinstance(value, Text):
        return value.plain
    buffer = io.StringIO()
    Console(width=80, file=buffer, force_terminal=False).print(value)
    return buffer.getvalue()


async def open_detail(pilot, panel):
    panel.focus()
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.asyncio
async def test_enter_opens_the_detail_region_in_a_loading_state():
    panel = panel_with(issue("ENG-1"))
    requested: list = []
    async with _LinearPanelHarness(panel).run_test() as pilot:
        panel.post_message = _capture(panel, requested)  # see helper below
        await open_detail(pilot, panel)

        region = panel.query_one("#detail", VerticalScroll)
        assert panel.detail_open is True
        assert region.has_class("-open")
        assert "loading" in region_text(panel)
    assert len(requested) == 1 and requested[0].item.id == "ENG-1"


def _capture(panel, into):
    original = type(panel).post_message

    def wrapped(message):
        if isinstance(message, panel.DetailRequested):
            into.append(message)
        return original(panel, message)

    return wrapped


@pytest.mark.asyncio
async def test_show_detail_renders_header_description_and_comments():
    panel = panel_with(issue("ENG-1"))
    async with _LinearPanelHarness(panel).run_test() as pilot:
        await open_detail(pilot, panel)
        key = panel.detail_key(issue("ENG-1"))
        panel.show_detail(key, detail("the description", "first comment", "second comment"))
        await pilot.pause()

        text = region_text(panel)
    assert "ENG-1" in text and "In Review" in text and "Lucas" in text
    assert "the description" in text
    assert "alice" in text and "first comment" in text and "second comment" in text


@pytest.mark.asyncio
async def test_enter_again_on_the_same_issue_closes_the_region():
    panel = panel_with(issue("ENG-1"))
    async with _LinearPanelHarness(panel).run_test() as pilot:
        await open_detail(pilot, panel)
        await pilot.press("enter")
        await pilot.pause()
        assert panel.detail_open is False
        assert not panel.query_one("#detail", VerticalScroll).has_class("-open")


@pytest.mark.asyncio
async def test_moving_the_cursor_shows_a_hint_for_an_unloaded_issue():
    panel = panel_with(issue("ENG-1"), issue("ENG-2"))
    async with _LinearPanelHarness(panel).run_test() as pilot:
        await open_detail(pilot, panel)
        panel.show_detail(panel.detail_key(issue("ENG-1")), detail())
        await pilot.press("down")
        await pilot.pause()
        assert "press enter to load" in region_text(panel)

        await pilot.press("up")
        await pilot.pause()
        assert "the description" in region_text(panel)  # cache hit, no refetch


@pytest.mark.asyncio
async def test_a_detail_error_renders_in_the_region_and_enter_retries():
    panel = panel_with(issue("ENG-1"))
    requested: list = []
    async with _LinearPanelHarness(panel).run_test() as pilot:
        panel.post_message = _capture(panel, requested)
        await open_detail(pilot, panel)
        panel.show_detail_error(panel.detail_key(issue("ENG-1")), "linear is down")
        await pilot.pause()
        assert "could not load: linear is down" in region_text(panel)

        await pilot.press("enter")  # same issue: closes
        await pilot.press("enter")  # reopens: error is not cached as data, refetches
        await pilot.pause()
    assert len(requested) == 2


@pytest.mark.asyncio
async def test_a_hostile_description_is_never_interpreted_as_markup():
    panel = panel_with(issue("ENG-1"))
    async with _LinearPanelHarness(panel).run_test() as pilot:
        await open_detail(pilot, panel)
        panel.show_detail(panel.detail_key(issue("ENG-1")), detail("[red]x[/red]"))
        await pilot.pause()
        content = panel.query_one("#detail-content", Static)
        rendered = "".join(content.render_line(y).text for y in range(content.size.height))
    assert "[red]x[/red]" in rendered


@pytest.mark.asyncio
async def test_shift_down_scrolls_an_overflowing_detail():
    long_description = _overflowing_description()
    panel = panel_with(issue("ENG-1"))
    async with _LinearPanelHarness(panel).run_test() as pilot:
        await open_detail(pilot, panel)
        panel.show_detail(panel.detail_key(issue("ENG-1")), detail(long_description))
        await pilot.pause()
        region = panel.query_one("#detail", VerticalScroll)
        before = region.scroll_offset.y
        await pilot.press("shift+down")
        await pilot.pause()
        assert region.scroll_offset.y > before


@pytest.mark.asyncio
async def test_refresh_preserves_scroll_but_a_cursor_move_resets_it():
    long_description = _overflowing_description()
    panel = panel_with(issue("ENG-1"), issue("ENG-2"))
    async with _LinearPanelHarness(panel).run_test() as pilot:
        await open_detail(pilot, panel)
        panel.show_detail(panel.detail_key(issue("ENG-1")), detail(long_description))
        await pilot.pause()
        await pilot.press("shift+down", "shift+down", "shift+down")
        await pilot.pause()
        region = panel.query_one("#detail", VerticalScroll)
        scrolled = region.scroll_offset.y
        assert scrolled > 0

        panel.refresh()
        await pilot.pause()
        assert region.scroll_offset.y == scrolled

        await pilot.press("down")
        await pilot.pause()
        assert region.scroll_offset.y == 0


def test_render_detail_renders_markdown_and_keeps_the_header_and_byline():
    md_description = "**bold**\n\n## Heading\n\n`code`\n\n- item one"
    panel = panel_with(issue("ENG-1"))
    rendered = panel.render_detail(issue("ENG-1"), detail(md_description, "**also bold**"))

    buffer = io.StringIO()
    Console(width=80, file=buffer, force_terminal=False).print(rendered)
    text = buffer.getvalue()

    assert "**" not in text
    assert "bold" in text
    assert "Heading" in text
    assert "code" in text
    assert "item one" in text
    # Header and comment byline still present alongside the rendered markdown.
    assert "ENG-1" in text and "In Review" in text and "Lucas" in text
    assert "alice" in text


async def _render_detail_segments(description: str):
    """Open the detail pane on a single issue, load `description`, and
    return the real rendered Segments (not a bare Console.print) — needed to
    prove a style survives Textual's own rendering path, not just rich's.
    """
    panel = panel_with(issue("ENG-1"))
    async with _LinearPanelHarness(panel).run_test() as pilot:
        await open_detail(pilot, panel)
        panel.show_detail(panel.detail_key(issue("ENG-1")), detail(description))
        await pilot.pause()
        content = panel.query_one("#detail-content", Static)
        return [
            segment
            for y in range(content.size.height)
            for segment in content.render_line(y)
            if segment.text.strip()
        ]


@pytest.mark.asyncio
async def test_inline_code_carries_no_background_through_the_real_render_path():
    """Rich's default "markdown.code" style paints a hardcoded black chip
    behind inline code, contrasting with the surrounding text's own
    (theme-dependent) background — wrong on a light terminal. Checked against
    the real Static widget (not a bare Console.print), since the fix hooks
    the console's theme stack at render time and a bare Console would not
    prove it survives Textual's own rendering path.
    """
    segments = await _render_detail_segments("plain text and `inline code` here")

    code_style = next(segment.style for segment in segments if "inline code" in segment.text)
    plain_style = next(segment.style for segment in segments if "plain text" in segment.text)
    assert code_style is not None and plain_style is not None

    # No background of its own: whatever the ambient widget background is,
    # code and plain text share it rather than code carrying a distinct chip.
    assert code_style.bgcolor == plain_style.bgcolor
    assert code_style.bold is True
    assert code_style.color is not None and code_style.color.name == "cyan"
    # The theme override targets only the "markdown.code" style name, so
    # ordinary paragraph text is unaffected — it doesn't pick up code's look.
    assert not (plain_style.bold and plain_style.color and plain_style.color.name == "cyan")


@pytest.mark.asyncio
async def test_a_linear_link_carries_the_href_as_style_link_through_the_real_render_path():
    """rich's Markdown(hyperlinks=True) — its own default — carries a link's
    href as Style.link (which Textual's strip.py turns into an OSC 8
    sequence) rather than printing the raw URL, so the visible text is just
    the label.
    """
    href = "https://linear.app/x/issue/CTRL-19"
    segments = await _render_detail_segments(f"blocked by [CTRL-19]({href}) also plain text")

    link_style = next(segment.style for segment in segments if "CTRL-19" in segment.text)
    assert link_style is not None
    assert link_style.link == href
    assert link_style.underline is True
    assert link_style.color is not None and link_style.color.name == "bright_blue"

    full_text = "".join(segment.text for segment in segments)
    assert href not in full_text


@pytest.mark.asyncio
async def test_a_local_path_code_span_is_underlined_when_the_check_is_forced_true(monkeypatch):
    monkeypatch.setattr("smorg.shell.markdown.is_local_path", lambda text: True)
    segments = await _render_detail_segments("open `src/app.py` please")

    code_style = next(segment.style for segment in segments if "src/app.py" in segment.text)
    assert code_style is not None
    assert code_style.underline is True
    # Still the same base inline-code look, just with underline added on top.
    assert code_style.bold is True
    assert code_style.color is not None and code_style.color.name == "cyan"


@pytest.mark.asyncio
async def test_a_local_path_code_span_is_not_underlined_when_the_check_is_forced_false(
    monkeypatch,
):
    monkeypatch.setattr("smorg.shell.markdown.is_local_path", lambda text: False)
    segments = await _render_detail_segments("open `src/app.py` please")

    code_style = next(segment.style for segment in segments if "src/app.py" in segment.text)
    assert code_style is not None
    assert not code_style.underline


@pytest.mark.asyncio
async def test_non_code_text_is_never_underlined_by_the_local_path_check(monkeypatch):
    monkeypatch.setattr("smorg.shell.markdown.is_local_path", lambda text: True)
    segments = await _render_detail_segments("plain text and `src/app.py` here")

    plain_style = next(segment.style for segment in segments if "plain text" in segment.text)
    assert plain_style is None or not plain_style.underline


@pytest.mark.asyncio
async def test_a_code_span_naming_a_real_file_is_underlined_via_the_actual_filesystem_check(
    tmp_path, monkeypatch
):
    """No monkeypatched is_local_path here — this exercises the real stat,
    relative to a chdir'd cwd, the same way the function resolves paths for
    real. Distinct filenames (not one a substring of the other) avoid a false
    match when picking each span's segment out by text.
    """
    (tmp_path / "existing.txt").write_text("hi")
    monkeypatch.chdir(tmp_path)
    is_local_path.cache_clear()
    try:
        segments = await _render_detail_segments("see `existing.txt` and `missing.txt`")
    finally:
        is_local_path.cache_clear()

    existing_style = next(segment.style for segment in segments if "existing.txt" in segment.text)
    missing_style = next(segment.style for segment in segments if "missing.txt" in segment.text)
    assert existing_style is not None and existing_style.underline is True
    assert missing_style is not None and not missing_style.underline


def _rendered_detail_text(panel: LinearPanel, detail_obj) -> str:
    rendered = panel.render_detail(issue("ENG-1"), detail_obj)
    buffer = io.StringIO()
    Console(width=80, file=buffer, force_terminal=False).print(rendered)
    return buffer.getvalue()


def test_render_detail_shows_the_earlier_comment_count_above_the_first_comment():
    detail_obj = replace(detail("d", "shown comment"), hidden_comments=3)
    text = _rendered_detail_text(panel_with(issue("ENG-1")), detail_obj)
    assert "… 3 earlier comments" in text
    assert text.index("earlier comments") < text.index("shown comment")


def test_render_detail_uses_the_singular_for_exactly_one_hidden_comment():
    detail_obj = replace(detail("d"), hidden_comments=1)
    text = _rendered_detail_text(panel_with(issue("ENG-1")), detail_obj)
    assert "… 1 earlier comment" in text
    assert "1 earlier comments" not in text


def test_render_detail_appends_a_plus_for_a_lower_bound_hidden_count():
    detail_obj = replace(detail("d"), hidden_comments=20, hidden_is_lower_bound=True)
    text = _rendered_detail_text(panel_with(issue("ENG-1")), detail_obj)
    assert "… 20+ earlier comments" in text


def test_render_detail_shows_nothing_when_no_comments_are_hidden():
    detail_obj = detail("d", "only comment")
    text = _rendered_detail_text(panel_with(issue("ENG-1")), detail_obj)
    assert "earlier comment" not in text


@pytest.mark.asyncio
async def test_the_gutter_shows_a_down_arrow_then_switches_to_an_up_arrow():
    from smorg.shell.panel import _DetailGutter

    long_description = _overflowing_description()
    panel = panel_with(issue("ENG-1"))
    async with _LinearPanelHarness(panel).run_test() as pilot:
        await open_detail(pilot, panel)
        panel.show_detail(panel.detail_key(issue("ENG-1")), detail(long_description))
        await pilot.pause()

        gutter = panel.query_one(_DetailGutter)

        def gutter_text() -> str:
            return "".join(gutter.render_line(y).text for y in range(gutter.size.height))

        at_top = gutter_text()
        assert "↓" in at_top
        assert "↑" not in at_top

        region = panel.query_one("#detail", VerticalScroll)
        region.scroll_end(animate=False)
        await pilot.pause()

        at_bottom = gutter_text()
    assert "↑" in at_bottom
    assert "↓" not in at_bottom

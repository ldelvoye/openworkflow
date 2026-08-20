"""Tests for the GitHub panel: two columns, a cursor in each, and no network."""

import io
from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text

from smorg.core.state import SeenState
from smorg.integrations.github.panel import _COLUMNS, GitHubPanel
from smorg.integrations.github.source import (
    Category,
    PullRequest,
    PullRequestDetail,
    Review,
)
from smorg.shell.panel import PanelState

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

LEFT_TITLE, LEFT_CATEGORIES = _COLUMNS[0]
RIGHT_TITLE, RIGHT_CATEGORIES = _COLUMNS[1]


def pull(
    number: int = 42,
    category: Category = Category.NEEDS_YOUR_REVIEW,
    repository: str = "octocat/hello",
    title: str | None = None,
) -> PullRequest:
    return PullRequest(
        id=f"{repository}#{number}",
        updated_at=NOW,
        url=f"https://github.com/{repository}/pull/{number}",
        number=number,
        title=title if title is not None else f"title of #{number}",
        repository=repository,
        author="octocat",
        category=category,
    )


def panel_with(*pulls: PullRequest, seen: SeenState | None = None) -> GitHubPanel:
    panel = GitHubPanel()
    panel.state = PanelState.READY
    panel.items = pulls
    panel.seen = seen or SeenState({})
    panel.integration_id = "github"
    return panel


def rendered(panel: GitHubPanel, width: int = 100) -> str:
    console = Console(width=width, file=io.StringIO(), force_terminal=False)
    with console.capture() as capture:
        console.print(panel.render_ready())
    return capture.get()


def test_the_panel_never_fetches():
    """The seam the whole design rests on, enforced rather than trusted."""
    source = (Path("src") / "smorg" / "integrations" / "github" / "panel.py").read_text()
    assert "httpx" not in source
    assert "Github(" not in source
    assert "import requests" not in source
    assert "fetch" not in source


# --- Two columns, side by side ---


def test_both_column_titles_land_on_the_same_line():
    """The whole point of the layout: the review inbox and your own pull
    requests are read side by side, not one after the other."""
    panel = panel_with(pull(), pull(51, Category.DRAFT))

    first_line = rendered(panel).splitlines()[0]

    assert LEFT_TITLE in first_line
    assert RIGHT_TITLE in first_line


def test_every_declared_category_gets_a_heading():
    text = panel_with(pull()).ready_text()

    for _, categories in _COLUMNS:
        for category in categories:
            assert str(category) in text


def test_an_empty_category_still_shows_its_heading_and_a_count_of_zero():
    """A section that vanishes when empty and a section that was never
    fetched look identical; a heading reading (0) says which this is."""
    text = panel_with(pull()).ready_text()

    assert f"{Category.READY_TO_MERGE} (0)" in text
    assert f"{Category.NEEDS_YOUR_REVIEW} (1)" in text


def test_a_pull_request_is_drawn_under_the_category_the_source_gave_it():
    text = panel_with(pull(51, Category.DRAFT)).ready_text()
    lines = text.splitlines()
    heading = lines.index(f"{Category.DRAFT} (1)")

    assert "#51" in lines[heading + 1]


def test_a_row_names_the_repository_and_the_number():
    """A review inbox spans repositories, so a bare number identifies nothing."""
    text = panel_with(pull(7, repository="octocat/tools")).ready_text()

    assert "octocat/tools#7" in text


# --- Change marks ---


def test_a_changed_pull_request_is_marked_and_a_seen_one_is_not():
    seen = SeenState({})
    unchanged = pull(7, repository="octocat/tools")
    seen.mark_seen("github", unchanged)

    text = panel_with(pull(42), unchanged, seen=seen).ready_text()
    marked = [line for line in text.splitlines() if "●" in line]

    assert any("#42" in line for line in marked)
    assert not any("#7" in line for line in marked)


# --- Selection moves within a column, and between them ---


def test_the_selection_starts_in_the_review_inbox():
    panel = panel_with(pull(42), pull(51, Category.DRAFT))

    selected = panel.selected_item()

    assert selected is not None
    assert selected.number == 42


def test_up_and_down_move_within_the_focused_column_only():
    panel = panel_with(
        pull(42, Category.NEEDS_YOUR_REVIEW),
        pull(43, Category.NEEDS_TEAM_REVIEW),
        pull(51, Category.DRAFT),
    )

    panel.action_cursor_down()
    first = panel.selected_item()

    panel.action_cursor_down()
    second = panel.selected_item()

    assert first is not None and first.number == 43
    # Wraps inside the column rather than crossing into the other one.
    assert second is not None and second.number == 42


def test_right_moves_the_selection_into_your_own_pull_requests():
    panel = panel_with(pull(42), pull(51, Category.DRAFT))

    panel.action_next_column()
    selected = panel.selected_item()

    assert selected is not None
    assert selected.number == 51


def test_each_column_keeps_its_own_cursor():
    """Switching away and back returns to the row you left, not to the top."""
    panel = panel_with(
        pull(42, Category.NEEDS_YOUR_REVIEW),
        pull(43, Category.NEEDS_TEAM_REVIEW),
        pull(51, Category.DRAFT),
    )
    panel.action_cursor_down()

    panel.action_next_column()
    panel.action_previous_column()
    selected = panel.selected_item()

    assert selected is not None
    assert selected.number == 43


def test_an_empty_column_has_nothing_selected_and_moving_is_a_no_op():
    panel = panel_with(pull(42))

    panel.action_next_column()
    panel.action_cursor_down()

    assert panel.selected_item() is None
    assert panel.selected_url() is None


def test_the_open_action_returns_the_url_of_the_selected_pull_request():
    panel = panel_with(pull(42), pull(51, Category.DRAFT))
    panel.action_next_column()

    assert panel.selected_url() == "https://github.com/octocat/hello/pull/51"


# --- Server text cannot restyle the panel ---


def test_a_title_that_looks_like_markup_is_drawn_literally():
    """Rich markup in a title would otherwise let somebody else's pull request
    colour or hide rows in your dashboard."""
    panel = panel_with(pull(42, title="[red]danger[/red]"))

    assert "[red]danger[/red]" in panel.ready_text()


@pytest.mark.parametrize("width", [40, 60, 120])
def test_no_row_wraps_at_any_width(width):
    """A wrapped title spills into the next row's place and breaks a grid that
    is already only half the screen wide."""
    panel = panel_with(pull(42, title="a very long title " * 12))

    lines = rendered(panel, width=width).splitlines()

    assert all(len(line.rstrip()) <= width for line in lines)


# --- The detail pane ---


def detail(**overrides) -> PullRequestDetail:
    fields = {
        "body": "Splits the loader in two.",
        "base": "main",
        "head": "tidy-loader",
        "reviews": (Review(author="hubot", state="CHANGES_REQUESTED", submitted_at=NOW),),
    }
    return PullRequestDetail(**(fields | overrides))


def plain(panel: GitHubPanel, item: PullRequest, shown: PullRequestDetail) -> str:
    console = Console(width=100, file=io.StringIO(), force_terminal=False)
    with console.capture() as capture:
        console.print(panel.render_detail(item, shown))
    return capture.get()


def test_the_detail_names_the_pull_request_its_branches_and_its_reviews():
    item = pull(42)
    text = plain(panel_with(item), item, detail())

    assert "octocat/hello#42" in text
    assert "tidy-loader" in text
    assert "main" in text
    assert "hubot" in text


def test_a_review_state_reads_as_words():
    item = pull(42)
    text = plain(panel_with(item), item, detail())

    assert "changes requested" in text


def test_a_pull_request_with_no_description_says_so():
    """Empty and unloaded look identical otherwise."""
    item = pull(42)
    text = plain(panel_with(item), item, detail(body=""))

    assert "no description" in text


def test_dropped_reviews_are_counted_rather_than_silently_missing():
    item = pull(42)
    text = plain(panel_with(item), item, detail(hidden_reviews=3))

    assert "3 earlier reviews" in text


def test_a_capped_review_count_reads_as_a_lower_bound():
    item = pull(42)
    text = plain(panel_with(item), item, detail(hidden_reviews=20, hidden_is_lower_bound=True))

    assert "20+ earlier reviews" in text


def test_an_unrecognised_detail_shape_falls_back_instead_of_crashing():
    item = pull(42)
    fallback = panel_with(item).render_detail(item, object())

    assert isinstance(fallback, Text)

"""How GitHub pull requests look. No network calls happen here; that is source.py's job.

Two columns, because the two questions this tab answers are different questions:
the left one is what other people are waiting on you for, the right one is what
you are waiting on other people for. Reading them as one list would bury
whichever is shorter today.

Server-controlled strings (repository, title, author) are appended to
rich.text.Text literally rather than interpolated into a markup string, so a
pull request titled like "[red]x[/red]" cannot style or hide anything.
"""

from __future__ import annotations

import webbrowser

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.binding import Binding

from smorg.core.contract import Item
from smorg.integrations.github.source import Category, PullRequest, PullRequestDetail, Review
from smorg.shell.format import age
from smorg.shell.markdown import Markdown
from smorg.shell.panel import Panel

CHANGED_MARK = "●"
# A standard ANSI color name (not a hex/truecolor value or a Textual $variable),
# so it renders through the terminal's own green under the app's ansi theme
# rather than an approximated RGB shade.
CHANGE_STYLE = "green"
SELECTED_MARK = "▸"
EMPTY_SECTION = "  —"

# One column per question, and the categories under each in the order they are
# worth looking at: on the right, what is not out for review yet, then what is
# waiting on somebody else, then what is waiting on you, then what is done.
COLUMNS: tuple[tuple[str, tuple[Category, ...]], ...] = (
    (
        "review inbox",
        (Category.NEEDS_YOUR_REVIEW, Category.NEEDS_TEAM_REVIEW),
    ),
    (
        "your pull requests",
        (Category.DRAFT, Category.WAITING, Category.NEEDS_ACTION, Category.READY_TO_MERGE),
    ),
)

COLUMN_TITLE_STYLE = "bold underline"

# Colored by who the section is waiting on: red where the ball is in your court,
# green where nothing is left to do, plain where it sits with someone else.
_CATEGORY_STYLES = {
    Category.NEEDS_YOUR_REVIEW: "bold red",
    Category.NEEDS_TEAM_REVIEW: "bold",
    Category.DRAFT: "bold",
    Category.WAITING: "bold yellow",
    Category.NEEDS_ACTION: "bold red",
    Category.READY_TO_MERGE: "bold green",
}

Section = tuple[Category, tuple[PullRequest, ...]]


def _heading(category: Category, pulls: tuple[PullRequest, ...]) -> str:
    """A count on every heading, including zero: "needs your review (0)" says
    the section was looked at, where a bare heading over nothing does not."""
    return f"{category} ({len(pulls)})"


def _review_label(state: str) -> str:
    """ "CHANGES_REQUESTED" -> "changes requested"."""
    return state.replace("_", " ").casefold()


def _hidden_reviews_line(hidden: int, lower_bound: bool) -> Text:
    # Singular only for an exact count of one — a lower bound of "1" still
    # means "at least one", which reads as plural.
    noun = "review" if hidden == 1 and not lower_bound else "reviews"
    count = f"{hidden}+" if lower_bound else str(hidden)
    return Text(f"… {count} earlier {noun}", style="dim")


class GitHubPanel(Panel):
    BINDINGS = [
        Binding("up", "cursor_up", "select pull request", show=False),
        Binding("down", "cursor_down", "select pull request", show=False),
        Binding("left", "previous_column", "switch column", show=False),
        Binding("right", "next_column", "switch column", show=False),
        Binding("o", "open_selected", "open in browser", show=False),
        Binding("enter", "toggle_detail", "view details", show=False),
        Binding("shift+up", "scroll_detail_up", "scroll details", show=False),
        Binding("shift+down", "scroll_detail_down", "scroll details", show=False),
    ]
    can_focus = True

    def __init__(self) -> None:
        super().__init__()
        self.column = 0
        # One cursor per column, so switching back to a column returns to where
        # it was rather than to its first row.
        self.cursors = [0 for _ in COLUMNS]

    def _sections(self, column: int) -> tuple[Section, ...]:
        """This column's categories with their pull requests, always all of
        them — a section with nothing in it still shows, since a missing
        heading and an empty one say different things.

        Rows keep the order the source returned them in (newest first), so a
        refresh never reshuffles a column under the cursor.
        """
        _, categories = COLUMNS[column]
        grouped: dict[Category, list[PullRequest]] = {category: [] for category in categories}
        for pull in self.items:
            if isinstance(pull, PullRequest) and pull.category in grouped:
                grouped[pull.category].append(pull)
        return tuple((category, tuple(grouped[category])) for category in categories)

    def _ordered(self, column: int) -> tuple[PullRequest, ...]:
        """This column's pull requests as one sequence, in the order they are
        drawn — which is the sequence the cursor moves through."""
        ordered: list[PullRequest] = []
        for _, pulls in self._sections(column):
            ordered.extend(pulls)
        return tuple(ordered)

    def _clamped_cursor(self, column: int) -> int:
        count = len(self._ordered(column))
        if count == 0:
            return 0
        return min(self.cursors[column], count - 1)

    def selected_item(self) -> PullRequest | None:
        ordered = self._ordered(self.column)
        if not ordered:
            return None
        return ordered[self._clamped_cursor(self.column)]

    def selected_url(self) -> str | None:
        pull = self.selected_item()
        return pull.url if pull is not None else None

    def render_ready(self) -> RenderableType:
        grid = Table.grid(expand=True, padding=(0, 2))
        for _ in COLUMNS:
            grid.add_column(ratio=1)
        grid.add_row(*(self._column(index) for index in range(len(COLUMNS))))
        return grid

    def ready_text(self) -> str:
        lines: list[str] = []
        for column, (title, _) in enumerate(COLUMNS):
            if lines:
                lines.append("")
            lines.append(title)
            for line in self._column_lines(column):
                lines.append(line.plain)
        return "\n".join(lines)

    def _column(self, column: int) -> RenderableType:
        title, _ = COLUMNS[column]
        parts: list[RenderableType] = [Text(title, style=COLUMN_TITLE_STYLE)]
        parts.extend(self._column_lines(column))
        return Group(*parts)

    def _column_lines(self, column: int) -> list[Text]:
        """Every line under a column's title, headings and rows alike, so the
        rendered view and ready_text() cannot drift apart."""
        cursor = self._clamped_cursor(column)
        focused = column == self.column
        lines: list[Text] = []
        position = 0
        for category, pulls in self._sections(column):
            lines.append(Text())
            lines.append(Text(_heading(category, pulls), style=_CATEGORY_STYLES[category]))
            if not pulls:
                lines.append(Text(EMPTY_SECTION, style="dim"))
            for pull in pulls:
                lines.append(self._row(pull, focused and position == cursor))
                position += 1
        return lines

    def _row(self, pull: PullRequest, selected: bool) -> Text:
        if selected:
            row = Text(style="bold")
            marker = f"{SELECTED_MARK} "
        else:
            row = Text()
            marker = "  "
        row.append(marker)

        changed = self.seen.is_changed(self.integration_id, pull)
        if changed:
            mark_char = CHANGED_MARK
            mark_style = CHANGE_STYLE
        else:
            mark_char = " "
            mark_style = None
        row.append(mark_char, style=mark_style)
        row.append(" ")
        row.append(f"{pull.repository}#{pull.number}", style="dim")
        row.append("  ")
        row.append(pull.title)
        # One row per pull request: a wrapped title spills into the next row's
        # place and breaks a column that is already only half the screen wide.
        # The full title is one "o" away.
        row.no_wrap = True
        row.overflow = "ellipsis"
        return row

    def render_detail(self, item: Item, detail: object) -> RenderableType:
        if not isinstance(detail, PullRequestDetail):
            return super().render_detail(item, detail)
        parts: list[RenderableType] = [self._detail_header(item, detail), Text()]
        # Markdown() interprets its input as CommonMark, not Rich's own
        # "[style]" markup, so a hostile "[red]x[/red]" body can't style or
        # hide anything — only headings/emphasis/code/lists render as markdown.
        if detail.body:
            body = detail.body
        else:
            body = "no description"
        parts.append(Markdown(body, code_theme="ansi_dark"))
        if detail.hidden_reviews or detail.hidden_is_lower_bound:
            parts.append(Text())
            parts.append(_hidden_reviews_line(detail.hidden_reviews, detail.hidden_is_lower_bound))
        for review in detail.reviews:
            parts.append(Text())
            parts.append(self._review_line(review))
        return Group(*parts)

    def _detail_header(self, item: Item, detail: PullRequestDetail) -> Text:
        header = Text()
        header.append(item.id, style="dim")
        if isinstance(item, PullRequest):
            header.append(" · ")
            header.append(str(item.category))
            if item.author:
                header.append(" · ")
                header.append(item.author)
        if detail.head and detail.base:
            header.append(" · ")
            header.append(f"{detail.head} → {detail.base}", style="dim")
        return header

    def _review_line(self, review: Review) -> Text:
        line = Text(style="dim")
        if review.author:
            author = review.author
        else:
            author = "someone"
        line.append(author)
        line.append(" · ")
        line.append(_review_label(review.state))
        if review.submitted_at is not None:
            line.append(" · ")
            line.append(age(review.submitted_at))
        return line

    def action_open_selected(self) -> None:
        pull = self.selected_item()
        if pull is None:
            return
        webbrowser.open(pull.url)
        self.mark_seen(pull)

    def action_toggle_detail(self) -> None:
        super().action_toggle_detail()
        # Opening the detail pane also counts as "having looked" at it.
        pull = self.selected_item()
        if pull is not None and self.detail_showing(pull):
            self.mark_seen(pull)

    def action_cursor_down(self) -> None:
        self._move(1)

    def action_cursor_up(self) -> None:
        self._move(-1)

    def _move(self, offset: int) -> None:
        ordered = self._ordered(self.column)
        if not ordered:
            return
        self.cursors[self.column] = (self._clamped_cursor(self.column) + offset) % len(ordered)
        self.refresh()

    def action_previous_column(self) -> None:
        self._switch_column(-1)

    def action_next_column(self) -> None:
        self._switch_column(1)

    def _switch_column(self, offset: int) -> None:
        self.column = (self.column + offset) % len(COLUMNS)
        self.refresh()

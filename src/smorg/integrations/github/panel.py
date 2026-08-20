"""Two columns:

- one for what other people are waiting on you for
- one for what you are waiting on other people for
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
CHANGE_STYLE = "green"
SELECTED_MARK = "▸"
EMPTY_SECTION = "  —"

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

_CATEGORY_STYLES = {
    Category.NEEDS_YOUR_REVIEW: "bold red",
    Category.NEEDS_TEAM_REVIEW: "bold",
    Category.DRAFT: "bold",
    Category.WAITING: "bold yellow",
    Category.NEEDS_ACTION: "bold red",
    Category.READY_TO_MERGE: "bold green",
}

Section = tuple[Category, tuple[PullRequest, ...]]


def _format_heading(category: Category, pulls: tuple[PullRequest, ...]) -> str:
    """NEEDS_YOUR_REVIEW and three pull requests -> "needs your review (3)" """
    return f"{category} ({len(pulls)})"


def _format_review_label(state: str) -> str:
    """ "CHANGES_REQUESTED" -> "changes requested" """
    return state.replace("_", " ").casefold()


def _format_hidden_reviews_line(hidden: int, lower_bound: bool) -> Text:
    """(1, False) -> "… 1 earlier review"

    (1, True) -> "… 1+ earlier reviews"
    """
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
        # One cursor per column, so switching back returns to where you were.
        self.cursors = [0 for _ in COLUMNS]

    def _sections(self, column: int) -> tuple[Section, ...]:
        """This column's categories with their pull requests."""
        _, categories = COLUMNS[column]
        grouped: dict[Category, list[PullRequest]] = {category: [] for category in categories}
        for pr in self.items:
            if isinstance(pr, PullRequest) and pr.category in grouped:
                grouped[pr.category].append(pr)
        sections: list[Section] = []
        for category in categories:
            prs = tuple[PullRequest, ...](grouped[category])
            sections.append((category, prs))
        return tuple[Section, ...](sections)

    def _ordered(self, column: int) -> tuple[PullRequest, ...]:
        """This column's pull requests as one ordered sequence (newest first)."""
        ordered: list[PullRequest] = []
        for _, prs in self._sections(column):
            ordered.extend(prs)
        return tuple[PullRequest, ...](ordered)

    def _clamped_cursor(self, row_count: int) -> int:
        return min(self.cursors[self.column], row_count - 1)

    def selected_item(self) -> PullRequest | None:
        ordered = self._ordered(self.column)
        if not ordered:
            return None
        index = self._clamped_cursor(len(ordered))
        return ordered[index]

    def selected_url(self) -> str | None:
        pull = self.selected_item()
        return pull.url if pull is not None else None

    def render_ready(self) -> RenderableType:
        grid = Table.grid(expand=True, padding=(0, 2))
        for _ in COLUMNS:
            grid.add_column(ratio=1)
        grid.add_row(*(self._format_column(column_index) for column_index in range(len(COLUMNS))))
        return grid

    def ready_text(self) -> str:
        lines: list[str] = []
        for column, (title, _) in enumerate(COLUMNS):
            if lines:
                lines.append("")
            lines.append(title)
            for line in self._format_column_lines(column):
                lines.append(line.plain)
        return "\n".join(lines)

    def _format_column(self, column: int) -> RenderableType:
        title, _ = COLUMNS[column]
        parts: list[RenderableType] = [Text(title, style=COLUMN_TITLE_STYLE)]
        parts.extend(self._format_column_lines(column))
        return Group(*parts)

    def _format_column_lines(self, column: int) -> list[Text]:
        """Every line under a column's title, headings and rows alike, so the
        rendered view and ready_text() cannot drift apart.
        """
        if column == self.column:
            selected = self.selected_item()
        else:
            selected = None
        lines: list[Text] = []
        for category, pulls in self._sections(column):
            lines.append(Text())
            lines.append(Text(_format_heading(category, pulls), style=_CATEGORY_STYLES[category]))
            if not pulls:
                lines.append(Text(EMPTY_SECTION, style="dim"))
            for pull in pulls:
                lines.append(self._format_row(pull, pull is selected))
        return lines

    def _format_row(self, pull: PullRequest, selected: bool) -> Text:
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
        parts: list[RenderableType] = [self._format_detail_header(item, detail), Text()]
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
            parts.append(
                _format_hidden_reviews_line(detail.hidden_reviews, detail.hidden_is_lower_bound)
            )
        for review in detail.reviews:
            parts.append(Text())
            parts.append(self._format_review_line(review))
        return Group(*parts)

    def _format_detail_header(self, item: Item, detail: PullRequestDetail) -> Text:
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

    def _format_review_line(self, review: Review) -> Text:
        line = Text(style="dim")
        if review.author:
            author = review.author
        else:
            author = "someone"
        line.append(author)
        line.append(" · ")
        line.append(_format_review_label(review.state))
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
        index = self._clamped_cursor(len(ordered))
        self.cursors[self.column] = (index + offset) % len(ordered)
        self.refresh()

    def action_previous_column(self) -> None:
        self._switch_column(-1)

    def action_next_column(self) -> None:
        self._switch_column(1)

    def _switch_column(self, offset: int) -> None:
        self.column = (self.column + offset) % len(COLUMNS)
        self.refresh()

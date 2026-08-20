"""One list of your issues, grouped by status in actionability order."""

from __future__ import annotations

import webbrowser

from rich.console import Group, RenderableType
from rich.text import Text
from textual.binding import Binding

from smorg.core.contract import Item
from smorg.integrations.linear.source import Comment, Issue, IssueDetail
from smorg.shell.format import age
from smorg.shell.markdown import Markdown
from smorg.shell.panel import Panel

_CHANGED_MARK = "●"
_SELECTED_MARK = "▸"

_CHANGE_STYLE = "green"
# Linear's state colors mapped to the nearest ANSI names; keys are casefolded.
_STATUS_STYLES = {
    "in progress": "bold yellow",
    "in review": "bold green",
    "todo": "bold",
    "blocked": "bold red",
}

# The longest glyph ("!!!" for Urgent) sets the column width so titles line up.
_PRIORITY_GLYPHS = {
    "Urgent": ("!!!", "bold red"),
    "High": ("!!", "yellow"),
    "Medium": ("!", None),
}
_FALLBACK_GLYPH = ("·", "dim")
_GLYPH_WIDTH = 3

# Ordered by actionability: doing, shepherding, queued, stuck.
_STATUS_RANKS = {"in progress": 0, "in review": 1, "todo": 3, "blocked": 5}


def _priority_glyph(priority: str) -> tuple[str, str | None]:
    entry = _PRIORITY_GLYPHS.get(priority, _FALLBACK_GLYPH)
    glyph, style = entry
    return glyph.ljust(_GLYPH_WIDTH), style


def _status_style(status: str, status_type: str) -> str:
    # Unknown labels fall back to the stable machine category.
    fallback = "bold yellow" if status_type == "started" else "bold"
    return _STATUS_STYLES.get(status.casefold(), fallback)


def _status_rank(status: str, status_type: str) -> int:
    known = _STATUS_RANKS.get(status.casefold())
    if known is not None:
        return known
    return 2 if status_type == "started" else 4


def _format_hidden_comments_line(hidden: int, lower_bound: bool) -> Text:
    """(1, False) -> "… 1 earlier comment"

    (1, True) -> "… 1+ earlier comments"
    """
    noun = "comment" if hidden == 1 and not lower_bound else "comments"
    count = f"{hidden}+" if lower_bound else str(hidden)
    return Text(f"… {count} earlier {noun}", style="dim")


class LinearPanel(Panel):
    BINDINGS = [
        Binding("up", "cursor_up", "select issue", show=False),
        Binding("down", "cursor_down", "select issue", show=False),
        Binding("o", "open_selected", "open in browser", show=False),
        Binding("enter", "toggle_detail", "view details", show=False),
        Binding("shift+up", "scroll_detail_up", "scroll details", show=False),
        Binding("shift+down", "scroll_detail_down", "scroll details", show=False),
    ]
    can_focus = True

    def __init__(self) -> None:
        super().__init__()
        self.cursor = 0

    def selected_item(self) -> Issue | None:
        issues = self._grouped()
        if not issues:
            return None
        return issues[self._clamped_cursor(len(issues))]

    def render_detail(self, item: Item, detail: object) -> RenderableType:
        if not isinstance(detail, IssueDetail):
            return super().render_detail(item, detail)
        # Markdown() interprets its input as CommonMark, not Rich's own "[style]" markup, so a
        # hostile "[red]x[/red]" body can't style or hide anything — only headings, emphasis,
        # code, and lists render as markdown.
        if detail.description:
            description = detail.description
        else:
            description = "no description"
        parts: list[RenderableType] = [
            self._format_detail_header(item, detail),
            Text(),
            Markdown(description, code_theme="ansi_dark"),
        ]
        if detail.hidden_comments or detail.hidden_is_lower_bound:
            parts.append(Text())
            parts.append(
                _format_hidden_comments_line(detail.hidden_comments, detail.hidden_is_lower_bound)
            )
        for comment in detail.comments:
            parts.append(Text())
            parts.append(self._format_byline(comment))
            parts.append(Markdown(comment.body, code_theme="ansi_dark"))
        return Group(*parts)

    def _format_detail_header(self, item: Item, detail: IssueDetail) -> Text:
        header = Text()
        header.append(item.id, style="dim")
        if isinstance(item, Issue):
            header.append(" · ")
            header.append(item.status)
        if detail.assignee:
            header.append(" · ")
            header.append(detail.assignee)
        return header

    def _format_byline(self, comment: Comment) -> Text:
        byline = Text(style="dim")
        if comment.author:
            author = comment.author
        else:
            author = "someone"
        byline.append(author)
        byline.append(" · ")
        byline.append(age(comment.created_at))
        return byline

    def selected_url(self) -> str | None:
        issue = self.selected_item()
        return issue.url if issue is not None else None

    def action_open_selected(self) -> None:
        issue = self.selected_item()
        if issue is None:
            return
        webbrowser.open(issue.url)
        self.mark_seen(issue)

    def action_toggle_detail(self) -> None:
        super().action_toggle_detail()
        issue = self.selected_item()
        if issue is not None and self.is_detail_showing(issue):
            self.mark_seen(issue)

    def action_cursor_down(self) -> None:
        self._move(1)

    def action_cursor_up(self) -> None:
        self._move(-1)

    def _move(self, offset: int) -> None:
        issues = self._grouped()
        if not issues:
            return
        self.cursor = (self._clamped_cursor(len(issues)) + offset) % len(issues)
        self.refresh()

    def _clamped_cursor(self, count: int) -> int:
        if count == 0:
            return 0
        return min(self.cursor, count - 1)

    def _grouped(self) -> tuple[Issue, ...]:
        """Issues as one ordered sequence: status groups in fixed rank order, so a refresh never
        reshuffles them. The cursor moves through this same sequence.
        """
        groups: dict[str, list[Issue]] = {}
        for issue in self.items:
            if not isinstance(issue, Issue):
                continue
            groups.setdefault(issue.status, []).append(issue)
        ordered_statuses = sorted(
            groups,
            key=lambda status: (
                _status_rank(status, groups[status][0].status_type),
                status.casefold(),
            ),
        )
        ordered_issues: list[Issue] = []
        for status in ordered_statuses:
            ordered_issues.extend(groups[status])
        return tuple(ordered_issues)

    def ready_text(self) -> str:
        return self.render_ready().plain.strip()

    def render_ready(self) -> Text:
        issues = self._grouped()
        cursor = self._clamped_cursor(len(issues))
        # One width for the whole list; a per-group width would shift the title column at every
        # group boundary.
        id_width = max((len(issue.id) for issue in issues), default=0)
        lines: list[Text] = []
        current_status = ""
        for index, issue in enumerate(issues):
            if issue.status != current_status:
                current_status = issue.status
                lines.append(
                    Text(current_status, style=_status_style(issue.status, issue.status_type))
                )
            lines.append(self._format_row(issue, index == cursor, id_width))
        body = Text("\n").join(lines)
        # One row per issue: a wrapped title orphans its tail under the id column and breaks
        # the grid. The full title is one "o" away.
        body.no_wrap = True
        body.overflow = "ellipsis"
        return body

    def _format_row(self, issue: Issue, selected: bool, id_width: int) -> Text:
        if selected:
            row = Text(style="bold")
            marker = f"{_SELECTED_MARK} "
        else:
            row = Text()
            marker = "  "
        row.append(marker)

        changed = self.seen.is_changed(self.integration_id, issue)
        if changed:
            mark_char = _CHANGED_MARK
            mark_style = _CHANGE_STYLE
        else:
            mark_char = " "
            mark_style = None
        row.append(mark_char, style=mark_style)
        row.append(" ")
        row.append(issue.id.ljust(id_width), style="dim")
        row.append("  ")
        glyph, glyph_style = _priority_glyph(issue.priority)
        row.append(glyph, style=glyph_style)
        row.append(" ")
        row.append(issue.title)
        return row

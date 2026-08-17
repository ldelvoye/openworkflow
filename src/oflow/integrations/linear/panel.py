"""How Linear issues look. No network calls happen here; that is source.py's job.

Server-controlled strings (id, status, title) are appended to rich.text.Text
literally rather than interpolated into a markup string, so an issue titled
like "[red]x[/red]" cannot style or hide anything in the panel.
"""

from __future__ import annotations

import webbrowser
from datetime import datetime

from rich.console import Group, RenderableType
from rich.text import Text
from textual.binding import Binding

from oflow.auth.store import now
from oflow.core.config import ConfigError
from oflow.core.contract import Item
from oflow.core.state import SeenState
from oflow.integrations.linear.source import Issue, IssueDetail
from oflow.shell.markdown import Markdown
from oflow.shell.panel import Panel

CHANGED_MARK = "●"
# A standard ANSI color name (not a hex/truecolor value or a Textual $variable),
# so it renders through the terminal's own green under the app's ansi theme
# rather than an approximated RGB shade.
CHANGE_STYLE = "green"
SELECTED_MARK = "▸"

# The longest glyph ("!!!" for Urgent) sets the column width so titles line up
# regardless of which priority a row carries.
_PRIORITY_GLYPHS = {
    "Urgent": ("!!!", "bold red"),
    "High": ("!!", "yellow"),
    "Medium": ("!", None),
}
_FALLBACK_GLYPH = ("·", "dim")
GLYPH_WIDTH = 3

# Linear's own state colors, mapped to the nearest ANSI names so they render
# through the terminal's palette (see CHANGE_STYLE above for why named colors).
# Keys are casefolded.
_STATUS_STYLES = {
    "in progress": "bold yellow",
    "in review": "bold green",
    "todo": "bold",
    "blocked": "bold red",
}

# Ordered by actionability: doing, shepherding, queued, stuck. An unknown
# label falls back to its machine status_type and sorts alphabetically
# against its peers, so the order stays stable between refreshes.
_STATUS_RANKS = {"in progress": 0, "in review": 1, "todo": 3, "blocked": 5}


def _priority_glyph(priority: str) -> tuple[str, str | None]:
    glyph, style = _PRIORITY_GLYPHS.get(priority, _FALLBACK_GLYPH)
    return glyph.ljust(GLYPH_WIDTH), style


def _status_style(status: str, status_type: str) -> str:
    # unknown labels fall back by the stable machine category.
    fallback = "bold yellow" if status_type == "started" else "bold"
    return _STATUS_STYLES.get(status.casefold(), fallback)


def _status_rank(status: str, status_type: str) -> int:
    known = _STATUS_RANKS.get(status.casefold())
    if known is not None:
        return known
    return 2 if status_type == "started" else 4


def _hidden_comments_line(hidden: int, lower_bound: bool) -> Text:
    # Singular only for an exact count of one — a lower bound of "1" still
    # means "at least one", which reads as plural.
    noun = "comment" if hidden == 1 and not lower_bound else "comments"
    count = f"{hidden}+" if lower_bound else str(hidden)
    return Text(f"… {count} earlier {noun}", style="dim")


def _age(moment: datetime) -> str:
    delta = now() - moment
    # A future stamp is clock skew, and anything under a minute reads the
    # same either way.
    if delta.total_seconds() < 60:
        return "now"
    if delta.days >= 1:
        return f"{delta.days}d"
    hours = delta.seconds // 3600
    if hours >= 1:
        return f"{hours}h"
    return f"{delta.seconds // 60}m"


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
        self.seen = SeenState({})
        self.integration_id = "linear"
        self.cursor = 0

    def _selected_issue(self) -> Issue | None:
        issues = self._grouped()
        if not issues:
            return None
        return issues[self._clamped_cursor(len(issues))]

    def selected_item(self) -> Issue | None:
        return self._selected_issue()

    def render_detail(self, item: Item, detail: object) -> RenderableType:
        if not isinstance(detail, IssueDetail):
            return super().render_detail(item, detail)
        header = Text()
        header.append(item.id, style="dim")
        if isinstance(item, Issue):
            header.append(" · ")
            header.append(item.status)
        if detail.assignee:
            header.append(" · ")
            header.append(detail.assignee)
        # Markdown() interprets its input as CommonMark, not Rich's own
        # "[style]" markup, so a hostile "[red]x[/red]" body can't style or
        # hide anything — only headings/emphasis/code/lists render as markdown.
        parts: list[RenderableType] = [
            header,
            Text(),
            Markdown(detail.description or "no description", code_theme="ansi_dark"),
        ]
        if detail.hidden_comments or detail.hidden_is_lower_bound:
            parts.append(Text())
            parts.append(
                _hidden_comments_line(detail.hidden_comments, detail.hidden_is_lower_bound)
            )
        for comment in detail.comments:
            byline = Text(style="dim")
            byline.append(comment.author or "someone")
            byline.append(" · ")
            byline.append(_age(comment.created_at))
            parts.append(Text())
            parts.append(byline)
            parts.append(Markdown(comment.body, code_theme="ansi_dark"))
        return Group(*parts)

    def selected_url(self) -> str | None:
        issue = self._selected_issue()
        return issue.url if issue is not None else None

    def action_open_selected(self) -> None:
        issue = self._selected_issue()
        if issue is None:
            return
        webbrowser.open(issue.url)
        self._mark_seen(issue)

    def action_toggle_detail(self) -> None:
        super().action_toggle_detail()
        # Opening the detail pane also counts as "having looked" at the issue.
        issue = self._selected_issue()
        if issue is not None and self.detail_showing(issue):
            self._mark_seen(issue)

    def _mark_seen(self, issue: Issue) -> None:
        self.seen.mark_seen(self.integration_id, issue)
        try:
            self.seen.save()
        except (ConfigError, OSError) as error:
            # A save failure here is cosmetic — the mark above already cleared
            # in memory — so the panel must keep running rather than crash.
            self.notify(str(error), severity="error")
        self.refresh()

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
        # Groups stay in the fixed rank order above, not first-appearance
        # order, so a refresh never reshuffles them; the cursor tracks this
        # same sequence.
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

    def render_ready(self) -> Text:
        issues = self._grouped()
        cursor = self._clamped_cursor(len(issues))
        # One width for the whole list — a per-group width would shift the
        # title column at every group boundary.
        id_width = max((len(issue.id) for issue in issues), default=0)
        lines: list[Text] = []
        current_status = ""
        for index, issue in enumerate(issues):
            if issue.status != current_status:
                current_status = issue.status
                lines.append(
                    Text(current_status, style=_status_style(issue.status, issue.status_type))
                )
            lines.append(self._row(issue, index == cursor, id_width))
        body = Text("\n").join(lines)
        # One row per issue: a wrapped title orphans its tail under the id
        # column and breaks the grid. The full title is one "o" away.
        body.no_wrap = True
        body.overflow = "ellipsis"
        return body

    def _row(self, issue: Issue, selected: bool, id_width: int) -> Text:
        row = Text(style="bold") if selected else Text()
        row.append(f"{SELECTED_MARK} " if selected else "  ")
        changed = self.seen.is_changed(self.integration_id, issue)
        row.append(CHANGED_MARK if changed else " ", style=CHANGE_STYLE if changed else None)
        row.append(" ")
        row.append(issue.id.ljust(id_width), style="dim")
        row.append("  ")
        glyph, glyph_style = _priority_glyph(issue.priority)
        row.append(glyph, style=glyph_style)
        row.append(" ")
        row.append(issue.title)
        return row

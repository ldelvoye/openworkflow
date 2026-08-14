"""How Linear issues look. No network calls happen here; that is source.py's job.

Server-controlled strings (id, status, title) are appended to rich.text.Text
literally rather than interpolated into a markup string, so an issue titled
like "[red]x[/red]" cannot style or hide anything in the panel.
"""

from __future__ import annotations

import webbrowser

from rich.text import Text
from textual.app import RenderResult
from textual.binding import Binding

from oflow.core.config import ConfigError
from oflow.core.state import SeenState
from oflow.integrations.linear.source import Issue
from oflow.shell.panel import Panel, PanelState

CHANGED_MARK = "●"
# A standard ANSI color name (not a hex/truecolor value or a Textual $variable),
# so it renders through the terminal's own green under the app's ansi theme
# rather than an approximated RGB shade.
CHANGE_STYLE = "green"
SELECTED_MARK = "▸"

# The longest glyph ("!!!" for Urgent) sets the column width so titles line up
# regardless of which priority a row carries.
_PRIORITY_GLYPHS = {"Urgent": "!!!", "High": "!!", "Medium": "!"}
GLYPH_WIDTH = 3


def _priority_glyph(priority: str) -> str:
    return _PRIORITY_GLYPHS.get(priority, "·").ljust(GLYPH_WIDTH)


class LinearPanel(Panel):
    # Up/down and "o" are deliberately unreserved shell-wide (see
    # contract.RESERVED_KEYS) so an integration's panel can own in-panel
    # navigation and its launch action. Keep both cursor actions' descriptions
    # identical so the shell's help overlay merges them into one row. All three
    # are show=False: up/down read as obvious once the panel is focused, and
    # "o" belongs only in the help overlay's integration section, not the
    # footer — opening a browser is a declared launch action, allowed
    # alongside the rule that this panel does no network calls of its own.
    BINDINGS = [
        Binding("up", "cursor_up", "select issue", show=False),
        Binding("down", "cursor_down", "select issue", show=False),
        Binding("o", "open_selected", "open in browser", show=False),
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

    def selected_url(self) -> str | None:
        issue = self._selected_issue()
        return issue.url if issue is not None else None

    def action_open_selected(self) -> None:
        issue = self._selected_issue()
        if issue is None:
            return
        webbrowser.open(issue.url)
        self.seen.mark_seen(self.integration_id, issue)
        try:
            self.seen.save()
        except (ConfigError, OSError) as error:
            # A save failure here is cosmetic — the mark above already cleared
            # in memory — so the panel must keep running rather than crash:
            # ConfigError covers a permissions refusal, OSError covers the disk
            # itself (full, read-only, revoked access), which is what
            # write_private_file/ensure_config_dir actually raise on failure.
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
        # A stable partition by status: issues keep their relative order within
        # a group, and groups appear in the order their status first showed up.
        # Cursor movement walks this same order, so "next row" and "next index"
        # always agree regardless of how the source ordered self.items.
        order: list[str] = []
        groups: dict[str, list[Issue]] = {}
        for issue in self.items:
            if not isinstance(issue, Issue):
                continue
            if issue.status not in groups:
                groups[issue.status] = []
                order.append(issue.status)
            groups[issue.status].append(issue)
        return tuple(issue for status in order for issue in groups[status])

    def render_items(self) -> str:
        issues = self._grouped()
        cursor = self._clamped_cursor(len(issues))
        lines: list[str] = []
        current_status = ""
        for index, issue in enumerate(issues):
            if issue.status != current_status:
                current_status = issue.status
                lines.append(current_status)
            lines.append(self._plain_row(issue, index == cursor))
        return "\n".join(lines).strip()

    def _plain_row(self, issue: Issue, selected: bool) -> str:
        pointer = SELECTED_MARK if selected else " "
        mark = CHANGED_MARK if self.seen.is_changed(self.integration_id, issue) else " "
        glyph = _priority_glyph(issue.priority)
        return f"{pointer} {mark} {issue.id}  {glyph} {issue.title}"

    def render(self) -> RenderResult:
        if self.state is not PanelState.READY:
            return super().render()
        return self._render_ready()

    def _render_ready(self) -> Text:
        issues = self._grouped()
        cursor = self._clamped_cursor(len(issues))
        lines: list[Text] = []
        current_status = ""
        for index, issue in enumerate(issues):
            if issue.status != current_status:
                current_status = issue.status
                header = Text()
                header.append(current_status, style="dim")
                lines.append(header)
            lines.append(self._styled_row(issue, index == cursor))
        return Text("\n").join(lines)

    def _styled_row(self, issue: Issue, selected: bool) -> Text:
        row = Text(style="bold") if selected else Text()
        row.append(f"{SELECTED_MARK} " if selected else "  ")
        changed = self.seen.is_changed(self.integration_id, issue)
        row.append(CHANGED_MARK if changed else " ", style=CHANGE_STYLE if changed else None)
        row.append(" ")
        row.append(issue.id, style="dim")
        row.append("  ")
        row.append(_priority_glyph(issue.priority))
        row.append(" ")
        row.append(issue.title)
        return row

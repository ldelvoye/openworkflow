"""Rendering markdown the way this app wants it, for any panel to reuse.

rich.markdown.Markdown's own inline-code and link styles assume a dark terminal and truecolor
rendering; Markdown here restyles both to stay legible on either theme using ANSI-named colors
only, and underlines a code span that names a real file or directory on disk, the same hint a
terminal gives before a cmd/ctrl-click.
"""

from __future__ import annotations

import functools
from pathlib import Path

from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import Markdown as _RichMarkdown
from rich.segment import Segment
from rich.style import Style
from rich.theme import Theme

_INLINE_CODE_STYLE = Style(bold=True, color="cyan")
_LOCAL_PATH_STYLE = _INLINE_CODE_STYLE + Style(underline=True)
_LINK_STYLE = Style(color="bright_blue", underline=True)
_MARKDOWN_THEME = Theme(
    {
        "markdown.code": _INLINE_CODE_STYLE,
        "markdown.link": _LINK_STYLE,
        "markdown.link_url": _LINK_STYLE,
    }
)

_MAX_LOCAL_PATH_LENGTH = 256


@functools.lru_cache(maxsize=256)
def is_local_path(text: str) -> bool:
    """Whether `text` names a file or directory that actually exists."""
    if not text or len(text) > _MAX_LOCAL_PATH_LENGTH or "\n" in text:
        return False
    for candidate in {text, text.rstrip("/")}:
        if candidate and Path(candidate).expanduser().exists():
            return True
    return False


def _underline_if_local_path(segment: Segment) -> Segment:
    if segment.style == _INLINE_CODE_STYLE and is_local_path(segment.text):
        return Segment(segment.text, _LOCAL_PATH_STYLE, segment.control)
    return segment


class Markdown(_RichMarkdown):
    """rich.markdown.Markdown with this app's code/link theme and real local paths underlined."""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        console.push_theme(_MARKDOWN_THEME)
        try:
            rendered = list(super().__rich_console__(console, options))
        finally:
            console.pop_theme()
        for item in rendered:
            if isinstance(item, Segment):
                yield _underline_if_local_path(item)
            else:
                yield item

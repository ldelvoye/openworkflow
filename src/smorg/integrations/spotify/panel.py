"""A Spotify-esque snapshot: what's playing now, what's queued next, and what played last."""

from __future__ import annotations

import webbrowser

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Input

from smorg.integrations.spotify.source import (
    FALLBACK_URL,
    LastPlayed,
    NowPlaying,
    PlayerState,
    Track,
)
from smorg.shell.format import age
from smorg.shell.panel import Panel

_DIM = "dim"

_PLAY_ICON = "▶"
_PAUSE_ICON = "⏸"
_PLAYING_STYLE = "green"
_PAUSED_STYLE = "yellow"

_QUEUE_DISPLAY_LIMIT = 10
# Aligns with the queue rows' title column: four number cells plus the two-cell gap.
_ROW_INDENT = "      "

_PLAY_NOW_PLACEHOLDER = "play now — search (not implemented yet)"
_ADD_TO_QUEUE_PLACEHOLDER = "add to queue — search (not implemented yet)"


def _format_artists(artists: tuple[str, ...]) -> str:
    """("Tame Impala", "Kevin Parker") -> "Tame Impala, Kevin Parker" """
    return ", ".join(artists)


def _format_track(track: Track) -> Text:
    """ "Bromeliad · Aaron Cherof, Minecraft" — the title carries the weight, the artists dim."""
    row = Text(track.track)
    row.append(f" · {_format_artists(track.artists)}", style=_DIM)
    return row


def _format_context(kind: str, name: str | None) -> str:
    if kind == "autoplay":
        return "autoplay"
    if name is None:
        return kind
    return f"{kind} · {name}"


def _format_banner(now_playing: NowPlaying | None) -> list[Text]:
    if now_playing is None:
        return [Text("nothing playing", style=_DIM)]
    if now_playing.is_playing:
        icon = _PLAY_ICON
        icon_style = _PLAYING_STYLE
    else:
        icon = _PAUSE_ICON
        icon_style = _PAUSED_STYLE
    banner = Text()
    banner.append(f"{icon} ", style=icon_style)
    banner.append(now_playing.track.track, style="bold")
    banner.append(f" · {_format_artists(now_playing.track.artists)}", style=_DIM)
    context_label = _format_context(now_playing.context_kind, now_playing.context_name)
    context = Text(f"  {context_label}", style=_DIM)
    return [banner, context]


def _format_queue(queue: tuple[Track, ...]) -> list[Text]:
    lines = [Text("  up next", style=_DIM)]
    if not queue:
        lines.append(Text(f"{_ROW_INDENT}queue is empty", style=_DIM))
        return lines
    for index, track in enumerate(queue[:_QUEUE_DISPLAY_LIMIT], start=1):
        row = Text()
        row.append(f"{index:>4}  ", style=_DIM)
        row.append_text(_format_track(track))
        lines.append(row)
    hidden = len(queue) - _QUEUE_DISPLAY_LIMIT
    if hidden > 0:
        lines.append(Text(f"{_ROW_INDENT}… {hidden} more", style=_DIM))
    return lines


def _format_last_played(last_played: LastPlayed | None) -> list[Text]:
    lines = [Text("  last played", style=_DIM)]
    if last_played is None:
        lines.append(Text(f"{_ROW_INDENT}nothing yet", style=_DIM))
        return lines
    row = Text(style=_DIM)
    row.append(_ROW_INDENT)
    row.append_text(_format_track(last_played.track))
    row.append(f"  {age(last_played.played_at)}")
    lines.append(row)
    return lines


class SpotifyPanel(Panel):
    DEFAULT_CSS = """
    SpotifyPanel > #player-search { dock: bottom; }
    """

    BINDINGS = [
        Binding("o", "open", "open in browser", show=False),
        Binding("p", "play_now", "play now", show=False),
        Binding("a", "add_to_queue", "add to queue", show=False),
    ]
    can_focus = True

    def compose(self) -> ComposeResult:
        yield from super().compose()
        search = Input(id="player-search")
        search.display = False
        yield search

    def _state(self) -> PlayerState | None:
        if len(self.items) != 1:
            return None
        item = self.items[0]
        if isinstance(item, PlayerState):
            return item
        return None

    def ready_text(self) -> str:
        return self.render_ready().plain.strip()

    def render_ready(self) -> Text:
        state = self._state()
        lines: list[Text] = []
        if state is None:
            lines.append(Text("nothing playing", style=_DIM))
        else:
            lines.extend(_format_banner(state.now_playing))
            lines.append(Text())
            lines.extend(_format_queue(state.queue))
            lines.append(Text())
            lines.extend(_format_last_played(state.last_played))
        body = Text("\n").join(lines)
        # One row per track: a wrapped row spills into the next row's place and breaks the layout.
        body.no_wrap = True
        body.overflow = "ellipsis"
        return body

    def action_open(self) -> None:
        state = self._state()
        if state is None:
            webbrowser.open(FALLBACK_URL)
        else:
            webbrowser.open(state.url)

    def action_play_now(self) -> None:
        self._open_search(_PLAY_NOW_PLACEHOLDER)

    def action_add_to_queue(self) -> None:
        self._open_search(_ADD_TO_QUEUE_PLACEHOLDER)

    def _open_search(self, placeholder: str) -> None:
        search = self.query_one("#player-search", Input)
        search.placeholder = placeholder
        search.value = ""
        search.display = True
        search.focus()

    def _close_search(self) -> None:
        search = self.query_one("#player-search", Input)
        search.display = False
        search.value = ""
        self.focus()

    def on_key(self, event: events.Key) -> None:
        # Only intercepted while the search strip actually holds focus, so escape still reaches
        # whatever else would otherwise handle it (the help overlay, most notably) the rest of
        # the time.
        if event.key != "escape":
            return
        if self.query_one("#player-search", Input).has_focus:
            event.stop()
            self._close_search()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.notify("not implemented yet — coming with write permissions")
        self._close_search()

"""Tests for the Spotify panel: one player-state snapshot, no cursor, no seen state."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input

from smorg.integrations.spotify.panel import SpotifyPanel
from smorg.integrations.spotify.source import (
    FALLBACK_URL,
    LastPlayed,
    NowPlaying,
    PlayerState,
    Track,
)
from smorg.shell.panel import PanelState

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def track(
    name: str = "Mr. Brightside",
    artists: tuple[str, ...] = ("The Killers",),
    album: str = "Hot Fuss",
) -> Track:
    slug = name.replace(" ", "-").replace("?", "")
    return Track(track=name, artists=artists, album=album, url=f"https://open.spotify.com/t/{slug}")


def now_playing(
    is_playing: bool = True,
    context_kind: str = "album",
    context_name: str | None = "Hot Fuss",
    playing_track: Track | None = None,
) -> NowPlaying:
    chosen_track = playing_track if playing_track is not None else track()
    return NowPlaying(
        track=chosen_track,
        is_playing=is_playing,
        context_kind=context_kind,
        context_name=context_name,
    )


def last_played(played_at: datetime = NOW) -> LastPlayed:
    return LastPlayed(
        track=track("Do I Wanna Know?", ("Arctic Monkeys",), "AM"), played_at=played_at
    )


def state(
    playing: NowPlaying | None = None,
    queue: tuple[Track, ...] = (),
    played: LastPlayed | None = None,
) -> PlayerState:
    if playing is not None:
        url = playing.track.url
    else:
        url = FALLBACK_URL
    return PlayerState(
        id="player", updated_at=NOW, url=url, now_playing=playing, queue=queue, last_played=played
    )


def panel_with(player_state: PlayerState) -> SpotifyPanel:
    panel = SpotifyPanel()
    panel.state = PanelState.READY
    panel.items = (player_state,)
    panel.integration_id = "spotify"
    return panel


def test_the_panel_never_fetches():
    """The seam the whole design rests on, enforced rather than trusted."""
    source = (Path("src") / "smorg" / "integrations" / "spotify" / "panel.py").read_text()
    assert "httpx" not in source
    assert "fetch" not in source


# --- The banner ---


def test_a_playing_track_gets_the_play_icon():
    text = panel_with(state(now_playing())).ready_text()

    assert "▶" in text
    assert "The Killers" in text
    assert "Mr. Brightside" in text


def test_a_paused_track_gets_the_pause_icon():
    text = panel_with(state(now_playing(is_playing=False))).ready_text()

    assert "⏸" in text
    assert "▶" not in text


def test_nothing_playing_says_so():
    text = panel_with(state()).ready_text()

    assert "nothing playing" in text


# --- The context label ---


def test_an_album_context_reads_kind_and_name():
    played = now_playing(context_kind="album", context_name="Hot Fuss")

    assert "album · Hot Fuss" in panel_with(state(played)).ready_text()


def test_an_autoplay_context_reads_as_autoplay_with_no_name():
    played = now_playing(context_kind="autoplay", context_name=None)

    assert "autoplay" in panel_with(state(played)).ready_text()


def test_a_context_with_no_resolved_name_falls_back_to_the_kind_alone():
    played = now_playing(context_kind="show", context_name=None)

    lines = [line.strip() for line in panel_with(state(played)).ready_text().splitlines()]

    assert "show" in lines


# --- The queue ---


def test_queue_rows_number_the_title_then_the_artists():
    queued = (track("Feel Good Inc.", ("Gorillaz", "De La Soul"), "Demon Days"),)

    text = panel_with(state(now_playing(), queue=queued)).ready_text()

    assert "1  Feel Good Inc. · Gorillaz, De La Soul" in text


def test_a_long_queue_is_capped_and_counts_the_rest():
    queued = tuple(track(f"Song {index}") for index in range(1, 21))

    text = panel_with(state(now_playing(), queue=queued)).ready_text()

    assert "Song 10" in text
    assert "Song 11" not in text
    assert "… 10 more" in text


def test_an_empty_queue_says_so():
    text = panel_with(state(now_playing())).ready_text()

    assert "queue is empty" in text


def test_rows_truncate_instead_of_wrapping():
    rendered = panel_with(state(now_playing())).render_ready()

    assert rendered.no_wrap is True
    assert rendered.overflow == "ellipsis"


def test_plain_output_is_derived_from_the_styled_render():
    """One row builder: the plain path must be the styled Text's own .plain."""
    panel = panel_with(state(now_playing()))

    assert panel.ready_text() == panel.render_ready().plain.strip()


# --- Last played ---


def test_last_played_row_shows_track_and_age(monkeypatch):
    moment = datetime(2026, 8, 20, 11, 55, tzinfo=UTC)
    monkeypatch.setattr("smorg.shell.format.now", lambda: moment + timedelta(minutes=5))
    played = last_played(played_at=moment)

    text = panel_with(state(now_playing(), played=played)).ready_text()

    assert "Do I Wanna Know? · Arctic Monkeys" in text
    assert "5m" in text


# --- Server text cannot restyle the panel ---


def test_a_track_that_looks_like_markup_is_drawn_literally():
    """Rich markup in a track name would otherwise let somebody else's
    listening history colour or hide rows in your dashboard."""
    hostile = now_playing(playing_track=track(name="[red]danger[/red]"))

    assert "[red]danger[/red]" in panel_with(state(hostile)).ready_text()


# --- The real bindings ---


class _SpotifyPanelHarness(App[None]):
    def __init__(self, panel: SpotifyPanel) -> None:
        super().__init__()
        self._panel = panel

    def compose(self) -> ComposeResult:
        yield self._panel


@pytest.mark.asyncio
async def test_pressing_o_opens_the_now_playing_url(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(
        "smorg.integrations.spotify.panel.webbrowser.open", lambda url: opened.append(url)
    )
    playing = now_playing()
    panel = panel_with(state(playing))
    async with _SpotifyPanelHarness(panel).run_test() as pilot:
        panel.focus()
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()

    assert opened == [playing.track.url]


@pytest.mark.asyncio
async def test_pressing_o_with_nothing_playing_opens_the_fallback(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(
        "smorg.integrations.spotify.panel.webbrowser.open", lambda url: opened.append(url)
    )
    panel = panel_with(state())
    async with _SpotifyPanelHarness(panel).run_test() as pilot:
        panel.focus()
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()

    assert opened == [FALLBACK_URL]


@pytest.mark.asyncio
async def test_pressing_p_opens_the_search_strip_with_the_play_now_placeholder():
    panel = panel_with(state(now_playing()))
    async with _SpotifyPanelHarness(panel).run_test() as pilot:
        panel.focus()
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()

        search = panel.query_one("#player-search", Input)
        assert search.display is True
        assert search.placeholder == "play now — search (not implemented yet)"
        assert search.has_focus


@pytest.mark.asyncio
async def test_pressing_a_opens_the_search_strip_with_the_add_to_queue_placeholder():
    panel = panel_with(state(now_playing()))
    async with _SpotifyPanelHarness(panel).run_test() as pilot:
        panel.focus()
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()

        search = panel.query_one("#player-search", Input)
        assert search.display is True
        assert search.placeholder == "add to queue — search (not implemented yet)"


@pytest.mark.asyncio
async def test_submitting_the_search_notifies_not_implemented_and_closes_it(monkeypatch):
    notified: list[str] = []
    monkeypatch.setattr(
        "smorg.integrations.spotify.panel.SpotifyPanel.notify",
        lambda self, message, **kwargs: notified.append(message),
    )
    panel = panel_with(state(now_playing()))
    async with _SpotifyPanelHarness(panel).run_test() as pilot:
        panel.focus()
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        search = panel.query_one("#player-search", Input)
        assert search.display is False

    assert notified == ["not implemented yet — coming with write permissions"]


@pytest.mark.asyncio
async def test_escape_closes_the_search_strip_and_returns_focus_to_the_panel():
    panel = panel_with(state(now_playing()))
    async with _SpotifyPanelHarness(panel).run_test() as pilot:
        panel.focus()
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        assert panel.query_one("#player-search", Input).display is True

        await pilot.press("escape")
        await pilot.pause()

        search = panel.query_one("#player-search", Input)
        assert search.display is False
        assert search.value == ""
        assert panel.has_focus


@pytest.mark.asyncio
async def test_escape_is_a_no_op_when_the_search_strip_is_not_showing():
    """Escape must not leak into a global dismiss when there is nothing local to dismiss —
    other handlers (the help overlay, most notably) still need it."""
    panel = panel_with(state(now_playing()))
    async with _SpotifyPanelHarness(panel).run_test() as pilot:
        panel.focus()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert panel.query_one("#player-search", Input).display is False

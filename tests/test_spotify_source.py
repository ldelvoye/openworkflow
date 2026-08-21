"""Tests for the Spotify source: one player-state snapshot built from three GETs (plus a
graceful fourth, only for a playlist context).

No network: httpx.MockTransport routes each request to a recorded response by path.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from smorg.auth.store import Credentials
from smorg.core.contract import AccessNotAllowed, AuthExpired, Malformed, Unavailable
from smorg.integrations.spotify.source import fetch

FIXTURES = Path(__file__).parent / "fixtures"
PLAYER = json.loads((FIXTURES / "spotify_player.json").read_text())
QUEUE = json.loads((FIXTURES / "spotify_queue.json").read_text())
RECENTLY_PLAYED = json.loads((FIXTURES / "spotify_recently_played.json").read_text())
EMPTY_QUEUE = {"queue": []}
EMPTY_RECENTLY_PLAYED = {"items": []}

CREDENTIALS = Credentials(
    access_token="spotify-secret-token",
    refresh_token=None,
    expires_at=None,
    scope="user-read-currently-playing",
)


class _Server:
    """Recorded answers keyed by path, and a log of every request served. Player, queue, and
    recently-played all default to "nothing there yet" so a test only sets up what it needs.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._player: tuple[int, object | None] = (204, None)
        self._queue: tuple[int, object] = (200, EMPTY_QUEUE)
        self._recently_played: tuple[int, object] = (200, EMPTY_RECENTLY_PLAYED)
        self._playlists: dict[str, tuple[int, object]] = {}

    def playing(self, payload: dict, status: int = 200) -> None:
        self._player = (status, payload)

    def nothing_playing(self) -> None:
        self._player = (204, None)

    def player_fails(self, status: int, payload: dict) -> None:
        self._player = (status, payload)

    def queued(self, payload: dict, status: int = 200) -> None:
        self._queue = (status, payload)

    def played(self, payload: dict, status: int = 200) -> None:
        self._recently_played = (status, payload)

    def playlist(self, playlist_id: str, payload: dict, status: int = 200) -> None:
        self._playlists[playlist_id] = (status, payload)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/me/player":
            status, payload = self._player
            if payload is None:
                return httpx.Response(status)
            return httpx.Response(status, json=payload)
        if path == "/v1/me/player/queue":
            status, payload = self._queue
            return httpx.Response(status, json=payload)
        if path == "/v1/me/player/recently-played":
            status, payload = self._recently_played
            return httpx.Response(status, json=payload)
        if path.startswith("/v1/playlists/"):
            playlist_id = path.rsplit("/", 1)[-1]
            if playlist_id not in self._playlists:
                raise AssertionError(f"no playlist response registered for {playlist_id}")
            status, payload = self._playlists[playlist_id]
            return httpx.Response(status, json=payload)
        raise AssertionError(f"unexpected request to {request.url}")


@pytest.fixture
def server() -> _Server:
    return _Server()


def fetch_with(server: _Server):
    http = httpx.Client(transport=httpx.MockTransport(server.handler))
    return fetch(CREDENTIALS, http)[0]


# --- Exactly one state, always ---


def test_fetch_returns_exactly_one_player_state(server):
    server.playing(PLAYER)
    result = fetch(CREDENTIALS, httpx.Client(transport=httpx.MockTransport(server.handler)))

    assert len(result) == 1


def test_nothing_playing_and_no_history_still_returns_one_idle_state(server):
    state = fetch_with(server)

    assert state.now_playing is None
    assert state.queue == ()
    assert state.last_played is None
    assert state.url == "https://open.spotify.com"


# --- What gets asked for ---


def test_a_full_snapshot_fetches_the_player_the_queue_and_recent_history(server):
    server.playing(PLAYER)
    server.queued(QUEUE)
    server.played(RECENTLY_PLAYED)

    fetch_with(server)

    paths = {request.url.path for request in server.requests}
    assert paths == {"/v1/me/player", "/v1/me/player/queue", "/v1/me/player/recently-played"}


# --- Now playing ---


def test_now_playing_carries_the_track_and_playback_state(server):
    server.playing(PLAYER)

    now_playing = fetch_with(server).now_playing

    assert now_playing is not None
    assert now_playing.track.track == "Mr. Brightside"
    assert now_playing.track.artists == ("The Killers",)
    assert now_playing.track.album == "Hot Fuss"
    assert now_playing.track.url == "https://open.spotify.com/track/3n3Ppam7vgaVa1iaRUc9Lp"
    assert now_playing.is_playing is True


def test_the_state_id_and_url_reflect_the_now_playing_track(server):
    server.playing(PLAYER)

    state = fetch_with(server)

    assert state.id == "player"
    assert state.url == "https://open.spotify.com/track/3n3Ppam7vgaVa1iaRUc9Lp"


def test_a_204_response_means_nothing_is_playing(server):
    server.nothing_playing()

    state = fetch_with(server)

    assert state.now_playing is None
    assert state.url == "https://open.spotify.com"


def test_a_missing_is_playing_flag_degrades_to_true(server):
    payload = dict(PLAYER)
    del payload["is_playing"]
    server.playing(payload)

    now_playing = fetch_with(server).now_playing

    assert now_playing is not None
    assert now_playing.is_playing is True


def test_a_null_item_is_treated_as_nothing_playing(server):
    server.playing(PLAYER | {"item": None})

    assert fetch_with(server).now_playing is None


def test_an_episode_is_treated_as_nothing_playing(server):
    server.playing(PLAYER | {"currently_playing_type": "episode"})

    assert fetch_with(server).now_playing is None


# --- Context: what's driving playback ---


def test_a_null_context_reads_as_autoplay(server):
    server.playing(PLAYER | {"context": None})

    now_playing = fetch_with(server).now_playing

    assert now_playing is not None
    assert now_playing.context_kind == "autoplay"
    assert now_playing.context_name is None


def test_an_album_context_names_itself_from_the_track_with_no_extra_call(server):
    server.playing(PLAYER)

    now_playing = fetch_with(server).now_playing

    assert now_playing is not None
    assert now_playing.context_kind == "album"
    assert now_playing.context_name == "Hot Fuss"
    assert not any(request.url.path.startswith("/v1/playlists/") for request in server.requests)


def test_an_artist_context_names_itself_from_the_track_s_first_artist(server):
    artist_context = {"type": "artist", "uri": "spotify:artist:0C0XlULifJtAgn6ZNCW2eu"}
    server.playing(PLAYER | {"context": artist_context})

    now_playing = fetch_with(server).now_playing

    assert now_playing is not None
    assert now_playing.context_kind == "artist"
    assert now_playing.context_name == "The Killers"


def test_an_unrecognised_context_type_is_kept_verbatim_with_no_name(server):
    show_context = {"type": "show", "uri": "spotify:show:abc123"}
    server.playing(PLAYER | {"context": show_context})

    now_playing = fetch_with(server).now_playing

    assert now_playing is not None
    assert now_playing.context_kind == "show"
    assert now_playing.context_name is None


def test_a_playlist_context_resolves_its_name_from_a_second_call(server):
    playlist_context = {"type": "playlist", "uri": "spotify:playlist:37i9dQZF1xyz"}
    server.playing(PLAYER | {"context": playlist_context})
    server.playlist("37i9dQZF1xyz", {"name": "Friday Favorites"})

    now_playing = fetch_with(server).now_playing

    assert now_playing is not None
    assert now_playing.context_kind == "playlist"
    assert now_playing.context_name == "Friday Favorites"
    playlist_request = next(
        request for request in server.requests if request.url.path == "/v1/playlists/37i9dQZF1xyz"
    )
    assert playlist_request.url.params["fields"] == "name"


def test_a_failing_playlist_name_lookup_degrades_to_no_name_not_an_error(server):
    """A missing playlist name must not break the whole tab."""
    playlist_context = {"type": "playlist", "uri": "spotify:playlist:missing"}
    server.playing(PLAYER | {"context": playlist_context})
    server.playlist("missing", {"error": "not found"}, status=404)

    now_playing = fetch_with(server).now_playing

    assert now_playing is not None
    assert now_playing.context_kind == "playlist"
    assert now_playing.context_name is None


# --- The queue ---


def test_the_queue_is_parsed_in_order(server):
    server.playing(PLAYER)
    server.queued(QUEUE)

    queue = fetch_with(server).queue

    assert [track.track for track in queue] == ["Feel Good Inc.", "Take On Me"]
    assert queue[0].artists == ("Gorillaz", "De La Soul")


# --- Last played ---


def test_last_played_carries_the_track_and_when_it_played(server):
    server.playing(PLAYER)
    server.played(RECENTLY_PLAYED)

    last_played = fetch_with(server).last_played

    assert last_played is not None
    assert last_played.track.track == "Do I Wanna Know?"
    assert last_played.track.artists == ("Arctic Monkeys",)
    assert last_played.played_at == datetime(2026, 8, 20, 11, 30, tzinfo=UTC)


# --- Sanitization and url validity (shared helpers, exercised through the now-playing path) ---


def test_a_track_title_carrying_terminal_escapes_is_sanitised(server):
    hostile_item = PLAYER["item"] | {"name": "Mr\x1b[31m. Bright\x00side"}
    server.playing(PLAYER | {"item": hostile_item})

    now_playing = fetch_with(server).now_playing

    assert now_playing is not None
    assert "\x1b" not in now_playing.track.track
    assert "\x00" not in now_playing.track.track


def test_a_non_https_track_url_is_malformed(server):
    plain_http = {"spotify": "http://open.spotify.com/track/x"}
    hostile_item = PLAYER["item"] | {"external_urls": plain_http}
    server.playing(PLAYER | {"item": hostile_item})

    with pytest.raises(Malformed):
        fetch_with(server)


# --- Failures cross the seam as one of the four, from any of the three main calls ---


def test_a_rejected_token_on_the_player_call_is_auth_expired(server):
    server.player_fails(401, {"error": {"status": 401, "message": "The access token expired"}})

    with pytest.raises(AuthExpired):
        fetch_with(server)


def test_an_unallowlisted_account_on_the_player_call_is_access_not_allowed(server):
    message = "User not registered in the Developer Dashboard"
    server.player_fails(403, {"error": {"status": 403, "message": message}})

    with pytest.raises(AccessNotAllowed):
        fetch_with(server)


def test_spotify_being_down_on_the_player_call_is_unavailable(server):
    server.player_fails(500, {"error": {"status": 500, "message": "down"}})

    with pytest.raises(Unavailable):
        fetch_with(server)


def test_a_network_failure_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(Unavailable):
        fetch(CREDENTIALS, httpx.Client(transport=httpx.MockTransport(handler)))


def test_a_body_that_is_not_json_is_malformed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with pytest.raises(Malformed):
        fetch(CREDENTIALS, httpx.Client(transport=httpx.MockTransport(handler)))


def test_a_failure_never_repeats_the_token(server):
    server.player_fails(401, {"error": {"status": 401}})

    with pytest.raises(AuthExpired) as raised:
        fetch_with(server)

    assert "spotify-secret-token" not in str(raised.value)

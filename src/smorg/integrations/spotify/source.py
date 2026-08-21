"""Fetch a snapshot of Spotify playback — now playing, the queue, and the last play — from the
REST API and map it to one typed player state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from smorg.auth.store import Credentials, now
from smorg.core.contract import (
    AccessNotAllowed,
    AuthExpired,
    IntegrationError,
    Item,
    Malformed,
    Unavailable,
)
from smorg.core.shape import required_string, timestamp
from smorg.core.text import sanitize_line

PLAYER_ENDPOINT = "https://api.spotify.com/v1/me/player"
QUEUE_ENDPOINT = "https://api.spotify.com/v1/me/player/queue"
RECENTLY_PLAYED_ENDPOINT = "https://api.spotify.com/v1/me/player/recently-played"
PLAYLISTS_ENDPOINT = "https://api.spotify.com/v1/playlists"

LAST_PLAYED_LIMIT = 1

# Where "o" opens when nothing is loaded on the player at all.
FALLBACK_URL = "https://open.spotify.com"


@dataclass(frozen=True)
class Track:
    track: str
    artists: tuple[str, ...]
    album: str
    url: str


@dataclass(frozen=True)
class NowPlaying:
    track: Track
    is_playing: bool
    # "album" | "playlist" | "artist" | "autoplay" | any other context type, verbatim.
    context_kind: str
    # None when there is nothing to name (autoplay, or a name that could not be resolved).
    context_name: str | None


@dataclass(frozen=True)
class LastPlayed:
    track: Track
    played_at: datetime


@dataclass(frozen=True)
class PlayerState(Item):
    """The whole tab's data in one snapshot — there is exactly one player, not a list of them."""

    now_playing: NowPlaying | None
    queue: tuple[Track, ...]
    last_played: LastPlayed | None


def fetch(credentials: Credentials, http: httpx.Client) -> tuple[PlayerState, ...]:
    """The player's current snapshot: what's playing, what's queued, and what played last."""
    now_playing = _fetch_now_playing(credentials, http)
    queue = _fetch_queue(credentials, http)
    last_played = _fetch_last_played(credentials, http)

    if now_playing is not None:
        url = now_playing.track.url
    else:
        url = FALLBACK_URL

    state = PlayerState(
        id="player",
        updated_at=now(),
        url=url,
        now_playing=now_playing,
        queue=queue,
        last_played=last_played,
    )
    return (state,)


def _get(
    credentials: Credentials, http: httpx.Client, url: str, params: dict[str, Any] | None = None
) -> httpx.Response:
    try:
        response = http.get(
            url,
            params=params or {},
            headers={"Authorization": f"Bearer {credentials.access_token}"},
        )
    except httpx.HTTPError as error:
        raise Unavailable("could not reach Spotify") from error
    if response.status_code == 401:
        raise AuthExpired("Spotify rejected the stored token; it may have expired or been revoked")
    if response.status_code == 403:
        # A dev-mode Spotify app only serves users the developer added to its allowlist in the
        # dashboard; a 403 here usually means the connected account isn't on it.
        raise AccessNotAllowed(
            "Spotify refused access; the connected account may need to be added to the app's "
            "allowlist at the Spotify dashboard"
        )
    return response


def _require_ok(response: httpx.Response) -> None:
    if response.status_code != 200:
        raise Unavailable(f"Spotify returned HTTP {response.status_code}")


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise Malformed("Spotify returned a body that is not JSON") from error
    if not isinstance(payload, dict):
        raise Malformed(f"Spotify returned {type(payload).__name__}, expected an object")
    return payload


def _fetch_now_playing(credentials: Credentials, http: httpx.Client) -> NowPlaying | None:
    response = _get(credentials, http, PLAYER_ENDPOINT)
    if response.status_code == 204:
        return None
    _require_ok(response)
    payload = _json_object(response)
    item = payload.get("item")
    if item is None or payload.get("currently_playing_type") != "track":
        return None
    if not isinstance(item, dict):
        raise Malformed(f"'item' was {type(item).__name__}, expected an object")
    track = _track_of(item)
    context_kind, context_name = _context_of(payload.get("context"), track, credentials, http)
    return NowPlaying(
        track=track,
        is_playing=_is_playing_of(payload),
        context_kind=context_kind,
        context_name=context_name,
    )


def _is_playing_of(payload: dict[str, Any]) -> bool:
    value = payload.get("is_playing")
    if isinstance(value, bool):
        return value
    # A missing or oddly-typed flag is informational, not load-bearing: degrade to "playing"
    # rather than breaking the whole tab over it.
    return True


def _context_of(
    context: object, track: Track, credentials: Credentials, http: httpx.Client
) -> tuple[str, str | None]:
    if context is None:
        return "autoplay", None
    if not isinstance(context, dict):
        raise Malformed(f"'context' was {type(context).__name__}, expected an object or null")
    kind = sanitize_line(required_string(context, "type"))
    if kind == "album":
        return kind, track.album
    if kind == "artist":
        if track.artists:
            name = track.artists[0]
        else:
            name = None
        return kind, name
    if kind == "playlist":
        return kind, _playlist_name_of(context.get("uri"), credentials, http)
    return kind, None


def _playlist_name_of(uri: object, credentials: Credentials, http: httpx.Client) -> str | None:
    """The playlist's own name, or None on any failure — a missing name must not break the tab."""
    if not isinstance(uri, str) or ":" not in uri:
        return None
    playlist_id = uri.rsplit(":", 1)[-1]
    try:
        response = _get(
            credentials, http, f"{PLAYLISTS_ENDPOINT}/{playlist_id}", params={"fields": "name"}
        )
        _require_ok(response)
        name = required_string(_json_object(response), "name")
    except IntegrationError:
        return None
    return sanitize_line(name)


def _fetch_queue(credentials: Credentials, http: httpx.Client) -> tuple[Track, ...]:
    response = _get(credentials, http, QUEUE_ENDPOINT)
    _require_ok(response)
    payload = _json_object(response)
    raw_queue = payload.get("queue")
    if not isinstance(raw_queue, list):
        raise Malformed("'queue' was missing or not a list")
    tracks: list[Track] = []
    for raw in raw_queue:
        if not isinstance(raw, dict):
            raise Malformed(f"a queued track was {type(raw).__name__}, expected an object")
        tracks.append(_track_of(raw))
    return tuple(tracks)


def _fetch_last_played(credentials: Credentials, http: httpx.Client) -> LastPlayed | None:
    response = _get(
        credentials, http, RECENTLY_PLAYED_ENDPOINT, params={"limit": LAST_PLAYED_LIMIT}
    )
    _require_ok(response)
    payload = _json_object(response)
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise Malformed("'items' was missing or not a list")
    if not raw_items:
        return None
    raw = raw_items[0]
    if not isinstance(raw, dict):
        raise Malformed(f"a play was {type(raw).__name__}, expected an object")
    raw_track = raw.get("track")
    if not isinstance(raw_track, dict):
        raise Malformed(f"'track' was {type(raw_track).__name__}, expected an object")
    return LastPlayed(track=_track_of(raw_track), played_at=timestamp(raw, "played_at"))


def _track_of(raw: dict[str, Any]) -> Track:
    return Track(
        track=sanitize_line(required_string(raw, "name")),
        artists=_artists_of(raw),
        album=sanitize_line(_album_name(raw)),
        url=_track_url(raw),
    )


def _track_url(track: dict[str, Any]) -> str:
    external_urls = track.get("external_urls")
    if not isinstance(external_urls, dict):
        raise Malformed(f"'external_urls' was {type(external_urls).__name__}, expected an object")
    url = required_string(external_urls, "spotify")
    if urlsplit(url).scheme != "https":
        raise Malformed("a track's Spotify url was not https")
    return url


def _artists_of(track: dict[str, Any]) -> tuple[str, ...]:
    artists = track.get("artists")
    if not isinstance(artists, list):
        raise Malformed(f"'artists' was {type(artists).__name__}, expected a list")
    names: list[str] = []
    for artist in artists:
        if not isinstance(artist, dict):
            raise Malformed(f"an artist was {type(artist).__name__}, expected an object")
        names.append(sanitize_line(required_string(artist, "name")))
    return tuple(names)


def _album_name(track: dict[str, Any]) -> str:
    album = track.get("album")
    if not isinstance(album, dict):
        raise Malformed(f"'album' was {type(album).__name__}, expected an object")
    return required_string(album, "name")

"""Spotify's declaration; connects with OAuth against an app the user creates themselves, and reads
the REST API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import httpx

from smorg.auth.oauth import OAuthMethod, ServerMetadata, StaticProvider
from smorg.auth.store import Credentials
from smorg.core.contract import Action, ActionClass, AuthPath, Manifest
from smorg.integrations.spotify.panel import SpotifyPanel
from smorg.integrations.spotify.source import PlayerState, fetch

METHOD = OAuthMethod(
    provider=StaticProvider(
        metadata=ServerMetadata(
            authorization_endpoint="https://accounts.spotify.com/authorize",
            token_endpoint="https://accounts.spotify.com/api/token",
        ),
        help_url="https://developer.spotify.com/dashboard",
        setup_hint='tick "Web API" when asked which API/SDKs the app will use',
    ),
    scopes=(
        "user-read-currently-playing",
        "user-read-playback-state",
        "user-read-recently-played",
    ),
)

MANIFEST = Manifest(
    id="spotify",
    display_name="Spotify",
    connections=(AuthPath(id="oauth", method=METHOD),),
    # A song is roughly three minutes; the banner must not go stale within one, or refocusing the
    # tab keeps showing a track that already ended.
    stale_after=timedelta(minutes=1),
    actions=(
        Action(id="open", label="Open in Spotify", key="o", action_class=ActionClass.LAUNCH),
        Action(id="play_now", label="Play now", key="p", action_class=ActionClass.REMOTE),
        Action(id="add_to_queue", label="Add to queue", key="a", action_class=ActionClass.REMOTE),
    ),
)


@dataclass(frozen=True)
class SpotifyIntegration:
    manifest: Manifest = MANIFEST
    panel_class: type[SpotifyPanel] = SpotifyPanel

    def fetch(self, credentials: Credentials, http: httpx.Client) -> tuple[PlayerState, ...]:
        return fetch(credentials, http)


INTEGRATION = SpotifyIntegration()

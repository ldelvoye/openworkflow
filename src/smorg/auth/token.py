"""Credentials a user pastes in, for a service that issues them by hand.

The counterpart to auth/oauth.py, and the second connection capability the
architecture left room for: no browser, no client registration, and nothing to
refresh. The token is whatever the user created in the service's own settings,
and stays valid until they revoke it or it expires — at which point the next
fetch fails as AuthExpired, which is what tells them to paste a new one.

Nothing here ever puts the entered value in an error message. A rejected token
is still a token, and the point of the store is that tokens do not leak.
"""

from __future__ import annotations

from dataclasses import dataclass

from smorg.auth.store import Credentials

__all__ = [
    "InvalidToken",
    "TokenPrompt",
    "accepted_token",
    "credentials_from_token",
]


@dataclass(frozen=True)
class TokenPrompt:
    """What to tell someone who has to go and create a token themselves.

    A connection path declaring one of these is asking for a single field of
    input; these three strings are what the CLI prints and what the in-app
    modal shows above it.
    """

    # Names the field, e.g. "GitHub personal access token".
    label: str
    # Where to create one.
    help_url: str
    # What the token has to be allowed to do, in the service's own words.
    scopes_hint: str


class InvalidToken(Exception):
    """The entered value cannot be a token. Never carries the value."""


def accepted_token(entered: str) -> str:
    """The entered token, with surrounding whitespace dropped.

    Rejects what a paste accident produces rather than storing it and leaving
    the service to reject it a refresh later: an empty entry, or a value
    carrying a space, a newline, or a control character, which no service
    issues.
    """
    token = entered.strip()
    if not token:
        raise InvalidToken("no token entered")
    if any(character.isspace() for character in token):
        raise InvalidToken("that token has whitespace inside it; paste it as one unbroken value")
    if not token.isprintable():
        raise InvalidToken("that token has characters no token carries; try pasting it again")
    return token


def credentials_from_token(token: str) -> Credentials:
    """A pasted token as stored credentials.

    No refresh token and no expiry: nothing here can renew what the service
    issued directly, and claiming an expiry we were never told would only make
    the refresh layer act on a guess.
    """
    return Credentials(access_token=token, refresh_token=None, expires_at=None, scope="")

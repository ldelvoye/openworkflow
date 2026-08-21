"""The refresh decision: hand fetching a token that is not about to expire.

Called from fetch worker threads. The lock is what makes two tabs' concurrent
refreshes safe: only one thread talks to the token endpoint; the others block,
re-read the store, and find fresh credentials already there.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import httpx

from smorg.auth import oauth
from smorg.auth.oauth import OAuthError, OAuthMethod
from smorg.auth.store import Credentials, get_credentials, now, set_credentials
from smorg.auth.token import TokenMethod
from smorg.core.contract import AuthExpired, AuthPath

# How close to expiry counts as expired: covers clock skew against the
# provider plus the gap between this check and the request using the token.
EXPIRY_MARGIN = timedelta(seconds=120)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(integration_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(integration_id, threading.Lock())


def _expiring(credentials: Credentials) -> bool:
    if credentials.expires_at is None:
        return False
    return now() >= credentials.expires_at - EXPIRY_MARGIN


def credentials_for(
    integration_id: str,
    path: AuthPath,
    client_id: str | None,
    http: httpx.Client,
) -> Credentials | None:
    """The credentials a fetch should use, renewed first where that is possible."""
    method = path.method
    if isinstance(method, TokenMethod):
        return get_credentials(integration_id)

    return fresh_credentials(integration_id, method, client_id, http)


def fresh_credentials(
    integration_id: str,
    method: OAuthMethod,
    client_id: str | None,
    http: httpx.Client,
) -> Credentials | None:
    """Stored credentials, refreshed and re-persisted when about to expire.

    None means not connected. Credentials that cannot be refreshed (no refresh
    token, no client id) are returned as-is: the server rejecting them produces
    the same AuthExpired the shell already handles.
    """
    credentials = get_credentials(integration_id)
    if credentials is None or not _expiring(credentials):
        return credentials
    if credentials.refresh_token is None or client_id is None:
        return credentials
    lock = _lock_for(integration_id)
    with lock:
        credentials = get_credentials(integration_id)
        if credentials is None or not _expiring(credentials):
            return credentials
        try:
            metadata = oauth.resolve_metadata(http, method)
            refreshed = oauth.refresh_credentials(http, metadata, client_id, credentials)
        except OAuthError as error:
            raise AuthExpired(f"token refresh failed ({error})") from error
        # A store failure here propagates as CredentialStoreError on purpose:
        # Linear rotates refresh tokens, so silently dropping the new one
        # would break every refresh after this session.
        set_credentials(integration_id, refreshed)
        return refreshed

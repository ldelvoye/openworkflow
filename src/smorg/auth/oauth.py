"""OAuth 2.1: discovery, dynamic client registration, PKCE, refresh, revocation.

An integration supplies an OAuthMethod and gets Credentials back.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from smorg.auth.store import Credentials, now

# A decoded JSON response body.
_JsonObject = dict[str, Any]

__all__ = [
    "REGISTERED_REDIRECT_URI",
    "REGISTRATION_PORT",
    "DiscoveredProvider",
    "OAuthError",
    "OAuthMethod",
    "ServerMetadata",
    "StaticProvider",
    "build_authorize_url",
    "callback_port",
    "discover",
    "exchange_code",
    "extra_scopes_warning",
    "make_pkce_pair",
    "refresh_credentials",
    "register_client",
    "resolve_metadata",
    "revoke",
]

REGISTRATION_PORT = 8765
REGISTERED_REDIRECT_URI = f"http://127.0.0.1:{REGISTRATION_PORT}/callback"


@dataclass(frozen=True)
class ServerMetadata:
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None = None
    revocation_endpoint: str | None = None
    resource: str | None = None


@dataclass(frozen=True)
class DiscoveredProvider:
    """Self-serve: endpoints come from the metadata document, and smorg registers itself as a
    public client under client_name."""

    metadata_url: str
    client_name: str


@dataclass(frozen=True)
class StaticProvider:
    """No discovery or registration: endpoints are declared here, and the user creates the OAuth
    app themselves (at help_url) and pastes its client id."""

    metadata: ServerMetadata
    help_url: str


@dataclass(frozen=True)
class OAuthMethod:
    """Authorize in the browser against provider, requesting scopes."""

    provider: DiscoveredProvider | StaticProvider
    scopes: tuple[str, ...]


class OAuthError(Exception):
    """A registration, token, or discovery request failed. Never carries a token."""


def _json_object(response: httpx.Response, source: str) -> _JsonObject:
    """Decode a response body that must be a JSON object."""
    try:
        payload = response.json()
    except ValueError as error:
        raise OAuthError(f"{source} returned a body that is not JSON") from error
    if not isinstance(payload, dict):
        raise OAuthError(f"{source} returned {type(payload).__name__}, expected a JSON object")
    return payload


def _require_https(url: str, name: str) -> str:
    """Refuse a plaintext endpoint named by a metadata document."""
    if urlsplit(url).scheme != "https":
        raise OAuthError(f"the {name} endpoint is not https: {url}")
    return url


def _require_https_if_present(url: str | None, name: str) -> str | None:
    return None if url is None else _require_https(url, name)


def discover(client: httpx.Client, provider: DiscoveredProvider) -> ServerMetadata:
    try:
        response = client.get(provider.metadata_url)
    except httpx.HTTPError as error:
        raise OAuthError(f"could not reach {provider.metadata_url}") from error
    if response.status_code != 200:
        raise OAuthError(f"metadata discovery failed with {response.status_code}")
    payload = _json_object(response, "the metadata endpoint")
    try:
        return ServerMetadata(
            authorization_endpoint=_require_https(payload["authorization_endpoint"], "authorize"),
            token_endpoint=_require_https(payload["token_endpoint"], "token"),
            registration_endpoint=_require_https(payload["registration_endpoint"], "registration"),
            revocation_endpoint=_require_https_if_present(
                payload.get("revocation_endpoint"), "revocation"
            ),
            resource=payload.get("resource"),
        )
    except KeyError as error:
        raise OAuthError(f"metadata document is missing {error}") from error


def resolve_metadata(client: httpx.Client, method: OAuthMethod) -> ServerMetadata:
    if isinstance(method.provider, StaticProvider):
        return method.provider.metadata
    return discover(client, method.provider)


def register_client(
    client: httpx.Client,
    metadata: ServerMetadata,
    provider: DiscoveredProvider,
    redirect_uri: str,
) -> str:
    if metadata.registration_endpoint is None:
        raise OAuthError("the provider names no registration endpoint")
    try:
        response = client.post(
            metadata.registration_endpoint,
            json={
                "client_name": provider.client_name,
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
    except httpx.HTTPError as error:
        raise OAuthError("could not reach the registration endpoint") from error
    if response.status_code not in (200, 201):
        raise OAuthError(f"client registration failed with {response.status_code}")
    try:
        return _json_object(response, "the registration endpoint")["client_id"]
    except KeyError as error:
        raise OAuthError("registration response contained no client_id") from error


def callback_port(method: OAuthMethod) -> int:
    # A hand-registered app pins its redirect URI, so a static provider binds the registered
    # port exactly; discovered providers accept any loopback port (RFC 8252 §7.3).
    if isinstance(method.provider, StaticProvider):
        return REGISTRATION_PORT
    return 0


def make_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def build_authorize_url(
    metadata: ServerMetadata,
    client_id: str,
    redirect_uri: str,
    challenge: str,
    scopes: tuple[str, ...],
    state: str,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if metadata.resource:
        params["resource"] = metadata.resource
    return f"{metadata.authorization_endpoint}?{urlencode(params)}"


def _credentials_from_token_response(
    payload: _JsonObject, fallback_refresh: str | None
) -> Credentials:
    expires_in = payload.get("expires_in")
    # RFC 6749 says a number; some providers send it as a decimal string. Accept
    # both, but say which field is wrong rather than blaming the access token.
    if isinstance(expires_in, str):
        try:
            expires_in = int(expires_in)
        except ValueError as error:
            raise OAuthError(
                f"token response gave a non-numeric expires_in: {expires_in!r}"
            ) from error
    received_refresh_token = payload.get("refresh_token")
    if received_refresh_token:
        refresh_token = received_refresh_token
    else:
        # The refresh token we already hold stays valid
        refresh_token = fallback_refresh

    if expires_in is None:
        # expires_in of 0 means the token is already dead; None means the server said nothing about
        # expiry at all.
        expires_at = None
    else:
        expires_at = now() + timedelta(seconds=expires_in)

    try:
        return Credentials(
            access_token=payload["access_token"],
            refresh_token=refresh_token,
            expires_at=expires_at,
            scope=payload.get("scope", ""),
        )
    except (KeyError, TypeError) as error:
        raise OAuthError("token response contained no usable access_token") from error


def _post_token(
    client: httpx.Client, metadata: ServerMetadata, form: dict[str, str]
) -> _JsonObject:
    # Binds the issued token to the protected resource; omit it and the token
    # carries the wrong audience — rejected later at the API, not here.
    if metadata.resource:
        form = form | {"resource": metadata.resource}
    try:
        response = client.post(metadata.token_endpoint, data=form)
    except httpx.HTTPError as error:
        raise OAuthError("could not reach the token endpoint") from error
    if response.status_code != 200:
        try:
            reason = response.json().get("error", "unknown_error")
        except (ValueError, AttributeError):
            reason = "unparseable error response"
        raise OAuthError(f"token request failed with {response.status_code}: {reason}")
    return _json_object(response, "the token endpoint")


def exchange_code(
    client: httpx.Client,
    metadata: ServerMetadata,
    client_id: str,
    code: str,
    verifier: str,
    redirect_uri: str,
) -> Credentials:
    payload = _post_token(
        client,
        metadata,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    return _credentials_from_token_response(payload, fallback_refresh=None)


def refresh_credentials(
    client: httpx.Client,
    metadata: ServerMetadata,
    client_id: str,
    credentials: Credentials,
) -> Credentials:
    if credentials.refresh_token is None:
        raise OAuthError("no refresh token available; re-run smorg connect")
    payload = _post_token(
        client,
        metadata,
        {
            "grant_type": "refresh_token",
            "refresh_token": credentials.refresh_token,
            "client_id": client_id,
        },
    )
    return _credentials_from_token_response(payload, fallback_refresh=credentials.refresh_token)


def extra_scopes_warning(
    integration_id: str, display_name: str, method: OAuthMethod, credentials: Credentials
) -> str | None:
    """None when the provider granted nothing beyond what was requested."""
    granted = set(credentials.scope.split())
    requested = set(method.scopes)
    extra = sorted(granted - requested)
    if not extra:
        return None
    return (
        f"{display_name} granted scopes smorg did not ask for: {', '.join(extra)}. "
        f"Nothing here uses them, but the stored token can. "
        f"Run 'smorg logout {integration_id}' to revoke it."
    )


def revoke(
    client: httpx.Client,
    metadata: ServerMetadata,
    client_id: str,
    credentials: Credentials,
) -> bool:
    """Ask the server to invalidate the refresh token (RFC 7009). Doesn't raise because local token
    is always deleted.
    """
    if metadata.revocation_endpoint is None:
        return False
    if credentials.refresh_token:
        token = credentials.refresh_token
    else:
        token = credentials.access_token
    try:
        response = client.post(
            metadata.revocation_endpoint,
            data={
                "token": token,
                "token_type_hint": "refresh_token" if credentials.refresh_token else "access_token",
                "client_id": client_id,
            },
        )
    except httpx.HTTPError:
        return False
    return response.status_code == 200

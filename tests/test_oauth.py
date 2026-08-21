import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from smorg.auth.oauth import (
    REGISTRATION_PORT,
    DiscoveredProvider,
    OAuthError,
    OAuthMethod,
    ServerMetadata,
    StaticProvider,
    build_authorize_url,
    callback_port,
    discover,
    exchange_code,
    extra_scopes_warning,
    make_pkce_pair,
    refresh_credentials,
    register_client,
    resolve_metadata,
    revoke,
)
from smorg.auth.store import Credentials

METADATA = json.loads((Path(__file__).parent / "fixtures" / "oauth_metadata.json").read_text())
RESOURCE = "https://mcp.linear.app/mcp"
REDIRECT = "http://127.0.0.1:8765/callback"
DISCOVERED = DiscoveredProvider(
    metadata_url="https://mcp.linear.app/.well-known/oauth-authorization-server",
    client_name="smorg",
)
METHOD = OAuthMethod(provider=DISCOVERED, scopes=("read",))


def client_returning(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def metadata_client(request):
    return httpx.Response(200, json=METADATA)


@pytest.fixture
def metadata():
    return discover(client_returning(metadata_client), DISCOVERED)


def test_discover_reads_endpoints(metadata):
    assert metadata.authorization_endpoint == "https://mcp.linear.app/authorize"
    assert metadata.token_endpoint == "https://mcp.linear.app/token"
    assert metadata.registration_endpoint == "https://mcp.linear.app/register"
    assert metadata.revocation_endpoint == "https://mcp.linear.app/token"
    assert metadata.resource == RESOURCE


def test_discover_raises_on_error_response():
    def handler(request):
        return httpx.Response(404)

    with pytest.raises(OAuthError):
        discover(client_returning(handler), DISCOVERED)


def test_discover_raises_when_a_200_is_not_json():
    def handler(request):
        return httpx.Response(200, content=b"<html>captive portal</html>")

    with pytest.raises(OAuthError, match="not JSON"):
        discover(client_returning(handler), DISCOVERED)


def test_discover_raises_when_a_200_is_not_an_object():
    def handler(request):
        return httpx.Response(200, json=["not", "an", "object"])

    with pytest.raises(OAuthError, match="expected a JSON object"):
        discover(client_returning(handler), DISCOVERED)


def test_register_client_raises_when_a_200_is_not_json(metadata):
    def handler(request):
        return httpx.Response(201, content=b"<html>proxy</html>")

    with pytest.raises(OAuthError, match="not JSON"):
        register_client(client_returning(handler), metadata, DISCOVERED, REDIRECT)


def test_exchange_code_raises_when_a_200_is_not_json(metadata):
    def handler(request):
        return httpx.Response(200, content=b"<html>proxy</html>")

    with pytest.raises(OAuthError, match="not JSON"):
        exchange_code(
            client_returning(handler), metadata, "client-abc", "code-1", "verifier-1", REDIRECT
        )


def test_expires_in_zero_is_an_expiry_not_an_absence(metadata):
    def handler(request):
        return httpx.Response(200, json={"access_token": "at-1", "expires_in": 0, "scope": "read"})

    credentials = exchange_code(
        client_returning(handler), metadata, "client-abc", "code-1", "verifier-1", REDIRECT
    )

    assert credentials.expires_at is not None


def test_discover_rejects_a_plaintext_endpoint():
    def handler(request):
        return httpx.Response(
            200, json=METADATA | {"token_endpoint": "http://mcp.linear.app/token"}
        )

    with pytest.raises(OAuthError, match="not https"):
        discover(client_returning(handler), DISCOVERED)


def test_a_string_expires_in_is_accepted(metadata):
    def handler(request):
        return httpx.Response(
            200, json={"access_token": "at-1", "expires_in": "3600", "scope": "read"}
        )

    credentials = exchange_code(
        client_returning(handler), metadata, "client-abc", "code-1", "verifier-1", REDIRECT
    )
    assert credentials.expires_at is not None


def test_a_non_numeric_expires_in_names_that_field(metadata):
    def handler(request):
        return httpx.Response(
            200, json={"access_token": "at-1", "expires_in": "soon", "scope": "read"}
        )

    with pytest.raises(OAuthError, match="expires_in"):
        exchange_code(
            client_returning(handler), metadata, "client-abc", "code-1", "verifier-1", REDIRECT
        )


def test_pkce_challenge_is_s256_of_verifier():
    verifier, challenge = make_pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert challenge == expected.rstrip(b"=").decode()
    assert "=" not in challenge


def test_pkce_pair_is_unique_per_call():
    assert make_pkce_pair()[0] != make_pkce_pair()[0]


def test_register_client_posts_a_public_client(metadata):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"client_id": "client-abc"})

    client_id = register_client(client_returning(handler), metadata, DISCOVERED, REDIRECT)

    assert client_id == "client-abc"
    assert seen["body"]["token_endpoint_auth_method"] == "none"
    assert seen["body"]["redirect_uris"] == [REDIRECT]
    assert "client_secret" not in seen["body"]


def test_register_client_raises_on_error_response(metadata):
    def handler(request):
        return httpx.Response(400, json={"error": "invalid_redirect_uri"})

    with pytest.raises(OAuthError):
        register_client(client_returning(handler), metadata, DISCOVERED, REDIRECT)


def test_authorize_url_carries_pkce_state_and_resource(metadata):
    url = build_authorize_url(metadata, "client-abc", REDIRECT, "chal", ("read",), "state-xyz")
    query = parse_qs(urlparse(url).query)

    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == ["chal"]
    assert query["state"] == ["state-xyz"]
    assert query["scope"] == ["read"]
    assert query["resource"] == [RESOURCE]


def test_exchange_code_returns_credentials_with_expiry(metadata):
    def handler(request):
        body = parse_qs(request.content.decode())
        assert body["grant_type"] == ["authorization_code"]
        assert body["code_verifier"] == ["verifier-1"]
        assert body["resource"] == [RESOURCE]
        return httpx.Response(
            200,
            json={
                "access_token": "at-1",
                "refresh_token": "rt-1",
                "expires_in": 3600,
                "scope": "read",
            },
        )

    credentials = exchange_code(
        client_returning(handler), metadata, "client-abc", "code-1", "verifier-1", REDIRECT
    )

    assert credentials.access_token == "at-1"
    assert credentials.refresh_token == "rt-1"
    assert credentials.scope == "read"
    assert credentials.expires_at is not None


def test_exchange_code_raises_on_error_response(metadata):
    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(OAuthError, match="invalid_grant"):
        exchange_code(
            client_returning(handler), metadata, "client-abc", "bad", "verifier-1", REDIRECT
        )


def test_refresh_keeps_the_old_refresh_token_when_none_returned(metadata):
    def handler(request):
        return httpx.Response(
            200, json={"access_token": "at-2", "expires_in": 3600, "scope": "read"}
        )

    old = Credentials(access_token="at-1", refresh_token="rt-1", expires_at=None, scope="read")
    refreshed = refresh_credentials(client_returning(handler), metadata, "client-abc", old)

    assert refreshed.access_token == "at-2"
    assert refreshed.refresh_token == "rt-1"


def test_refresh_without_a_refresh_token_raises(metadata):
    def handler(request):
        raise AssertionError("must not reach the network")

    old = Credentials(access_token="at-1", refresh_token=None, expires_at=None, scope="read")
    with pytest.raises(OAuthError, match="smorg connect"):
        refresh_credentials(client_returning(handler), metadata, "client-abc", old)


def test_oauth_errors_never_contain_a_token(metadata):
    def handler(request):
        return httpx.Response(401, json={"error": "invalid_client"})

    old = Credentials(
        access_token="at-secret", refresh_token="rt-secret", expires_at=None, scope="read"
    )
    with pytest.raises(OAuthError) as excinfo:
        refresh_credentials(client_returning(handler), metadata, "client-abc", old)

    assert "at-secret" not in str(excinfo.value)
    assert "rt-secret" not in str(excinfo.value)


def test_revoke_posts_the_refresh_token(metadata):
    seen = {}

    def handler(request):
        seen["body"] = parse_qs(request.content.decode())
        return httpx.Response(200)

    credentials = Credentials(
        access_token="at-1", refresh_token="rt-1", expires_at=None, scope="read"
    )
    assert revoke(client_returning(handler), metadata, "client-abc", credentials) is True
    assert seen["body"]["token"] == ["rt-1"]
    assert seen["body"]["token_type_hint"] == ["refresh_token"]


def test_revoke_reports_failure_instead_of_raising(metadata):
    def handler(request):
        raise httpx.ConnectError("offline")

    credentials = Credentials(
        access_token="at-1", refresh_token="rt-1", expires_at=None, scope="read"
    )
    assert revoke(client_returning(handler), metadata, "client-abc", credentials) is False


# --- extra_scopes_warning: one wording shared by the CLI print and the TUI toast ---


def test_extra_scopes_warning_names_the_scopes_beyond_what_was_requested():
    credentials = Credentials(
        access_token="at", refresh_token="rt", expires_at=None, scope="read write"
    )

    warning = extra_scopes_warning("linear", "Linear", METHOD, credentials)

    assert warning is not None
    assert "write" in warning
    assert "did not ask for" in warning
    assert "smorg logout linear" in warning


def test_extra_scopes_warning_is_none_when_granted_matches_requested():
    credentials = Credentials(access_token="at", refresh_token="rt", expires_at=None, scope="read")

    assert extra_scopes_warning("linear", "Linear", METHOD, credentials) is None


STATIC_METADATA = ServerMetadata(
    authorization_endpoint="https://accounts.example.invalid/authorize",
    token_endpoint="https://accounts.example.invalid/api/token",
)
STATIC = OAuthMethod(
    provider=StaticProvider(
        metadata=STATIC_METADATA,
        help_url="https://developer.example.invalid/dashboard",
    ),
    scopes=("read",),
)


def no_network(request):
    raise AssertionError(f"unexpected request to {request.url}")


def test_resolve_metadata_uses_declared_endpoints_without_a_request():
    metadata = resolve_metadata(client_returning(no_network), STATIC)

    assert metadata is STATIC_METADATA


def test_resolve_metadata_discovers_for_a_discovered_provider():
    metadata = resolve_metadata(client_returning(metadata_client), METHOD)

    assert metadata.authorization_endpoint == METADATA["authorization_endpoint"]


def test_register_client_refuses_metadata_without_a_registration_endpoint():
    with pytest.raises(OAuthError):
        register_client(client_returning(no_network), STATIC_METADATA, DISCOVERED, REDIRECT)


def test_the_callback_port_is_pinned_only_for_a_static_provider():
    assert callback_port(STATIC) == REGISTRATION_PORT
    assert callback_port(METHOD) == 0

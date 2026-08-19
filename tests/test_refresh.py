import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from smorg.auth.oauth import ProviderConfig
from smorg.auth.refresh import EXPIRY_MARGIN, credentials_for, fresh_credentials
from smorg.auth.store import Credentials, get_credentials, set_credentials
from smorg.auth.token import TokenPrompt
from smorg.core.contract import AuthExpired, ConnectionPath

PROVIDER = ProviderConfig(
    metadata_url="https://auth.invalid/.well-known/oauth-authorization-server",
    scopes=("read",),
    client_name="smorg",
)
METADATA = {
    "authorization_endpoint": "https://auth.invalid/authorize",
    "token_endpoint": "https://auth.invalid/token",
    "registration_endpoint": "https://auth.invalid/register",
}
NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SMORG_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("SMORG_CREDENTIAL_STORE", "file")


def credentials(
    expires_in: timedelta | None, refresh_token: str | None = "refresh-1"
) -> Credentials:
    if expires_in is not None:
        expires_at = NOW + expires_in
    else:
        expires_at = None
    return Credentials(
        access_token="access-old",
        refresh_token=refresh_token,
        expires_at=expires_at,
        scope="read",
    )


def refresh_server(hits: list[str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        if request.url.path.startswith("/.well-known"):
            return httpx.Response(200, json=METADATA)
        return httpx.Response(
            200, json={"access_token": "access-new", "expires_in": 3600, "scope": "read"}
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_untouched_when_not_near_expiry():
    set_credentials("linear", credentials(timedelta(hours=2)))
    hits: list[str] = []
    result = fresh_credentials("linear", PROVIDER, "client-1", refresh_server(hits))
    assert result is not None and result.access_token == "access-old"
    assert hits == []


def test_untouched_when_expiry_is_unknown():
    set_credentials("linear", credentials(None))
    hits: list[str] = []
    result = fresh_credentials("linear", PROVIDER, "client-1", refresh_server(hits))
    assert result is not None and result.access_token == "access-old"
    assert hits == []


def test_none_stays_none():
    assert fresh_credentials("linear", PROVIDER, "client-1", refresh_server([])) is None


def test_a_token_inside_the_margin_is_refreshed_and_persisted():
    set_credentials("linear", credentials(EXPIRY_MARGIN - timedelta(seconds=60)))
    result = fresh_credentials("linear", PROVIDER, "client-1", refresh_server([]))
    assert result is not None and result.access_token == "access-new"
    stored = get_credentials("linear")
    assert stored is not None and stored.access_token == "access-new"


def test_an_already_expired_token_is_refreshed():
    set_credentials("linear", credentials(timedelta(seconds=-10)))
    result = fresh_credentials("linear", PROVIDER, "client-1", refresh_server([]))
    assert result is not None and result.access_token == "access-new"


def test_a_refresh_response_without_a_refresh_token_keeps_the_old_one():
    set_credentials("linear", credentials(timedelta(seconds=-10)))
    result = fresh_credentials("linear", PROVIDER, "client-1", refresh_server([]))
    assert result is not None and result.refresh_token == "refresh-1"


def test_no_refresh_token_returns_the_stored_credentials_untouched():
    set_credentials("linear", credentials(timedelta(seconds=-10), refresh_token=None))
    hits: list[str] = []
    result = fresh_credentials("linear", PROVIDER, "client-1", refresh_server(hits))
    assert result is not None and result.access_token == "access-old"
    assert hits == []


def test_no_client_id_returns_the_stored_credentials_untouched():
    set_credentials("linear", credentials(timedelta(seconds=-10)))
    hits: list[str] = []
    result = fresh_credentials("linear", PROVIDER, None, refresh_server(hits))
    assert result is not None and result.access_token == "access-old"
    assert hits == []


def test_a_failed_refresh_is_auth_expired_and_never_leaks_the_token():
    set_credentials("linear", credentials(timedelta(seconds=-10)))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/.well-known"):
            return httpx.Response(200, json=METADATA)
        return httpx.Response(400, json={"error": "invalid_grant"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(AuthExpired) as excinfo:
        fresh_credentials("linear", PROVIDER, "client-1", http)
    assert "refresh" in str(excinfo.value)
    assert "refresh-1" not in str(excinfo.value)
    assert "access-old" not in str(excinfo.value)


def test_concurrent_refreshes_hit_the_token_endpoint_once():
    """The lock plus the re-read after acquiring it: the second thread must
    find the already-refreshed credentials instead of refreshing again."""
    set_credentials("linear", credentials(timedelta(seconds=-10)))
    token_hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/.well-known"):
            return httpx.Response(200, json=METADATA)
        token_hits.append("token")
        return httpx.Response(
            200, json={"access_token": "access-new", "expires_in": 3600, "scope": "read"}
        )

    def run() -> None:
        http = httpx.Client(transport=httpx.MockTransport(handler))
        fresh_credentials("linear", PROVIDER, "client-1", http)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert token_hits == ["token"]


def test_the_refresh_request_is_a_refresh_grant_for_the_stored_token():
    set_credentials("linear", credentials(timedelta(seconds=-10)))
    forms: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/.well-known"):
            return httpx.Response(200, json=METADATA)
        forms.append(dict(pair.split("=", 1) for pair in request.content.decode().split("&")))
        return httpx.Response(
            200, json={"access_token": "access-new", "expires_in": 3600, "scope": "read"}
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    fresh_credentials("linear", PROVIDER, "client-1", http)
    assert forms[0]["grant_type"] == "refresh_token"
    assert forms[0]["refresh_token"] == "refresh-1"
    assert forms[0]["client_id"] == "client-1"


# --- A token path has nothing behind it to renew ---

TOKEN_PATH = ConnectionPath(
    id="token",
    method=TokenPrompt(
        label="API token", help_url="https://example.invalid/tokens", scopes_hint="read"
    ),
)
OAUTH_PATH = ConnectionPath(id="oauth", method=PROVIDER)


def test_a_token_path_is_handed_its_stored_credentials_untouched():
    """Even sitting inside the expiry margin: nothing here issued this token,
    so there is no endpoint that would renew it, and the service rejecting it
    is what reports it expired."""
    expiring = credentials(EXPIRY_MARGIN / 2)
    set_credentials("github", expiring)
    hits: list[str] = []

    resolved = credentials_for("github", TOKEN_PATH, None, refresh_server(hits))

    assert resolved == expiring
    assert hits == []


def test_an_oauth_path_still_refreshes_through_the_same_resolver():
    set_credentials("linear", credentials(EXPIRY_MARGIN / 2))
    hits: list[str] = []

    resolved = credentials_for("linear", OAUTH_PATH, "client-1", refresh_server(hits))

    assert resolved is not None
    assert resolved.access_token == "access-new"
    assert hits != []

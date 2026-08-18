"""Tests for the browser login loop.

A real loopback HTTPServer on an ephemeral port, driven by a stubbed browser
that issues genuine HTTP requests to it. Only the OAuth endpoints are faked, so
the callback handling under test is the real thing.
"""

import json
import threading
import urllib.parse
from pathlib import Path

import httpx
import pytest

from smorg.auth import oauth
from smorg.cli import run_login

METADATA = json.loads((Path(__file__).parent / "fixtures" / "oauth_metadata.json").read_text())
PROVIDER = oauth.ProviderConfig(
    metadata_url="https://mcp.linear.app/.well-known/oauth-authorization-server",
    scopes=("read",),
    client_name="smorg",
)
TOKEN = {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600, "scope": "read"}


def oauth_client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("oauth-authorization-server"):
            return httpx.Response(200, json=METADATA)
        if path == "/register":
            return httpx.Response(201, json={"client_id": "client-abc"})
        if path == "/token":
            return httpx.Response(200, json=TOKEN)
        raise AssertionError(f"unexpected request to {request.url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def browser_sending(monkeypatch, *paths: str) -> None:
    """Stub the browser so it issues the given callback requests, in order.

    Delivered from a thread because run_login only starts serving after open()
    returns — a synchronous request would deadlock against a socket nobody is
    reading yet. `{state}` in a path is filled with the real state parameter.
    """

    def fake_open(url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        redirect = urllib.parse.urlparse(query["redirect_uri"][0])
        base = f"{redirect.scheme}://{redirect.netloc}"
        state = query["state"][0]

        def deliver() -> None:
            for path in paths:
                try:
                    httpx.get(base + path.format(state=state), timeout=5)
                except httpx.HTTPError:
                    pass

        threading.Thread(target=deliver, daemon=True).start()
        return True

    monkeypatch.setattr("smorg.cli.webbrowser.open", fake_open)


def test_login_returns_the_client_id_and_credentials(monkeypatch):
    browser_sending(monkeypatch, "/callback?code=code-1&state={state}")

    client_id, credentials = run_login(oauth_client(), PROVIDER, None, port=0, timeout=10)

    assert client_id == "client-abc"
    assert credentials.access_token == "at-1"
    assert credentials.scope == "read"


def test_stray_requests_do_not_consume_the_login(monkeypatch):
    browser_sending(
        monkeypatch,
        "/favicon.ico",
        "/",
        "/callback?code=code-1&state=not-our-state",
        "/callback?code=code-1&state={state}",
    )

    _, credentials = run_login(oauth_client(), PROVIDER, None, port=0, timeout=10)

    assert credentials.access_token == "at-1"


def test_registration_is_stable_while_the_callback_port_is_not(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("oauth-authorization-server"):
            return httpx.Response(200, json=METADATA)
        if path == "/register":
            seen["registered"] = json.loads(request.content)["redirect_uris"][0]
            return httpx.Response(201, json={"client_id": "client-abc"})
        seen["exchanged"] = dict(urllib.parse.parse_qsl(request.content.decode()))["redirect_uri"]
        return httpx.Response(200, json=TOKEN)

    browser_sending(monkeypatch, "/callback?code=code-1&state={state}")
    client = httpx.Client(transport=httpx.MockTransport(handler))

    run_login(client, PROVIDER, None, port=0, timeout=10)

    assert seen["registered"] == oauth.REGISTERED_REDIRECT_URI
    assert seen["exchanged"] != seen["registered"]


def test_a_non_ascii_state_is_rejected_without_disturbing_the_login(monkeypatch):
    browser_sending(
        monkeypatch,
        "/callback?code=code-1&state=%C3%A9tat",
        "/callback?code=code-1&state={state}",
    )

    _, credentials = run_login(oauth_client(), PROVIDER, None, port=0, timeout=10)

    assert credentials.access_token == "at-1"


def test_a_forged_error_cannot_abort_a_pending_login(monkeypatch):
    browser_sending(monkeypatch, "/callback?error=access_denied&state=forged")

    with pytest.raises(oauth.OAuthError, match="timed out"):
        run_login(oauth_client(), PROVIDER, None, port=0, timeout=2)


def test_a_genuine_refusal_ends_the_login(monkeypatch):
    browser_sending(monkeypatch, "/callback?error=access_denied&state={state}")

    with pytest.raises(oauth.OAuthError, match="access_denied"):
        run_login(oauth_client(), PROVIDER, None, port=0, timeout=10)


def test_error_text_cannot_carry_terminal_escapes(monkeypatch):
    browser_sending(monkeypatch, "/callback?error=denied%1b%5b31m&state={state}")

    with pytest.raises(oauth.OAuthError) as excinfo:
        run_login(oauth_client(), PROVIDER, None, port=0, timeout=10)

    assert "\x1b" not in str(excinfo.value)
    assert "denied" in str(excinfo.value)


def test_a_registered_client_is_reused_rather_than_registered_again(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/register":
            raise AssertionError("must not register when a client id is already known")
        if request.url.path.endswith("oauth-authorization-server"):
            return httpx.Response(200, json=METADATA)
        return httpx.Response(200, json=TOKEN)

    browser_sending(monkeypatch, "/callback?code=code-1&state={state}")
    client = httpx.Client(transport=httpx.MockTransport(handler))

    client_id, _ = run_login(client, PROVIDER, "client-existing", port=0, timeout=10)

    assert client_id == "client-existing"

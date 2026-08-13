"""Command line entry point: connect, status, logout."""

from __future__ import annotations

import argparse
import http.server
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import httpx

from oflow.auth import oauth
from oflow.auth.store import (
    Credentials,
    CredentialStoreError,
    delete_credentials,
    get_credentials,
    now,
    set_credentials,
)
from oflow.config import TabConfig, add_tab, load_config, save_config
from oflow.registry import UnknownIntegration, get_integration

LOGIN_TIMEOUT_SECONDS = 300


def _callback_handler(received: dict[str, str]) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            received.update({key: value[0] for key, value in query.items()})
            self.send_response(200)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"oflow: authentication complete. You can close this tab.")

        def log_message(self, format: str, *args: object) -> None:
            """Silence the default access log, which would print over our output."""

    return Handler


def run_login(
    client: httpx.Client, provider: oauth.ProviderConfig, client_id: str | None
) -> tuple[str, Credentials]:
    """Register if needed, take the user through the browser, return the tokens.

    The client id is returned alongside the credentials because a first login
    mints one, and it must be persisted so later logins reuse the registration.
    """
    metadata = oauth.discover(client, provider)
    if client_id is None:
        client_id = oauth.register_client(client, metadata, provider, oauth.REDIRECT_URI)

    verifier, challenge = oauth.make_pkce_pair()
    state = secrets.token_urlsafe(16)
    url = oauth.build_authorize_url(
        metadata, client_id, oauth.REDIRECT_URI, challenge, provider.scopes, state
    )

    received: dict[str, str] = {}
    try:
        server = http.server.HTTPServer(
            ("127.0.0.1", oauth.LOOPBACK_PORT), _callback_handler(received)
        )
    except OSError as error:
        raise oauth.OAuthError(
            f"port {oauth.LOOPBACK_PORT} is already in use, so the callback cannot be "
            f"received. Close whatever is holding it and try again."
        ) from error

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    print("opening your browser to authorize oflow")
    print(f"if it does not open, paste this:\n{url}\n")
    webbrowser.open(url)
    thread.join(timeout=LOGIN_TIMEOUT_SECONDS)
    server.server_close()

    if not received:
        raise oauth.OAuthError("timed out waiting for the browser callback")
    if "error" in received:
        raise oauth.OAuthError(f"authorization was refused: {received['error']}")
    if received.get("state") != state:
        raise oauth.OAuthError("the callback did not come from this login attempt; aborting")
    if "code" not in received:
        raise oauth.OAuthError("the callback carried no authorization code")

    credentials = oauth.exchange_code(
        client, metadata, client_id, received["code"], verifier, oauth.REDIRECT_URI
    )
    return client_id, credentials


def _connect(integration_id: str) -> int:
    try:
        integration = get_integration(integration_id)
    except UnknownIntegration as error:
        print(str(error), file=sys.stderr)
        return 1

    config = load_config()
    existing = next((tab for tab in config.tabs if tab.integration == integration_id), None)

    with httpx.Client(timeout=30) as client:
        try:
            client_id, credentials = run_login(
                client, integration.manifest.provider, existing.client_id if existing else None
            )
        except oauth.OAuthError as error:
            print(f"connect failed: {error}", file=sys.stderr)
            return 1

    try:
        set_credentials(integration_id, credentials)
    except CredentialStoreError as error:
        print(str(error), file=sys.stderr)
        return 1

    save_config(add_tab(config, TabConfig(integration=integration_id, client_id=client_id)))
    print(f"connected {integration.manifest.display_name} (scope: {credentials.scope})")
    return 0


def _describe(credentials: Credentials) -> str:
    if credentials.expires_at is None:
        expiry = "no expiry"
    elif credentials.is_expired(now()):
        expiry = "expired"
    else:
        expiry = f"expires {credentials.expires_at.isoformat(timespec='minutes')}"
    return f"connected — scope {credentials.scope}, {expiry}"


def _status() -> int:
    config = load_config()
    if not config.tabs:
        print("no tabs configured. run: oflow connect linear")
        return 0

    for tab in config.tabs:
        try:
            credentials = get_credentials(tab.integration)
        except CredentialStoreError as error:
            print(f"{tab.integration}: error — {error}")
            continue
        if credentials is None:
            print(f"{tab.integration}: disconnected")
        else:
            print(f"{tab.integration}: {_describe(credentials)}")
    return 0


def _revoke(provider: oauth.ProviderConfig, client_id: str, credentials: Credentials) -> bool:
    # Local deletion happens either way, so nothing here may raise: being
    # offline must not leave credentials stranded on the machine.
    try:
        with httpx.Client(timeout=15) as client:
            metadata = oauth.discover(client, provider)
            return oauth.revoke(client, metadata, client_id, credentials)
    except oauth.OAuthError:
        return False


def _logout(integration_id: str) -> int:
    try:
        integration = get_integration(integration_id)
    except UnknownIntegration as error:
        print(str(error), file=sys.stderr)
        return 1

    tab = next(
        (tab for tab in load_config().tabs if tab.integration == integration_id),
        None,
    )
    try:
        credentials = get_credentials(integration_id)
    except CredentialStoreError as error:
        print(str(error), file=sys.stderr)
        return 1

    revoked = False
    if credentials is not None and tab is not None and tab.client_id is not None:
        revoked = _revoke(integration.manifest.provider, tab.client_id, credentials)

    try:
        delete_credentials(integration_id)
    except CredentialStoreError as error:
        print(str(error), file=sys.stderr)
        return 1

    if credentials is None:
        print(f"{integration_id}: already disconnected")
    elif revoked:
        print(f"logged out of {integration_id}; the token was revoked")
    else:
        print(
            f"logged out of {integration_id}; the token could not be revoked and stays "
            f"valid until it expires"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oflow")
    subparsers = parser.add_subparsers(dest="command", required=True)

    connect = subparsers.add_parser("connect", help="authenticate an integration")
    connect.add_argument("integration")

    subparsers.add_parser("status", help="show connection state for configured tabs")

    logout = subparsers.add_parser("logout", help="revoke and delete stored credentials")
    logout.add_argument("integration")

    args = parser.parse_args(argv)
    if args.command == "connect":
        return _connect(args.integration)
    if args.command == "status":
        return _status()
    return _logout(args.integration)


if __name__ == "__main__":
    raise SystemExit(main())

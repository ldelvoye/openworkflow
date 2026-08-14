"""Command line entry point: connect, status, logout."""

from __future__ import annotations

import argparse
import http.server
import secrets
import sys
import time
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
from oflow.config import ConfigError, TabConfig, add_tab, load_config, save_config
from oflow.contract import Integration
from oflow.registry import UnknownIntegration, get_integration, known_integration_ids
from oflow.shell.app import OflowApp
from oflow.shell.terminal_palette import query_terminal_palette
from oflow.text import printable

LOGIN_TIMEOUT_SECONDS = 300


def _callback_handler(
    expected_state: str, received: dict[str, str]
) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            query = {key: value[0] for key, value in urllib.parse.parse_qs(parsed.query).items()}
            # A request that cannot prove it belongs to this login is refused
            # without ending the wait. Otherwise any stray probe on this port
            # consumes the callback and aborts a sign-in in progress, and a
            # forged ?error= reads as a genuine refusal — state is the only
            # thing distinguishing the provider's redirect from anyone else's.
            # Compared as bytes: the str form of compare_digest rejects
            # non-ASCII input with a TypeError, and this side of the comparison
            # is whatever the caller sent.
            if (
                parsed.path != "/callback"
                or not secrets.compare_digest(
                    query.get("state", "").encode(), expected_state.encode()
                )
                or not query.keys() & {"code", "error"}
            ):
                self.send_response(404)
                self.end_headers()
                return
            received.update(query)
            self.send_response(200)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"oflow: authentication complete. You can close this tab.")

        def log_message(self, format: str, *args: object) -> None:
            """Silence the default access log, which would print over our output."""

    return Handler


def run_login(
    client: httpx.Client,
    provider: oauth.ProviderConfig,
    client_id: str | None,
    port: int = 0,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
) -> tuple[str, Credentials]:
    """Register if needed, take the user through the browser, return the tokens.

    The client id comes back alongside the credentials because a first login
    mints one, and it has to be persisted so later logins reuse the registration.

    Registration names a stable loopback URI while the callback listens on an
    ephemeral port — see REGISTRATION_PORT for why a server accepts that. The
    payoff is that nothing can bind a port it cannot predict.
    """
    verifier, challenge = oauth.make_pkce_pair()
    state = secrets.token_urlsafe(16)
    received: dict[str, str] = {}

    try:
        server = http.server.HTTPServer(("127.0.0.1", port), _callback_handler(state, received))
    except OSError as error:
        raise oauth.OAuthError(f"could not open a port for the callback: {error}") from error

    try:
        # Bound before the redirect is built so an ephemeral port resolves to
        # the one actually listening.
        redirect_uri = oauth.redirect_uri_for(server.server_port)
        metadata = oauth.discover(client, provider)
        if client_id is None:
            client_id = oauth.register_client(
                client, metadata, provider, oauth.REGISTERED_REDIRECT_URI
            )

        url = oauth.build_authorize_url(
            metadata, client_id, redirect_uri, challenge, provider.scopes, state
        )
        print("opening your browser to authorize oflow")
        print(f"if it does not open, paste this:\n{url}\n")
        webbrowser.open(url)

        # One deadline for the whole wait rather than per request, so a drip of
        # stray requests cannot extend it indefinitely.
        server.timeout = 1.0
        deadline = time.monotonic() + timeout
        while not received and time.monotonic() < deadline:
            server.handle_request()

        if not received:
            raise oauth.OAuthError("timed out waiting for the browser callback")
        if "error" in received:
            raise oauth.OAuthError(f"authorization was refused: {printable(received['error'])}")

        credentials = oauth.exchange_code(
            client, metadata, client_id, received["code"], verifier, redirect_uri
        )
        return client_id, credentials
    finally:
        server.server_close()


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

    _warn_on_extra_scopes(integration, credentials)

    try:
        set_credentials(integration_id, credentials)
    except CredentialStoreError as error:
        # The token is live and about to become unreachable — nothing will hold
        # it, so nothing could revoke it later. Hand it back before giving up.
        _revoke(integration.manifest.provider, client_id, credentials)
        print(str(error), file=sys.stderr)
        return 1

    save_config(add_tab(config, TabConfig(integration=integration_id, client_id=client_id)))
    print(f"connected {integration.manifest.display_name} (scope: {credentials.scope})")
    return 0


def _warn_on_extra_scopes(integration: Integration, credentials: Credentials) -> None:
    """Say so when the provider granted more than was asked for.

    A warning rather than a refusal: providers may return their whole granted
    set regardless of the request, and every call site here is read-only. The
    cost of an over-scoped token is that it is a bigger prize if stolen, which
    is worth knowing before deciding to keep it — hence before the success line.
    """
    extra = sorted(set(credentials.scope.split()) - set(integration.manifest.provider.scopes))
    if not extra:
        return
    print(
        f"warning: {integration.manifest.display_name} granted scopes oflow did not ask for: "
        f"{', '.join(extra)}. Nothing here uses them, but the stored token can. "
        f"Run 'oflow logout {integration.manifest.id}' to revoke it.",
        file=sys.stderr,
    )


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
        available = known_integration_ids()
        if available:
            print(f"no tabs configured. run: oflow connect {available[0]}")
        else:
            print("no tabs configured, and this build registers no integrations")
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
    # Deleting must not depend on the registry. Credentials outlive an
    # integration dropped from a build, and requiring it here would leave them
    # stored with no command able to remove them.
    integration: Integration | None = None
    unsupported = ""
    try:
        integration = get_integration(integration_id)
    except UnknownIntegration as error:
        unsupported = str(error)

    try:
        credentials = get_credentials(integration_id)
    except CredentialStoreError as error:
        print(str(error), file=sys.stderr)
        return 1

    if integration is None and credentials is None:
        print(unsupported, file=sys.stderr)
        return 1

    tab = next(
        (tab for tab in load_config().tabs if tab.integration == integration_id),
        None,
    )
    revoked = False
    if integration is not None and credentials is not None and tab is not None and tab.client_id:
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
    elif integration is None:
        print(
            f"deleted credentials for {integration_id}, which this build no longer supports. "
            f"The token could not be revoked and stays valid until it expires."
        )
    else:
        print(
            f"logged out of {integration_id}; the token could not be revoked and stays "
            f"valid until it expires"
        )
    return 0


def _run() -> int:
    tabs = tuple(tab.integration for tab in load_config().tabs)
    # Must happen before OflowApp exists: once Textual's driver is running it
    # owns stdin, and it has no notion of an OSC color-query response (see
    # query_terminal_palette's docstring) — this is the only safe window.
    palette = query_terminal_palette()
    OflowApp(tabs=tabs, palette=palette).run()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oflow")
    subparsers = parser.add_subparsers(dest="command", required=True)

    connect = subparsers.add_parser("connect", help="authenticate an integration")
    connect.add_argument("integration")

    subparsers.add_parser("status", help="show connection state for configured tabs")

    logout = subparsers.add_parser("logout", help="revoke and delete stored credentials")
    logout.add_argument("integration")

    subparsers.add_parser("run", help="open the dashboard")

    args = parser.parse_args(argv)
    try:
        if args.command == "connect":
            return _connect(args.integration)
        if args.command == "status":
            return _status()
        if args.command == "run":
            return _run()
        return _logout(args.integration)
    except ConfigError as error:
        # The config file is documented as hand-editable, so a broken one is a
        # user mistake to report, not a traceback.
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

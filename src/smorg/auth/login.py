"""The browser-driven half of OAuth: run a loopback callback server, send the
user to authorize, wait for the redirect, and exchange the code.

Print-free: the caller decides how to surface the authorize URL
(`on_authorize_url`) and whether the wait can be cancelled (`cancelled`).
`cli.run_login` is the printing frontend; the in-app connect modal is the other.
"""

from __future__ import annotations

import http.server
import secrets
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable

import httpx

from smorg.auth import oauth
from smorg.auth.oauth import OAuthMethod
from smorg.auth.store import Credentials
from smorg.core.text import sanitize_line

LOGIN_TIMEOUT_SECONDS = 300


class LoginCancelled(oauth.OAuthError):
    """The caller's `cancelled` event was set before the callback arrived."""


def _callback_handler(
    expected_state: str, received: dict[str, str]
) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            parsed_query = urllib.parse.parse_qs(parsed.query)
            query = {key: value[0] for key, value in parsed_query.items()}
            # Only a request whose `state` matches this login's is accepted —
            # a stray probe or a forged ?error= is refused without ending the
            # wait. Compared as bytes, since compare_digest's str form raises
            # on non-ASCII input.
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
            self.wfile.write(b"smorg: authentication complete. You can close this tab.")

        def log_message(self, format: str, *args: object) -> None:
            """Silence the default access log, which would print over our output."""

    return Handler


def _cancellation_requested(cancelled: threading.Event | None) -> bool:
    if cancelled is None:
        return False
    return cancelled.is_set()


def perform_login(
    client: httpx.Client,
    method: OAuthMethod,
    client_id: str | None,
    *,
    on_authorize_url: Callable[[str], None],
    cancelled: threading.Event | None = None,
    port: int | None = None,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
) -> tuple[str, Credentials]:
    """Register if needed, take the user through the browser, return the tokens.

    Returns the client id alongside the credentials so a first-time
    registration can be persisted for later logins to reuse. port None means
    the provider's own default (see oauth.callback_port)
    """
    verifier, challenge = oauth.make_pkce_pair()
    state = secrets.token_urlsafe(16)
    received: dict[str, str] = {}

    if port is None:
        port = oauth.callback_port(method)
    try:
        server = http.server.HTTPServer(("127.0.0.1", port), _callback_handler(state, received))
    except OSError as error:
        raise oauth.OAuthError(f"could not open a port for the callback: {error}") from error

    try:
        redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
        metadata = oauth.resolve_metadata(client, method)
        if client_id is None:
            provider = method.provider
            if isinstance(provider, oauth.StaticProvider):
                raise oauth.OAuthError(
                    "this provider cannot register clients; connect with a client id"
                )
            client_id = oauth.register_client(
                client, metadata, provider, oauth.REGISTERED_REDIRECT_URI
            )

        url = oauth.build_authorize_url(
            metadata, client_id, redirect_uri, challenge, method.scopes, state
        )
        on_authorize_url(url)
        webbrowser.open(url)

        # One deadline for the whole wait rather than per request, so a drip of
        # stray requests cannot extend it indefinitely. cancelled is polled at
        # the same 1-second granularity as the deadline.
        server.timeout = 1.0  # seconds
        deadline = time.monotonic() + timeout
        while (
            not received and time.monotonic() < deadline and not _cancellation_requested(cancelled)
        ):
            server.handle_request()

        # A code that already arrived wins over a cancellation requested in
        # the same instant — only an empty result can still be cancelled.
        if not received:
            if _cancellation_requested(cancelled):
                raise LoginCancelled("login cancelled before the browser callback arrived")
            raise oauth.OAuthError("timed out waiting for the browser callback")
        if "error" in received:
            raise oauth.OAuthError(f"authorization was refused: {sanitize_line(received['error'])}")

        credentials = oauth.exchange_code(
            client, metadata, client_id, received["code"], verifier, redirect_uri
        )
        return client_id, credentials
    finally:
        server.server_close()

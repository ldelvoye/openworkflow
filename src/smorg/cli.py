"""Command line entry point: connect, status, logout."""

from __future__ import annotations

import argparse
import sys
from getpass import getpass

import httpx

from smorg import __version__
from smorg.auth import oauth
from smorg.auth.login import LOGIN_TIMEOUT_SECONDS, perform_login
from smorg.auth.oauth import extra_scopes_warning
from smorg.auth.store import (
    Credentials,
    CredentialStoreError,
    get_credentials,
    now,
    set_credentials,
)
from smorg.auth.token import InvalidToken, TokenPrompt, accepted_token, credentials_from_token
from smorg.core.config import (
    Config,
    ConfigError,
    TabConfig,
    add_tab,
    load_config,
    resolve_connection,
    save_config,
    tab_for,
)
from smorg.core.contract import ConnectionPath, Integration
from smorg.core.registry import UnknownIntegration, get_integration, known_integration_ids
from smorg.core.removal import remove_integration, revoke_best_effort
from smorg.shell.app import SmorgApp
from smorg.shell.terminal_palette import query_terminal_palette


def run_login(
    client: httpx.Client,
    provider: oauth.ProviderConfig,
    client_id: str | None,
    port: int = 0,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
) -> tuple[str, Credentials]:
    """Thin printing wrapper over perform_login: prints the same two lines
    this command has always printed, delegates the rest."""

    def on_authorize_url(url: str) -> None:
        print("opening your browser to authorize smorg")
        print(f"if it does not open, paste this:\n{url}\n")

    return perform_login(
        client, provider, client_id, on_authorize_url=on_authorize_url, port=port, timeout=timeout
    )


def _connect(integration_id: str) -> int:
    try:
        integration = get_integration(integration_id)
    except UnknownIntegration as error:
        print(str(error), file=sys.stderr)
        return 1

    config = load_config()
    try:
        path, existing_client_id = resolve_connection(
            integration.manifest, tab_for(config, integration_id)
        )
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1

    method = path.method
    if isinstance(method, TokenPrompt):
        return _connect_with_token(integration, config, path, method)

    provider = method

    with httpx.Client(timeout=30) as client:
        try:
            client_id, credentials = run_login(client, provider, existing_client_id)
        except oauth.OAuthError as error:
            print(f"connect failed: {error}", file=sys.stderr)
            return 1

    _warn_on_extra_scopes(integration, provider, credentials)

    try:
        set_credentials(integration_id, credentials)
    except CredentialStoreError as error:
        # The token is live and about to become unreachable — nothing will hold
        # it, so nothing could revoke it later. Hand it back before giving up.
        revoke_best_effort(provider, client_id, credentials)
        print(str(error), file=sys.stderr)
        return 1

    save_config(
        add_tab(
            config,
            TabConfig(integration=integration_id, client_id=client_id, connection=path.id),
        )
    )
    print(f"connected {integration.manifest.display_name} (scope: {credentials.scope})")
    return 0


def _connect_with_token(
    integration: Integration, config: Config, path: ConnectionPath, prompt: TokenPrompt
) -> int:
    """Ask for a token the user created themselves and store it.

    Re-running this is also the whole re-authentication story for a token
    path: add_tab replaces the existing entry, and the new token overwrites
    the old one in the store — which is what the "run: smorg connect" in an
    expired tab's error is pointing at.
    """
    display_name = integration.manifest.display_name
    print(f"{display_name} connects with a token you create yourself.")
    print(f"create one at: {prompt.help_url}")
    print(f"it needs: {prompt.scopes_hint}")
    # getpass, not input: the token is a live credential and a terminal's
    # scrollback outlives this command.
    entered = getpass(f"{prompt.label}: ")
    try:
        token = accepted_token(entered)
    except InvalidToken as error:
        print(str(error), file=sys.stderr)
        return 1

    try:
        set_credentials(integration.manifest.id, credentials_from_token(token))
    except CredentialStoreError as error:
        # Nothing to hand back, unlike the OAuth path: this token was not
        # issued to us, and it stays the user's to revoke either way.
        print(str(error), file=sys.stderr)
        return 1

    save_config(add_tab(config, TabConfig(integration=integration.manifest.id, connection=path.id)))
    print(f"connected {display_name}")
    return 0


def _warn_on_extra_scopes(
    integration: Integration, provider: oauth.ProviderConfig, credentials: Credentials
) -> None:
    """Say so when the provider granted more than was asked for. See
    extra_scopes_warning for why this is worth a warning and not a refusal."""
    warning = extra_scopes_warning(
        integration.manifest.id, integration.manifest.display_name, provider, credentials
    )
    if warning is not None:
        print(f"warning: {warning}", file=sys.stderr)


def _describe(credentials: Credentials) -> str:
    if credentials.expires_at is None:
        expiry = "no expiry"
    elif credentials.is_expired(now()):
        expiry = "expired"
    else:
        expiry = f"expires {credentials.expires_at.isoformat(timespec='minutes')}"
    # A pasted token carries no scope to report; naming an empty one would
    # read as a token that was granted nothing.
    if not credentials.scope:
        return f"connected — {expiry}"
    return f"connected — scope {credentials.scope}, {expiry}"


def _status() -> int:
    config = load_config()
    if not config.tabs:
        available = known_integration_ids()
        if available:
            print(f"no tabs configured. run: smorg connect {available[0]}")
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


def _logout(integration_id: str) -> int:
    # A renderer over remove_integration: the helper does the deleting and
    # reports what it found, this only chooses the words.
    try:
        result = remove_integration(integration_id)
    except UnknownIntegration as error:
        print(str(error), file=sys.stderr)
        return 1
    except CredentialStoreError as error:
        print(str(error), file=sys.stderr)
        return 1

    if not result.had_credentials:
        print(f"{integration_id}: already disconnected")
    elif result.revoked:
        print(f"logged out of {integration_id}; the token was revoked")
    elif not result.supported:
        print(
            f"deleted credentials for {integration_id}, which this build no longer supports. "
            f"The token could not be revoked and stays valid until it expires."
        )
    else:
        print(
            f"logged out of {integration_id}; the token could not be revoked and stays "
            f"valid until it expires"
        )
    if result.tab_removed:
        print(f"removed the {integration_id} tab")
    return 0


def _run() -> int:
    tabs = load_config().tabs
    # Must happen before SmorgApp exists: once Textual's driver is running it
    # owns stdin, and it has no notion of an OSC color-query response (see
    # query_terminal_palette's docstring) — this is the only safe window.
    palette = query_terminal_palette()
    SmorgApp(tabs=tabs, palette=palette).run()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smorg")
    parser.add_argument("--version", action="version", version=f"smorg {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    connect = subparsers.add_parser("connect", help="authenticate an integration")
    connect.add_argument("integration")

    subparsers.add_parser("status", help="show connection state for configured tabs")

    logout = subparsers.add_parser(
        "logout", help="disconnect an integration and remove its tab and stored data"
    )
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

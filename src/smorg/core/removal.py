"""Remove every trace of an integration: stored credentials, its config tab,
and its seen-state marks.

Lives in core, not cli or shell: both need it, and the CLI imports the shell,
so shell code could never reach logic defined in cli.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from smorg.auth import oauth
from smorg.auth.store import Credentials, delete_credentials, get_credentials
from smorg.core.config import Config, load_config, save_config, tab_for
from smorg.core.registry import UnknownIntegration, get_integration
from smorg.core.state import SeenState


def revoke_best_effort(
    provider: oauth.ProviderConfig, client_id: str, credentials: Credentials
) -> bool:
    """Ask the provider to revoke a token. Never raises.

    Local deletion happens either way, so a revocation failure must not block
    it: being offline must not leave credentials stranded on the machine.
    """
    try:
        with httpx.Client(timeout=15) as client:
            metadata = oauth.discover(client, provider)
            return oauth.revoke(client, metadata, client_id, credentials)
    except oauth.OAuthError:
        return False


@dataclass(frozen=True)
class RemovalResult:
    supported: bool
    had_credentials: bool
    revoked: bool
    tab_removed: bool


def remove_integration(integration_id: str) -> RemovalResult:
    """Delete every stored trace of an integration and report what was found.

    Works even for an integration this build no longer registers — this is
    the only path that can still remove its leftover credentials/tab.
    """
    try:
        integration = get_integration(integration_id)
    except UnknownIntegration:
        integration = None

    credentials = get_credentials(integration_id)  # CredentialStoreError propagates

    config = load_config()  # ConfigError propagates
    tab = tab_for(config, integration_id)

    if integration is None and credentials is None and tab is None:
        # Nothing to remove — reuse get_integration's own "not supported" error.
        get_integration(integration_id)

    revoked = False
    if integration is not None and credentials is not None and tab is not None and tab.client_id:
        try:
            path = integration.manifest.connection(tab.connection)
        except ValueError:
            path = None  # a stale connection id must not block deletion

        # Only try to revoke OAuth tokens: a pasted token has no provider to ask
        if path is not None and isinstance(path.method, oauth.ProviderConfig):
            revoked = revoke_best_effort(path.method, tab.client_id, credentials)

    # Credentials before config: dropping the tab first could strand
    # credentials with nothing left pointing at them.
    delete_credentials(integration_id)  # CredentialStoreError propagates

    tab_removed = tab is not None
    if tab_removed:
        remaining_tabs = tuple(
            entry for entry in config.tabs if entry.integration != integration_id
        )
        save_config(Config(tabs=remaining_tabs))

    state = SeenState.load()
    state.forget(integration_id)
    state.save()

    return RemovalResult(
        supported=integration is not None,
        had_credentials=credentials is not None,
        revoked=revoked,
        tab_removed=tab_removed,
    )

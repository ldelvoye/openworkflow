from dataclasses import dataclass
from datetime import timedelta

import httpx
import pytest

from smorg.auth.oauth import ProviderConfig
from smorg.auth.store import Credentials
from smorg.core.contract import (
    Action,
    ActionClass,
    AuthExpired,
    ConnectionPath,
    IntegrationError,
    Item,
    Malformed,
    Manifest,
    Unavailable,
)
from smorg.core.registry import (
    UnknownIntegration,
    get_integration,
    known_integration_ids,
    manifests,
)
from smorg.shell.panel import Panel

PROVIDER = ProviderConfig(
    metadata_url="https://example.invalid/.well-known/oauth-authorization-server",
    scopes=("read",),
    client_name="smorg",
)
DEFAULT_CONNECTIONS = (ConnectionPath(id="mcp", method=PROVIDER),)


def manifest(
    identifier: str = "fake",
    actions: tuple[Action, ...] = (),
    connections: tuple[ConnectionPath, ...] = DEFAULT_CONNECTIONS,
) -> Manifest:
    return Manifest(
        id=identifier,
        display_name=identifier.title(),
        connections=connections,
        stale_after=timedelta(minutes=5),
        actions=actions,
    )


@dataclass(frozen=True)
class FakeIntegration:
    """Stands in for a real integration, so it satisfies the whole protocol.

    A fake that implements less than the contract can pass a test that a real
    integration would fail.
    """

    manifest: Manifest
    panel_class: type[Panel] = Panel

    def fetch(self, credentials: Credentials, http: httpx.Client) -> tuple[Item, ...]:
        return ()


@pytest.fixture
def registered(monkeypatch):
    def register(*manifests: Manifest):
        integrations = tuple(FakeIntegration(manifest=entry) for entry in manifests)
        monkeypatch.setattr("smorg.integrations.INTEGRATIONS", integrations)
        return integrations

    return register


def test_every_integration_error_shares_one_base():
    for error in (AuthExpired, Unavailable, Malformed):
        assert issubclass(error, IntegrationError)


def test_manifest_accepts_a_valid_declaration():
    declared = manifest(
        actions=(Action(id="open", label="Open", key="o", action_class=ActionClass.LAUNCH),)
    )
    assert declared.actions[0].action_class is ActionClass.LAUNCH


def test_manifest_rejects_duplicate_action_keys():
    with pytest.raises(ValueError, match="duplicate action key"):
        manifest(
            actions=(
                Action(id="open", label="Open", key="o", action_class=ActionClass.LAUNCH),
                Action(id="copy", label="Copy", key="o", action_class=ActionClass.LOCAL),
            )
        )


def test_manifest_rejects_a_reserved_shell_key():
    with pytest.raises(ValueError, match="reserved"):
        manifest(
            actions=(Action(id="reload", label="Reload", key="r", action_class=ActionClass.LOCAL),)
        )


def test_manifest_rejects_empty_connections():
    with pytest.raises(ValueError, match="no connection path"):
        manifest(connections=())


def test_manifest_rejects_duplicate_connection_ids():
    with pytest.raises(ValueError, match="duplicate connection path"):
        manifest(connections=(ConnectionPath(id="mcp", method=PROVIDER),) * 2)


def test_connection_with_no_chosen_id_returns_the_first_declared_path():
    mcp = ConnectionPath(id="mcp", method=PROVIDER)
    api_key = ConnectionPath(id="api-key", method=PROVIDER)
    declared = manifest(connections=(mcp, api_key))
    assert declared.connection(None) is mcp


def test_connection_with_an_unknown_id_names_the_declared_ones():
    declared = manifest(connections=(ConnectionPath(id="mcp", method=PROVIDER),))
    with pytest.raises(ValueError, match="mcp") as excinfo:
        declared.connection("nope")
    assert "nope" in str(excinfo.value)


def test_get_integration_returns_the_registered_object(registered):
    integrations = registered(manifest("linear"))
    assert get_integration("linear") is integrations[0]


def test_known_integration_ids_are_sorted(registered):
    registered(manifest("sentry"), manifest("linear"))
    assert known_integration_ids() == ("linear", "sentry")


def test_unknown_integration_names_what_is_available(registered):
    registered(manifest("linear"))
    with pytest.raises(UnknownIntegration) as excinfo:
        get_integration("jira")
    message = str(excinfo.value)
    assert "jira" in message
    assert "linear" in message


def test_unknown_integration_when_nothing_is_registered(registered):
    registered()
    with pytest.raises(UnknownIntegration, match="no integrations"):
        get_integration("jira")


def test_registry_refuses_two_integrations_sharing_an_id(registered):
    registered(manifest("linear"), manifest("linear"))
    with pytest.raises(ValueError, match="linear"):
        get_integration("linear")


def test_manifests_enumerates_every_registered_one_sorted_by_id(registered):
    """Also proves manifests() reads the same per-call INTEGRATIONS import as
    _by_id: the registered fixture only takes effect through that indirection.
    """
    registered(manifest("sentry"), manifest("linear"))
    assert [entry.id for entry in manifests()] == ["linear", "sentry"]

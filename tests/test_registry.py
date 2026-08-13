from dataclasses import dataclass
from datetime import timedelta

import httpx
import pytest

from oflow.auth.oauth import ProviderConfig
from oflow.auth.store import Credentials
from oflow.contract import (
    Action,
    ActionClass,
    AuthExpired,
    IntegrationError,
    Item,
    Malformed,
    Manifest,
    Unavailable,
)
from oflow.registry import UnknownIntegration, get_integration, known_integration_ids

PROVIDER = ProviderConfig(
    metadata_url="https://example.invalid/.well-known/oauth-authorization-server",
    scopes=("read",),
    client_name="oflow",
)


def manifest(identifier: str = "fake", actions: tuple[Action, ...] = ()) -> Manifest:
    return Manifest(
        id=identifier,
        display_name=identifier.title(),
        provider=PROVIDER,
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

    def fetch(self, credentials: Credentials, http: httpx.Client) -> tuple[Item, ...]:
        return ()


@pytest.fixture
def registered(monkeypatch):
    def register(*manifests: Manifest):
        integrations = tuple(FakeIntegration(manifest=entry) for entry in manifests)
        monkeypatch.setattr("oflow.integrations.INTEGRATIONS", integrations)
        return integrations

    return register


def test_action_classes_are_the_three_safety_tiers():
    assert {member.value for member in ActionClass} == {"local", "launch", "remote"}
    assert ActionClass.LAUNCH == "launch"


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

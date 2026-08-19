from datetime import UTC, datetime

import pytest

from smorg.auth import oauth
from smorg.auth.store import Credentials, get_credentials, set_credentials
from smorg.core.config import Config, TabConfig, load_config, save_config
from smorg.core.contract import Item
from smorg.core.registry import UnknownIntegration
from smorg.core.removal import remove_integration
from smorg.core.state import SeenState

LIVE = Credentials(
    access_token="at-secret",
    refresh_token="rt-secret",
    expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    scope="read",
)
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def item(identifier: str = "ENG-1") -> Item:
    return Item(id=identifier, updated_at=NOW, url="https://example.invalid/1")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SMORG_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("SMORG_CREDENTIAL_STORE", "file")


def test_removal_purges_all_three_stores_for_only_the_named_integration():
    save_config(Config(tabs=(TabConfig(integration="linear"), TabConfig(integration="sentry"))))
    set_credentials("linear", LIVE)
    set_credentials("sentry", LIVE)
    state = SeenState.load()
    state.mark_seen("linear", item())
    state.mark_seen("sentry", item())
    state.save()

    remove_integration("linear")

    assert get_credentials("linear") is None
    assert get_credentials("sentry") is not None
    assert [tab.integration for tab in load_config().tabs] == ["sentry"]
    survivors = SeenState.load()
    assert survivors.is_changed("linear", item()) is True
    assert survivors.is_changed("sentry", item()) is False


def test_unknown_id_with_no_stored_trace_raises_unknown_integration():
    with pytest.raises(UnknownIntegration, match="jira"):
        remove_integration("jira")


def test_credentials_outliving_a_dropped_integration_are_still_deleted():
    save_config(Config(tabs=(TabConfig(integration="jira"),)))
    set_credentials("jira", LIVE)

    result = remove_integration("jira")

    assert result.supported is False
    assert result.revoked is False
    assert result.had_credentials is True
    assert get_credentials("jira") is None
    assert load_config().tabs == ()


def test_revocation_failure_still_deletes_everything(monkeypatch):
    save_config(Config(tabs=(TabConfig(integration="linear", client_id="client-abc"),)))
    set_credentials("linear", LIVE)

    def unreachable(client, provider):
        raise oauth.OAuthError("discovery unreachable")

    monkeypatch.setattr("smorg.core.removal.oauth.discover", unreachable)

    result = remove_integration("linear")

    assert result.revoked is False
    assert get_credentials("linear") is None
    assert load_config().tabs == ()


def test_stale_connection_id_skips_revocation_but_removal_completes(monkeypatch):
    save_config(
        Config(tabs=(TabConfig(integration="linear", client_id="client-abc", connection="nope"),))
    )
    set_credentials("linear", LIVE)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("revocation must not be attempted for a stale connection id")

    monkeypatch.setattr("smorg.core.removal.oauth.discover", fail_if_called)

    result = remove_integration("linear")

    assert result.revoked is False
    assert get_credentials("linear") is None
    assert load_config().tabs == ()

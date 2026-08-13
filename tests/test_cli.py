from datetime import UTC, datetime

import pytest

from oflow.auth.store import (
    Credentials,
    CredentialStoreError,
    get_credentials,
    set_credentials,
)
from oflow.cli import main
from oflow.config import Config, TabConfig, load_config, save_config
from oflow.registry import known_integration_ids

LIVE = Credentials(
    access_token="at-secret",
    refresh_token="rt-secret",
    expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    scope="read",
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OFLOW_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("OFLOW_CREDENTIAL_STORE", "file")


@pytest.fixture
def connected():
    save_config(Config(tabs=(TabConfig(integration="linear", client_id="client-abc"),)))
    set_credentials("linear", LIVE)


@pytest.fixture
def revocation(monkeypatch):
    """Stub the network so logout never reaches a real server."""
    calls = {"revoked": 0}

    def fake_discover(client, provider):
        return object()

    def fake_revoke(client, metadata, client_id, credentials):
        calls["revoked"] += 1
        return calls.get("succeeds", True)

    monkeypatch.setattr("oflow.cli.oauth.discover", fake_discover)
    monkeypatch.setattr("oflow.cli.oauth.revoke", fake_revoke)
    return calls


def test_linear_is_the_registered_integration():
    assert known_integration_ids() == ("linear",)


def test_connect_rejects_an_unknown_integration(capsys):
    assert main(["connect", "jira"]) == 1
    captured = capsys.readouterr()
    assert "not a supported integration" in captured.err
    assert "linear" in captured.err


def test_logout_rejects_an_unknown_integration(capsys):
    assert main(["logout", "jira"]) == 1
    assert "not a supported integration" in capsys.readouterr().err


def test_status_with_nothing_configured(capsys):
    assert main(["status"]) == 0
    assert "no tabs configured" in capsys.readouterr().out


def test_status_reports_the_connection_without_a_token(connected, capsys):
    assert main(["status"]) == 0
    output = capsys.readouterr().out

    assert "at-secret" not in output
    assert "rt-secret" not in output
    assert "linear" in output
    assert "read" in output


def test_status_reports_a_configured_tab_with_no_credentials(capsys):
    save_config(Config(tabs=(TabConfig(integration="linear", client_id="client-abc"),)))
    assert main(["status"]) == 0
    assert "disconnected" in capsys.readouterr().out


def test_logout_deletes_credentials_but_keeps_the_tab(connected, revocation):
    assert main(["logout", "linear"]) == 0
    assert get_credentials("linear") is None
    assert load_config().tabs[0].integration == "linear"


def test_logout_revokes_before_deleting(connected, revocation, capsys):
    assert main(["logout", "linear"]) == 0
    assert revocation["revoked"] == 1
    assert "revoked" in capsys.readouterr().out


def test_logout_still_deletes_when_revocation_fails(connected, revocation, capsys):
    revocation["succeeds"] = False
    assert main(["logout", "linear"]) == 0

    assert get_credentials("linear") is None
    assert "could not be revoked" in capsys.readouterr().out


def test_connect_warns_about_scopes_beyond_the_request(monkeypatch, capsys):
    over_scoped = Credentials("at", "rt", None, "read write")
    monkeypatch.setattr("oflow.cli.run_login", lambda *args, **kwargs: ("client-abc", over_scoped))

    assert main(["connect", "linear"]) == 0

    captured = capsys.readouterr()
    assert "write" in captured.err
    assert "did not ask for" in captured.err
    assert "connected Linear" in captured.out


def test_connect_revokes_a_token_it_cannot_store(monkeypatch):
    credentials = Credentials("at", "rt", None, "read")
    monkeypatch.setattr("oflow.cli.run_login", lambda *args, **kwargs: ("client-abc", credentials))
    revoked: list[tuple] = []
    monkeypatch.setattr("oflow.cli._revoke", lambda *args: bool(revoked.append(args)) or True)

    def refuse(*args):
        raise CredentialStoreError("keychain refused")

    monkeypatch.setattr("oflow.cli.set_credentials", refuse)

    assert main(["connect", "linear"]) == 1
    assert len(revoked) == 1


def test_logout_deletes_credentials_for_a_deregistered_integration(capsys):
    set_credentials("jira", LIVE)

    assert main(["logout", "jira"]) == 0

    assert get_credentials("jira") is None
    assert "no longer supports" in capsys.readouterr().out


def test_logout_when_already_disconnected_makes_no_revocation_call(revocation, capsys):
    save_config(Config(tabs=(TabConfig(integration="linear", client_id="client-abc"),)))
    assert main(["logout", "linear"]) == 0
    assert revocation["revoked"] == 0
    assert "already disconnected" in capsys.readouterr().out

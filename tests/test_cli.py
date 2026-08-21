from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from smorg.auth.oauth import OAuthMethod, ServerMetadata, StaticProvider
from smorg.auth.store import (
    Credentials,
    CredentialStoreError,
    get_credentials,
    set_credentials,
)
from smorg.cli import _offer_path_setup, main
from smorg.core.config import Config, TabConfig, load_config, save_config
from smorg.core.contract import AuthPath, Manifest
from smorg.core.registry import known_integration_ids

LIVE = Credentials(
    access_token="at-secret",
    refresh_token="rt-secret",
    expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    scope="read",
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SMORG_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("SMORG_CREDENTIAL_STORE", "file")


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

    monkeypatch.setattr("smorg.core.removal.oauth.discover", fake_discover)
    monkeypatch.setattr("smorg.core.removal.oauth.revoke", fake_revoke)
    return calls


# --- --version ---


def test_version_flag_exits():
    with pytest.raises(SystemExit):
        main(["--version"])


def test_version_flag_says_dev_on_a_dev_build(monkeypatch, capsys):
    monkeypatch.setattr("smorg.cli.is_dev_build", lambda: True)

    with pytest.raises(SystemExit):
        main(["--version"])

    assert capsys.readouterr().out.strip().endswith("(dev)")


def test_version_flag_omits_dev_on_a_release_build(monkeypatch, capsys):
    monkeypatch.setattr("smorg.cli.is_dev_build", lambda: False)

    with pytest.raises(SystemExit):
        main(["--version"])

    assert not capsys.readouterr().out.strip().endswith("(dev)")


def test_the_allowlist_is_what_this_build_registers():
    assert known_integration_ids() == ("github", "linear", "spotify")


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


def test_logout_deletes_credentials_and_removes_the_tab(connected, revocation, capsys):
    assert main(["logout", "linear"]) == 0
    assert get_credentials("linear") is None
    assert load_config().tabs == ()
    assert "removed the linear tab" in capsys.readouterr().out


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
    monkeypatch.setattr("smorg.cli.run_login", lambda *args, **kwargs: ("client-abc", over_scoped))

    assert main(["connect", "linear"]) == 0

    captured = capsys.readouterr()
    assert "write" in captured.err
    assert "did not ask for" in captured.err
    assert "connected Linear" in captured.out


def test_connect_revokes_a_token_it_cannot_store(monkeypatch):
    credentials = Credentials("at", "rt", None, "read")
    monkeypatch.setattr("smorg.cli.run_login", lambda *args, **kwargs: ("client-abc", credentials))
    revoked: list[tuple] = []

    def fake_revoke(*args):
        revoked.append(args)
        return True

    monkeypatch.setattr("smorg.cli.revoke_best_effort", fake_revoke)

    def refuse(*args):
        raise CredentialStoreError("keychain refused")

    monkeypatch.setattr("smorg.cli.set_credentials", refuse)

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


# --- Opening the dashboard ---


def test_no_command_opens_the_dashboard(monkeypatch):
    calls = {"run": 0}

    def fake_run():
        calls["run"] += 1
        return 0

    monkeypatch.setattr("smorg.cli._run", fake_run)

    assert main([]) == 0
    assert calls["run"] == 1


def test_run_command_opens_the_dashboard(monkeypatch):
    calls = {"run": 0}

    def fake_run():
        calls["run"] += 1
        return 0

    monkeypatch.setattr("smorg.cli._run", fake_run)

    assert main(["run"]) == 0
    assert calls["run"] == 1


# --- Connecting with a token the user pasted ---

PASTED = "github_pat_11ABCDEFG0abcdefghijklmnop"


class Pasting:
    """Stands in for the person at the hidden prompt."""

    def __init__(self, monkeypatch) -> None:
        self._monkeypatch = monkeypatch
        self.enter(PASTED)

    def enter(self, entered: str) -> None:
        def answer(prompt: str) -> str:
            return entered

        self._monkeypatch.setattr("smorg.cli.getpass", answer)


@pytest.fixture
def pasting(monkeypatch):
    return Pasting(monkeypatch)


def test_connect_stores_a_pasted_token_and_records_the_tab(pasting):
    assert main(["connect", "github"]) == 0

    stored = get_credentials("github")
    assert stored is not None
    assert stored.access_token == PASTED
    assert load_config().tabs == (TabConfig(integration="github", connection="token"),)


def test_connect_says_where_to_get_a_token_and_what_it_needs(pasting, capsys):
    main(["connect", "github"])

    out = capsys.readouterr().out
    assert "github.com/settings/personal-access-tokens" in out
    assert "Pull requests" in out


def test_connect_never_echoes_a_pasted_token(pasting, capsys):
    """getpass keeps it off the screen; nothing after may put it back."""
    main(["connect", "github"])

    captured = capsys.readouterr()
    assert PASTED not in captured.out
    assert PASTED not in captured.err


def test_connect_refuses_an_unusable_token_and_stores_nothing(pasting, capsys):
    pasting.enter("")

    assert main(["connect", "github"]) == 1

    assert get_credentials("github") is None
    assert load_config().tabs == ()
    assert "no token entered" in capsys.readouterr().err


def test_connecting_again_replaces_the_token_without_a_second_tab(pasting):
    """The whole re-authentication story for a token path: an expired tab says
    to run this, and running it is what fixes the tab."""
    main(["connect", "github"])
    pasting.enter("github_pat_22replacementtoken")

    assert main(["connect", "github"]) == 0

    stored = get_credentials("github")
    assert stored is not None
    assert stored.access_token == "github_pat_22replacementtoken"
    assert len(load_config().tabs) == 1


def test_status_of_a_token_tab_names_no_scope(pasting, capsys):
    """A pasted token was granted no scope through us; printing an empty one
    would read as a token that can do nothing."""
    main(["connect", "github"])
    capsys.readouterr()

    assert main(["status"]) == 0

    out = capsys.readouterr().out
    assert "github: connected — no expiry" in out


# --- Offering to add the running executable's directory to PATH ---


class _FakeTTY:
    """A stdin/stdout stand-in that reports as a real terminal and records what's written to it."""

    def __init__(self) -> None:
        self.written = ""

    def isatty(self) -> bool:
        return True

    def write(self, data: str) -> None:
        self.written += data

    def flush(self) -> None:
        pass


def _prepare_not_on_path(monkeypatch, tmp_path):
    """A `smorg` executable that isn't resolvable by name, with zsh as the shell.

    Returns the bin directory that needs adding to PATH and the fake stdout `_offer_path_setup`
    will print to. Patches sys.stdin/sys.stdout itself rather than through a fixture: pytest's
    capture machinery swaps those objects again at the setup-to-call phase boundary, which would
    silently discard a patch applied from fixture setup.
    """
    monkeypatch.setattr("sys.stdin", _FakeTTY())
    stdout = _FakeTTY()
    monkeypatch.setattr("sys.stdout", stdout)
    monkeypatch.setattr("smorg.core.path_setup.shutil.which", lambda name: None)

    executable = tmp_path / "opt" / "smorg" / "bin" / "smorg"
    executable.parent.mkdir(parents=True)
    executable.write_text("")
    monkeypatch.setattr("smorg.core.path_setup.sys.argv", [str(executable)])

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    return executable.parent, stdout


def test_offer_path_setup_yes_appends_the_export_line(monkeypatch, tmp_path):
    bin_dir, stdout = _prepare_not_on_path(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    _offer_path_setup()

    rc_file = Path.home() / ".zshrc"
    assert rc_file.read_text() == f'export PATH="{bin_dir}:$PATH"\n'
    assert "restart your shell" in stdout.written


def test_offer_path_setup_no_leaves_the_rc_file_untouched(monkeypatch, tmp_path):
    _prepare_not_on_path(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    _offer_path_setup()

    rc_file = Path.home() / ".zshrc"
    assert not rc_file.exists()


def test_offer_path_setup_yes_reports_a_write_failure_instead_of_crashing(monkeypatch, tmp_path):
    """append_once can raise OSError (an unwritable rc file); nothing above _offer_path_setup
    catches it, so it must handle its own failure rather than let it crash the CLI before the
    dashboard opens.
    """
    _prepare_not_on_path(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    stderr = _FakeTTY()
    monkeypatch.setattr("sys.stderr", stderr)

    tmp_path.chmod(0o500)  # HOME, and so the .zshrc directory: read/execute only, no write
    try:
        _offer_path_setup()
    finally:
        tmp_path.chmod(0o700)

    rc_file = Path.home() / ".zshrc"
    assert not rc_file.exists()
    assert "could not write" in stderr.written


STATIC_MANIFEST = Manifest(
    id="widget",
    display_name="Widget",
    connections=(
        AuthPath(
            id="oauth",
            method=OAuthMethod(
                provider=StaticProvider(
                    metadata=ServerMetadata(
                        authorization_endpoint="https://accounts.widget.invalid/authorize",
                        token_endpoint="https://accounts.widget.invalid/token",
                    ),
                    help_url="https://developer.widget.invalid/apps",
                    setup_hint="tick the widget api box",
                ),
                scopes=("read",),
            ),
        ),
    ),
    stale_after=timedelta(minutes=5),
    actions=(),
)


def test_connect_prompts_for_a_client_id_on_a_static_path(monkeypatch, capsys):
    monkeypatch.setattr(
        "smorg.cli.get_integration",
        lambda integration_id: SimpleNamespace(manifest=STATIC_MANIFEST),
    )
    logins: list[str | None] = []

    def fake_run_login(client, provider, client_id, **kwargs):
        logins.append(client_id)
        return (client_id, LIVE)

    monkeypatch.setattr("smorg.cli.run_login", fake_run_login)
    monkeypatch.setattr("builtins.input", lambda prompt="": "client-static")

    assert main(["connect", "widget"]) == 0

    out = capsys.readouterr().out
    assert "https://developer.widget.invalid/apps" in out
    assert "http://127.0.0.1:8765/callback" in out
    assert "tick the widget api box" in out
    assert logins == ["client-static"]
    expected = TabConfig(integration="widget", client_id="client-static", connection="oauth")
    assert load_config().tabs == (expected,)


def test_connect_refuses_an_empty_client_id_on_a_static_path(monkeypatch, capsys):
    monkeypatch.setattr(
        "smorg.cli.get_integration",
        lambda integration_id: SimpleNamespace(manifest=STATIC_MANIFEST),
    )
    logins: list[str | None] = []

    def fake_run_login(client, provider, client_id, **kwargs):
        logins.append(client_id)
        return (client_id, LIVE)

    monkeypatch.setattr("smorg.cli.run_login", fake_run_login)
    monkeypatch.setattr("builtins.input", lambda prompt="": "   ")

    assert main(["connect", "widget"]) == 1

    assert "a client id is required" in capsys.readouterr().err
    assert logins == []
    assert load_config().tabs == ()

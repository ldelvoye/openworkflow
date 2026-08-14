from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from keyring.errors import KeyringError

from oflow.auth.store import (
    CredentialPermissionError,
    Credentials,
    CredentialStoreError,
    InsecureBackendError,
    MalformedCredentialsError,
    delete_credentials,
    get_credentials,
    set_credentials,
)

CREDS = Credentials(
    access_token="secret-access-token",
    refresh_token="secret-refresh-token",
    expires_at=datetime(2026, 1, 1, tzinfo=UTC),
    scope="read",
)


@pytest.fixture
def file_store(tmp_path, monkeypatch):
    monkeypatch.setenv("OFLOW_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("OFLOW_CREDENTIAL_STORE", "file")
    return tmp_path / "cfg"


def test_repr_redacts_both_tokens():
    # str must stay aliased to repr — this also catches someone adding a
    # leaking __str__.
    for rendered in (repr(CREDS), str(CREDS)):
        assert "secret-access-token" not in rendered
        assert "secret-refresh-token" not in rendered
        assert "redacted" in rendered


def test_is_expired():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert CREDS.is_expired(now + timedelta(seconds=1)) is True
    assert CREDS.is_expired(now - timedelta(hours=1)) is False


def test_file_store_roundtrip(file_store):
    assert get_credentials("linear") is None
    set_credentials("linear", CREDS)
    assert get_credentials("linear") == CREDS


def test_file_store_writes_0600(file_store):
    set_credentials("linear", CREDS)
    assert (file_store / "credentials.json").stat().st_mode & 0o777 == 0o600


def test_file_store_rejects_wide_file_permissions(file_store):
    set_credentials("linear", CREDS)
    (file_store / "credentials.json").chmod(0o644)
    with pytest.raises(CredentialPermissionError):
        get_credentials("linear")


def test_file_store_rejects_wide_directory_permissions(file_store):
    set_credentials("linear", CREDS)
    file_store.chmod(0o755)
    with pytest.raises(CredentialPermissionError, match="755"):
        get_credentials("linear")


def test_wide_directory_is_not_silently_repaired(file_store):
    set_credentials("linear", CREDS)
    file_store.chmod(0o755)
    with pytest.raises(CredentialPermissionError):
        set_credentials("linear", CREDS)
    assert file_store.stat().st_mode & 0o777 == 0o755


def test_delete_removes_only_that_integration(file_store):
    set_credentials("linear", CREDS)
    set_credentials("sentry", CREDS)
    delete_credentials("linear")
    assert get_credentials("linear") is None
    assert get_credentials("sentry") == CREDS


def test_missing_access_token_raises_credential_store_error(file_store):
    set_credentials("linear", CREDS)
    path = file_store / "credentials.json"
    path.write_text('{"linear": {"scope": "read"}}')
    with pytest.raises(MalformedCredentialsError):
        get_credentials("linear")


def test_unparseable_expiry_raises_credential_store_error(file_store):
    set_credentials("linear", CREDS)
    path = file_store / "credentials.json"
    path.write_text('{"linear": {"access_token": "at", "expires_at": "not-a-date"}}')
    with pytest.raises(MalformedCredentialsError):
        get_credentials("linear")


def test_invalid_json_raises_credential_store_error(file_store):
    set_credentials("linear", CREDS)
    (file_store / "credentials.json").write_text("{not json")
    with pytest.raises(MalformedCredentialsError):
        get_credentials("linear")


def test_symlinked_credentials_file_is_refused(file_store, tmp_path):
    set_credentials("linear", CREDS)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("{}")
    elsewhere.chmod(0o600)
    path = file_store / "credentials.json"
    path.unlink()
    path.symlink_to(elsewhere)

    with pytest.raises(CredentialPermissionError, match="symlink"):
        get_credentials("linear")


def test_failed_write_leaves_the_previous_file_intact(file_store, monkeypatch):
    set_credentials("linear", CREDS)
    path = file_store / "credentials.json"
    before = path.read_text()

    monkeypatch.setattr(
        "oflow.auth.store.json.dumps", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with pytest.raises(RuntimeError):
        set_credentials("sentry", CREDS)

    assert path.read_text() == before
    assert not (file_store / "credentials.json.tmp").exists()


def test_serialised_credentials_never_leave_the_store_module():
    package = Path(__file__).parents[1] / "src" / "oflow"
    offenders = [
        module.relative_to(package).as_posix()
        for module in package.rglob("*.py")
        if module.name != "store.py" and "_to_dict" in module.read_text()
    ]
    assert offenders == [], f"_to_dict returns unredacted tokens; referenced by {offenders}"


def test_keyring_store_rejects_insecure_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("OFLOW_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("OFLOW_CREDENTIAL_STORE", raising=False)

    class FakeInsecureBackend:
        pass

    monkeypatch.setattr("oflow.auth.store.keyring.get_keyring", lambda: FakeInsecureBackend())
    with pytest.raises(InsecureBackendError) as excinfo:
        set_credentials("linear", CREDS)
    assert "OFLOW_CREDENTIAL_STORE=file" in str(excinfo.value)


def _fake_secure_backend() -> object:
    """An object that passes the secure-backend check without touching a real
    keychain, so a get/set/delete call actually reaches the fake below it."""

    class FakeSecureBackend:
        pass

    FakeSecureBackend.__module__ = "keyring.backends.macOS"
    FakeSecureBackend.__qualname__ = "Keyring"
    return FakeSecureBackend()


def test_keyring_backend_error_on_get_raises_credential_store_error(tmp_path, monkeypatch):
    monkeypatch.setenv("OFLOW_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("OFLOW_CREDENTIAL_STORE", raising=False)
    monkeypatch.setattr("oflow.auth.store.keyring.get_keyring", lambda: _fake_secure_backend())

    def raise_keyring_error(service, integration_id):
        raise KeyringError("the keychain is locked")

    monkeypatch.setattr("oflow.auth.store.keyring.get_password", raise_keyring_error)

    with pytest.raises(CredentialStoreError):
        get_credentials("linear")


def test_file_store_read_oserror_raises_credential_store_error(file_store, monkeypatch):
    set_credentials("linear", CREDS)

    def raise_oserror(self):
        raise OSError("permission denied")

    monkeypatch.setattr("oflow.auth.store.Path.read_text", raise_oserror)

    with pytest.raises(CredentialStoreError):
        get_credentials("linear")

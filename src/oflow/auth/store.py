"""The only module that reads or writes credentials.

Defaults to the OS keychain. An insecure keyring backend is refused rather than
used: keyring resolves to a failing backend and raises when nothing secure is
available, but an unrelated ``keyrings.alt`` install in the same environment
gives it an insecure backend to pick instead, turning that loud failure into a
plaintext token on disk.

The opt-in fallback store is not a substitute for a keychain: it is plaintext
JSON, protected only by mode 0600 inside a 0700 directory. Both are enforced on
every read, and a widened directory or file is reported rather than repaired.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import keyring
from keyring.errors import PasswordDeleteError

from oflow.config import (
    ConfigPermissionError,
    config_dir,
    ensure_config_dir,
    require_config_dir_permissions,
    require_private_path,
)

__all__ = [
    "CredentialPermissionError",
    "CredentialStoreError",
    "Credentials",
    "InsecureBackendError",
    "MalformedCredentialsError",
    "delete_credentials",
    "get_credentials",
    "now",
    "set_credentials",
]

STORE_ENV = "OFLOW_CREDENTIAL_STORE"
SERVICE = "oflow"
FILE_MODE = 0o600

StoredCredentials = dict[str, str | None]

SECURE_BACKENDS = frozenset(
    {
        "keyring.backends.macOS.Keyring",
        "keyring.backends.SecretService.Keyring",
        "keyring.backends.Windows.WinVaultKeyring",
        "keyring.backends.kwallet.DBusKeyring",
    }
)


class CredentialStoreError(Exception):
    """Base class for anything that stops credentials being read or written."""


class InsecureBackendError(CredentialStoreError):
    pass


class CredentialPermissionError(CredentialStoreError):
    pass


class MalformedCredentialsError(CredentialStoreError):
    """Stored credentials could not be read back into a Credentials."""


@dataclass(frozen=True)
class Credentials:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scope: str

    def __repr__(self) -> str:
        expiry = self.expires_at.isoformat() if self.expires_at else "never"
        return (
            "Credentials(access_token=<redacted>, refresh_token=<redacted>, "
            f"expires_at={expiry}, scope={self.scope!r})"
        )

    __str__ = __repr__

    def is_expired(self, now: datetime) -> bool:
        if self.expires_at is None:
            return False
        return now > self.expires_at


def now() -> datetime:
    return datetime.now(UTC)


def _backend_name(backend: object) -> str:
    return f"{type(backend).__module__}.{type(backend).__qualname__}"


def _require_secure_backend() -> None:
    name = _backend_name(keyring.get_keyring())
    if name not in SECURE_BACKENDS:
        raise InsecureBackendError(
            f"refusing to store tokens in {name}, which is not a known-secure keyring "
            f"backend. Set OFLOW_CREDENTIAL_STORE=file to use a 0600 file instead."
        )


def _use_file_store() -> bool:
    return os.environ.get(STORE_ENV) == "file"


def _credentials_path() -> Path:
    return config_dir() / "credentials.json"


def _require_directory() -> None:
    try:
        require_config_dir_permissions()
    except ConfigPermissionError as error:
        raise CredentialPermissionError(str(error)) from error


def _require_private(path: Path, expected_mode: int) -> None:
    try:
        require_private_path(path, expected_mode)
    except ConfigPermissionError as error:
        raise CredentialPermissionError(str(error)) from error


def _read_file_store() -> dict[str, StoredCredentials]:
    _require_directory()
    path = _credentials_path()
    if not path.exists() and not path.is_symlink():
        return {}
    _require_private(path, FILE_MODE)
    try:
        return json.loads(path.read_text())
    except ValueError as error:
        raise MalformedCredentialsError(f"{path} is not valid JSON") from error


def _write_file_store(payload: dict[str, StoredCredentials]) -> None:
    try:
        ensure_config_dir()
    except ConfigPermissionError as error:
        raise CredentialPermissionError(str(error)) from error
    path = _credentials_path()
    temporary = path.with_name(path.name + ".tmp")

    # Written to a temp file and renamed so an interrupted write cannot truncate
    # the live file: it holds the refresh token, and losing it means logging in
    # again. O_EXCL refuses a pre-existing file or a symlink planted at that
    # path, and the 0600 creation mode means the tokens are never briefly wider.
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        # The creation mode is masked by the process umask, which can only make
        # it narrower — narrow enough to fail our own read check. fchmod pins it.
        os.fchmod(descriptor, FILE_MODE)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(payload))
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(path)


def _to_dict(credentials: Credentials) -> StoredCredentials:
    """Serialise for storage. The returned dict holds raw tokens and, unlike
    Credentials, has no redacting repr — so it must never leave this module.
    A test enforces that no other module references it.
    """
    return {
        "access_token": credentials.access_token,
        "refresh_token": credentials.refresh_token,
        "expires_at": credentials.expires_at.isoformat() if credentials.expires_at else None,
        "scope": credentials.scope,
    }


def _from_dict(raw: StoredCredentials) -> Credentials:
    try:
        access_token = raw["access_token"]
        if not isinstance(access_token, str):
            raise TypeError("access_token must be a string")
        expires_at = raw.get("expires_at")
        return Credentials(
            access_token=access_token,
            refresh_token=raw.get("refresh_token"),
            expires_at=datetime.fromisoformat(expires_at) if expires_at else None,
            scope=raw.get("scope") or "",
        )
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedCredentialsError(
            f"stored credentials are unreadable ({error}); re-run oflow connect"
        ) from error


def get_credentials(integration_id: str) -> Credentials | None:
    if _use_file_store():
        raw = _read_file_store().get(integration_id)
        return _from_dict(raw) if raw else None
    _require_secure_backend()
    stored = keyring.get_password(SERVICE, integration_id)
    if stored is None:
        return None
    try:
        decoded = json.loads(stored)
    except ValueError as error:
        raise MalformedCredentialsError(
            f"keychain entry for {integration_id} is not valid JSON; re-run oflow connect"
        ) from error
    return _from_dict(decoded)


def set_credentials(integration_id: str, credentials: Credentials) -> None:
    if _use_file_store():
        payload = _read_file_store()
        payload[integration_id] = _to_dict(credentials)
        _write_file_store(payload)
        return
    _require_secure_backend()
    keyring.set_password(SERVICE, integration_id, json.dumps(_to_dict(credentials)))


def delete_credentials(integration_id: str) -> None:
    if _use_file_store():
        payload = _read_file_store()
        payload.pop(integration_id, None)
        _write_file_store(payload)
        return
    _require_secure_backend()
    try:
        keyring.delete_password(SERVICE, integration_id)
    except PasswordDeleteError:
        pass

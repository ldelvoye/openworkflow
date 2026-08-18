"""The only module that reads or writes credentials.

Defaults to the OS keychain. An insecure keyring backend is refused rather than
used — an unrelated ``keyrings.alt`` install can make what should be a loud
"no secure backend" failure silently pick a plaintext one instead.

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
from keyring.errors import KeyringError, PasswordDeleteError

from smorg.core.config import (
    FILE_MODE,
    ConfigPermissionError,
    config_dir,
    ensure_config_dir,
    require_config_dir_permissions,
    require_private_path,
    write_private_file,
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

STORE_ENV = "SMORG_CREDENTIAL_STORE"
SERVICE = "smorg"

# One integration's credentials as they are serialised. The store on disk is a
# mapping of integration id to one of these.
StoredCredential = dict[str, str | None]

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


def _to_dict(credentials: Credentials) -> StoredCredential:
    """Serialise for storage.

    Holds raw tokens with no redacting repr, unlike Credentials — must never
    leave this module. A test enforces that.
    """
    return {
        "access_token": credentials.access_token,
        "refresh_token": credentials.refresh_token,
        "expires_at": credentials.expires_at.isoformat() if credentials.expires_at else None,
        "scope": credentials.scope,
    }


def _from_dict(raw: StoredCredential) -> Credentials:
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
            f"stored credentials are unreadable ({error}); re-run smorg connect"
        ) from error


class _FileStore:
    """Plaintext JSON, guarded only by the permissions on it and its directory."""

    def get(self, integration_id: str) -> Credentials | None:
        raw = self._read().get(integration_id)
        return _from_dict(raw) if raw else None

    def set(self, integration_id: str, credentials: Credentials) -> None:
        payload = self._read()
        payload[integration_id] = _to_dict(credentials)
        self._write(payload)

    def delete(self, integration_id: str) -> None:
        payload = self._read()
        payload.pop(integration_id, None)
        self._write(payload)

    def _path(self) -> Path:
        return config_dir() / "credentials.json"

    def _read(self) -> dict[str, StoredCredential]:
        _translate_permission_error(require_config_dir_permissions)
        path = self._path()
        if not path.exists() and not path.is_symlink():
            return {}
        _translate_permission_error(lambda: require_private_path(path, FILE_MODE))
        # Callers branch on CredentialStoreError to decide whether to keep going;
        # a bare OSError (unreadable file, revoked permissions mid-session) must
        # degrade the same way a permission refusal does, not crash the caller.
        try:
            text = path.read_text()
        except OSError as error:
            raise CredentialStoreError(str(error)) from error
        try:
            return json.loads(text)
        except ValueError as error:
            raise MalformedCredentialsError(f"{path} is not valid JSON") from error

    def _write(self, payload: dict[str, StoredCredential]) -> None:
        _translate_permission_error(ensure_config_dir)
        # Atomic replace matters more here than for config: this file holds the
        # refresh token, so a truncated write costs a login.
        try:
            write_private_file(self._path(), json.dumps(payload))
        except OSError as error:
            raise CredentialStoreError(str(error)) from error


class _KeyringStore:
    """The OS keychain, entered under one account per integration.

    keyring.errors.KeyringError is not a CredentialStoreError, so every call
    into keyring is translated here — otherwise a locked keychain or a denied
    prompt crashes the caller instead of degrading like any other store failure.
    """

    def get(self, integration_id: str) -> Credentials | None:
        try:
            stored = keyring.get_password(SERVICE, integration_id)
        except KeyringError as error:
            raise CredentialStoreError(str(error)) from error
        if stored is None:
            return None
        try:
            decoded = json.loads(stored)
        except ValueError as error:
            raise MalformedCredentialsError(
                f"keychain entry for {integration_id} is not valid JSON; re-run smorg connect"
            ) from error
        return _from_dict(decoded)

    def set(self, integration_id: str, credentials: Credentials) -> None:
        try:
            keyring.set_password(SERVICE, integration_id, json.dumps(_to_dict(credentials)))
        except KeyringError as error:
            raise CredentialStoreError(str(error)) from error

    def delete(self, integration_id: str) -> None:
        try:
            keyring.delete_password(SERVICE, integration_id)
        except PasswordDeleteError:
            pass
        except KeyringError as error:
            raise CredentialStoreError(str(error)) from error


def _translate_permission_error(check) -> None:
    """Surface a config-layer permission refusal as this module's own error."""
    try:
        check()
    except ConfigPermissionError as error:
        raise CredentialPermissionError(str(error)) from error


def _backend_name(backend: object) -> str:
    return f"{type(backend).__module__}.{type(backend).__qualname__}"


def _require_secure_backend() -> None:
    name = _backend_name(keyring.get_keyring())
    if name not in SECURE_BACKENDS:
        raise InsecureBackendError(
            f"refusing to store tokens in {name}, which is not a known-secure keyring "
            f"backend. Set SMORG_CREDENTIAL_STORE=file to use a 0600 file instead."
        )


def _backend() -> _FileStore | _KeyringStore:
    """Pick the store, validating the keychain before anything reaches it."""
    if os.environ.get(STORE_ENV) == "file":
        return _FileStore()
    _require_secure_backend()
    return _KeyringStore()


def get_credentials(integration_id: str) -> Credentials | None:
    return _backend().get(integration_id)


def set_credentials(integration_id: str, credentials: Credentials) -> None:
    _backend().set(integration_id, credentials)


def delete_credentials(integration_id: str) -> None:
    _backend().delete(integration_id)

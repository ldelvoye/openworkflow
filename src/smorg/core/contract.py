"""What an integration must provide, and the errors it is allowed to raise."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

import httpx

from smorg.auth.oauth import ProviderConfig
from smorg.auth.store import Credentials
from smorg.auth.token import TokenPrompt
from smorg.core.keys import RESERVED_KEYS

if TYPE_CHECKING:
    from smorg.shell.panel import Panel


class ActionClass(StrEnum):
    """How far an action reaches.

    LOCAL -> our own state
    LAUNCH -> browser or clipboard
    REMOTE -> API (not implemented yet)
    """

    LOCAL = "local"
    LAUNCH = "launch"
    REMOTE = "remote"


@dataclass(frozen=True)
class Action:
    id: str
    # Written in its natural form (e.g. "Open in Linear"); each UI surface
    # applies its own casing convention at render time.
    label: str
    key: str
    action_class: ActionClass


@dataclass(frozen=True)
class Item:
    """The minimum the shell needs from any integration's data."""

    id: str
    updated_at: datetime
    url: str


@dataclass(frozen=True)
class ConnectionPath:
    id: str
    method: ProviderConfig | TokenPrompt


def _duplicates(values: Sequence[str]) -> list[str]:
    return sorted({value for value in values if values.count(value) > 1})


@dataclass(frozen=True)
class Manifest:
    id: str
    display_name: str
    connections: tuple[ConnectionPath, ...]
    stale_after: timedelta
    actions: tuple[Action, ...]

    def __post_init__(self) -> None:
        keys = [action.key for action in self.actions]
        duplicates = _duplicates(keys)
        if duplicates:
            raise ValueError(f"duplicate action key(s) in {self.id}: {duplicates}")
        reserved = sorted(set[str](keys) & RESERVED_KEYS)
        if reserved:
            raise ValueError(
                f"{self.id} binds reserved shell key(s) {reserved}; "
                f"panels may add keys, not rebind global ones"
            )
        if not self.connections:
            raise ValueError(f"{self.id} declares no connection path; it could never connect")
        connection_ids = [connection.id for connection in self.connections]
        duplicate_connections = _duplicates(connection_ids)
        if duplicate_connections:
            raise ValueError(
                f"duplicate connection path id(s) in {self.id}: {duplicate_connections}"
            )

    def connection(self, chosen: str | None) -> ConnectionPath:
        """Resolve a config-recorded path id to its declaration; None means
        "nothing recorded yet" and returns the first declared path. The
        single resolver every call site uses, instead of indexing connections
        directly."""
        if chosen is None:
            return self.connections[0]
        for path in self.connections:
            if path.id == chosen:
                return path
        declared = ", ".join(path.id for path in self.connections)
        raise ValueError(f"{self.id} declares no connection path {chosen!r}; has: {declared}")


class IntegrationError(Exception):
    """Base class for every failure a source may surface to the shell."""


class AccessNotAllowed(IntegrationError):
    """Credentials (usually from token-based auth) is not allowed access to the requested data.
    Last-good data is kept and no re-connect is offered.
    """


class AuthExpired(IntegrationError):
    """Credentials are no longer valid. The shell offers an inline re-connect."""


class Unavailable(IntegrationError):
    """The service could not be reached. Last-good data is kept and marked stale."""


class Malformed(IntegrationError):
    """The response did not match the expected shape. The tab is broken; say so."""


class Integration(Protocol):
    # A property rather than an attribute so the protocol is read-only, which
    # frozen dataclasses satisfy. Nothing assigns a manifest; it is a
    # declaration, not state.
    @property
    def manifest(self) -> Manifest: ...

    @property
    def panel_class(self) -> type[Panel]:
        """The widget class the shell mounts for this integration's tab."""
        ...

    def fetch(self, credentials: Credentials, http: httpx.Client) -> Sequence[Item]:
        """Return the integration's items. Raises IntegrationError, never anything else."""
        ...

    def fetch_detail(self, credentials: Credentials, http: httpx.Client, item: Item) -> object:
        """One item's expanded detail, in whatever shape this integration's
        panel renders. The shell never inspects it. Raises IntegrationError,
        never anything else."""
        ...

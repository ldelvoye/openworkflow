"""What an integration must provide, and the errors it is allowed to raise.

Provisional. This has exactly one consumer, and a contract shaped against a
single implementation is reliably wrong for the second — so it carries nothing
that the first integration does not need. Generalise when there are two.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Protocol

from oflow.auth.oauth import ProviderConfig

# Bound by the shell as priority bindings, which Textual checks ahead of the
# focused widget. A panel that declared one of these would be silently ignored,
# so the manifest rejects it outright instead.
RESERVED_KEYS = frozenset[str]({"r", "q", "?", "tab", "escape", "j", "k", "enter"})


class ActionClass(StrEnum):
    """How far an action reaches, which is the whole safety boundary.

    LOCAL touches only our own state, LAUNCH hands off to the browser or
    clipboard, REMOTE writes to somebody's API. Only the first two are
    implemented; REMOTE exists so adding one later is a declaration rather than
    a retrofit.
    """

    LOCAL = "local"
    LAUNCH = "launch"
    REMOTE = "remote"


@dataclass(frozen=True)
class Action:
    id: str
    label: str
    key: str
    action_class: ActionClass


@dataclass(frozen=True)
class Manifest:
    id: str
    display_name: str
    provider: ProviderConfig
    stale_after: timedelta
    actions: tuple[Action, ...]

    def __post_init__(self) -> None:
        keys = [action.key for action in self.actions]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"duplicate action key(s) in {self.id}: {duplicates}")
        reserved = sorted(set[str](keys) & RESERVED_KEYS)
        if reserved:
            raise ValueError(
                f"{self.id} binds reserved shell key(s) {reserved}; "
                f"panels may add keys, not rebind global ones"
            )


class IntegrationError(Exception):
    """Base class for every failure a source may surface to the shell."""


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

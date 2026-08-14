"""What an integration must provide, and the errors it is allowed to raise.

Provisional. This has exactly one consumer, and a contract shaped against a
single implementation is reliably wrong for the second — so it carries nothing
that the first integration does not need. Generalise when there are two.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

import httpx

from oflow.auth.oauth import ProviderConfig
from oflow.auth.store import Credentials

if TYPE_CHECKING:
    # Deferred: shell.panel imports this module for Item, so a real import here
    # would be circular. Safe under annotations-as-strings (see __future__ import
    # above) since the name is only ever used in a type position.
    from oflow.shell.panel import Panel

# Exactly the keys the shell binds, plus escape (HelpOverlay's own binding to
# dismiss itself — a ModalScreen's bindings take precedence over the app's).
# The rest are checked ahead of the focused widget via priority=True. Either
# way, a panel that declared one of these would be silently ignored, so the
# manifest rejects it outright instead. Reserve a key only once something
# actually binds it; j, k, tab, and enter are free until a future shell
# binding needs one back (e.g. a detail pane re-adding enter).
RESERVED_KEYS = frozenset[str]({"r", "q", "?", "escape", "shift+left", "shift+right"})


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
class Item:
    """The minimum the shell needs from any integration's data.

    Change highlighting keys off updated_at and the launch action opens url, so
    those two plus an identity are the whole shared vocabulary. Everything a
    panel draws beyond this belongs to the integration that defined it — a
    shared type carrying every field an integration might want would undo the
    point of per-integration rendering.
    """

    id: str
    updated_at: datetime
    url: str


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

    @property
    def panel_class(self) -> type[Panel]:
        """The widget class the shell mounts for this integration's tab."""
        ...

    def fetch(self, credentials: Credentials, http: httpx.Client) -> Sequence[Item]:
        """Return the integration's items. Raises IntegrationError, never anything else."""
        ...

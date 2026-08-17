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
    # Deferred: shell.panel imports this module for Item, so a real import
    # here would be circular; safe since the name is only used in a type
    # position.
    from oflow.shell.panel import Panel


@dataclass(frozen=True)
class ShellKey:
    """One shell-level key binding, declared once so shell/app.py's BINDINGS
    and this module's RESERVED_KEYS cannot drift apart. Fields mirror the
    Binding constructor arguments they end up as (see shell/app.py).
    """

    key: str
    action: str
    description: str
    key_display: str | None = None
    show: bool = True


# The shell's own keymap, checked ahead of the focused widget via
# priority=True. shift+right carries the merged key_display for both
# directions so the footer shows one "switch tab" entry instead of two.
SHELL_KEYS = (
    ShellKey("shift+left", "previous_tab", "switch tab", show=False),
    ShellKey("shift+right", "next_tab", "switch tab", key_display="⇧ + ←/→"),
    ShellKey("r", "refresh", "refresh"),
    ShellKey("m", "mark_all_seen", "mark all seen"),
    ShellKey("question_mark", "help", "help"),
    ShellKey("q", "quit", "quit"),
)

# Textual's binding name for this key ("question_mark") differs from the
# character a manifest would rebind ("?") — the only shell key where they
# do, so the mapping is spelled out rather than derived.
_MANIFEST_KEY_OVERRIDES = {"question_mark": "?"}

# Every key the shell binds, plus escape (HelpOverlay's own dismiss key). A
# manifest binding one of these would be silently ignored, so it is
# rejected outright instead.
RESERVED_KEYS = frozenset[str](
    _MANIFEST_KEY_OVERRIDES.get(shell_key.key, shell_key.key) for shell_key in SHELL_KEYS
) | {"escape"}


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
    # Written in its natural form (e.g. "Open in Linear"); each UI surface
    # applies its own casing convention at render time.
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

    def fetch_detail(self, credentials: Credentials, http: httpx.Client, item: Item) -> object:
        """One item's expanded detail, in whatever shape this integration's
        panel renders. The shell never inspects it. Raises IntegrationError,
        never anything else."""
        ...

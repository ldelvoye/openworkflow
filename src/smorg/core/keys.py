"""The shell's keymap: declared once so shell/app.py's BINDINGS and this
module's RESERVED_KEYS cannot drift apart. A manifest may add keys of its
own; it may not rebind any key reserved here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ShellKey:
    """One shell-level key binding. Fields mirror the Binding constructor
    arguments they end up as (see shell/app.py).
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

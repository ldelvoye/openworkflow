"""Making text that arrived over the network safe to put on a screen."""

from __future__ import annotations


def printable(value: str, limit: int = 120) -> str:
    """Strip control characters and cap length.

    Anything a remote party controls eventually reaches a terminal, where escape
    sequences are executed rather than displayed, and an unbounded string can
    push the rest of the interface off screen.
    """
    cleaned = "".join(character for character in value if character.isprintable())
    return cleaned[:limit] or "(unspecified)"


def printable_block(value: str, limit: int | None = 4000) -> str:
    """printable() for multi-line text: newlines survive, every line is
    sanitized on its own, and the cap applies to the whole block. Empty stays
    empty — the caller decides what an absent description reads as.

    limit=None skips capping entirely, so a caller that needs to run further
    text-shape-sensitive processing (e.g. unwrapping paired tags) before
    capping can sanitize first without truncating mid-tag.
    """
    raw_lines = value.split("\n")
    lines = (
        "".join(character for character in line if character.isprintable()) for line in raw_lines
    )
    sanitized = "\n".join(lines)
    return sanitized if limit is None else sanitized[:limit]


def capped(value: str, limit: int) -> str:
    """Truncate to `limit` characters, appending a visible marker when it
    cuts — an unmarked cut reads as if the text just ends there, which is
    indistinguishable from a real ending and from a cut landing mid-word or
    mid-tag.
    """
    if len(value) <= limit:
        return value
    return value[:limit] + "\n\n… (truncated)"

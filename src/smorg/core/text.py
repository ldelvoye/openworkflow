"""Making text that arrived over the network safe to put on a screen."""

from __future__ import annotations


def _strip_unprintable(text: str) -> str:
    return "".join(character for character in text if character.isprintable())


def sanitize_line(value: str, limit: int = 120) -> str:
    trimmed = _strip_unprintable(value)[:limit]
    if not trimmed:
        return "(unspecified)"
    return trimmed


def sanitize_block(value: str, limit: int | None = 4000) -> str:
    """`sanitize_line()` for multi-line text: each line sanitized on its own, newlines kept, the cap
    applied to the whole block; empty stays empty.

    limit=None skips capping, for a caller that must sanitize before its own shape-sensitive
    processing (e.g. unwrapping paired tags).
    """
    lines = [_strip_unprintable(line) for line in value.split("\n")]
    sanitized = "\n".join(lines)
    if limit is None:
        return sanitized
    return sanitized[:limit]


def truncate(value: str, limit: int) -> str:
    """value truncated to `limit` characters, with a visible marker when it cuts; without one a
    cut would read as the real ending.
    """
    if len(value) <= limit:
        return value
    return value[:limit] + "\n\n… (truncated)"

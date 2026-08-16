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


def printable_block(value: str, limit: int = 4000) -> str:
    """printable() for multi-line text: newlines survive, every line is
    sanitized on its own, and the cap applies to the whole block. Empty stays
    empty — the caller decides what an absent description reads as.
    """
    lines = (
        "".join(character for character in line if character.isprintable())
        for line in value.split("\n")
    )
    return "\n".join(lines)[:limit]

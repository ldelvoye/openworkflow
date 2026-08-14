"""The terminal's real colors, so a screenshot shows what the user sees.

oflow renders under Textual's "ansi-dark" theme, so on-screen colors resolve
through the terminal's own palette; Textual's SVG export does not, and falls
back to a fixed generic mapping instead. This module learns the real palette
via OSC 4/10/11 so export_screenshot can use it. Queried once, in cli._run,
strictly before OflowApp exists — see query_terminal_palette below.
"""

from __future__ import annotations

import os
import re
import select
import sys
import time
from dataclasses import dataclass
from typing import Protocol

from rich.terminal_theme import TerminalTheme

RGB = tuple[int, int, int]

_QUERY_TIMEOUT_SECONDS = 0.3

# Batched into one write (16 OSC4 slots + OSC10/11) for a single round trip.
# ST-terminated; _RESPONSE below also accepts a BEL-terminated reply.
_QUERY = "".join(f"\x1b]4;{i};?\x1b\\" for i in range(16)) + "\x1b]10;?\x1b\\" + "\x1b]11;?\x1b\\"

_RESPONSE = re.compile(
    r"\x1b\](?P<code>\d+);(?:(?P<index>\d+);)?rgb:"
    r"(?P<r>[0-9a-fA-F]{1,4})/(?P<g>[0-9a-fA-F]{1,4})/(?P<b>[0-9a-fA-F]{1,4})"
    r"(?:\x1b\\|\x07)"
)


def _scale(hex_digits: str) -> int:
    """An OSC color component is 1-4 hex digits scaled to its own width, not 0-255."""
    maximum = 16 ** len(hex_digits) - 1
    return round(int(hex_digits, 16) * 255 / maximum)


@dataclass(frozen=True)
class TerminalPalette:
    """The terminal's real background, foreground, and 16 ANSI colors."""

    background: RGB
    foreground: RGB
    ansi: tuple[RGB, ...]
    """Exactly 16 entries: normal 0-7, then bright 8-15."""

    def to_terminal_theme(self) -> TerminalTheme:
        """Rich's mapping type, consumed by Console.export_svg's `theme=`."""
        return TerminalTheme(
            self.background, self.foreground, list(self.ansi[:8]), list(self.ansi[8:])
        )


def parse_palette(data: str) -> TerminalPalette | None:
    """Parse OSC 4/10/11 responses into a palette. Pure and side-effect free
    (fed a recorded string, not a live terminal) so it is cheaply testable.

    Returns None unless background, foreground, and all 16 ANSI slots were
    found — a partial answer is treated as no answer, since a screenshot with
    some colors real and others guessed would misinform more than one
    consistently using the fallback mapping.
    """
    colors: dict[str, RGB] = {}
    for match in _RESPONSE.finditer(data):
        rgb = (_scale(match["r"]), _scale(match["g"]), _scale(match["b"]))
        code = match["code"]
        if code == "10":
            colors["foreground"] = rgb
        elif code == "11":
            colors["background"] = rgb
        elif code == "4" and match["index"] is not None:
            colors[match["index"]] = rgb

    if "background" not in colors or "foreground" not in colors:
        return None
    try:
        ansi = tuple(colors[str(index)] for index in range(16))
    except KeyError:
        return None
    return TerminalPalette(
        background=colors["background"], foreground=colors["foreground"], ansi=ansi
    )


def _read_until_timeout(fd: int, timeout: float) -> str:
    """Collect bytes for up to `timeout` seconds, stopping early once a full
    palette has already been parsed out of what arrived so far.
    """
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            break
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        chunks.append(chunk)
        if parse_palette(b"".join(chunks).decode("ascii", "replace")) is not None:
            break
    return b"".join(chunks).decode("ascii", "replace")


class _TTYStream(Protocol):
    """The subset of a stream this module needs from stdin/stdout."""

    def isatty(self) -> bool: ...
    def fileno(self) -> int: ...
    def write(self, data: str, /) -> object: ...
    def flush(self) -> object: ...


def query_terminal_palette(
    timeout: float = _QUERY_TIMEOUT_SECONDS,
    *,
    stdin: _TTYStream | None = None,
    stdout: _TTYStream | None = None,
) -> TerminalPalette | None:
    """Ask the terminal for its real palette.

    Must run before OflowApp exists, never once Textual's driver owns stdin:
    it has no notion of an OSC response, so it replays one as synthetic
    keypresses byte-by-byte — the "r" in an "rgb:" reply would fire this
    app's refresh binding. Never raises: returns None when the palette can't
    be learned; `timeout` bounds the wait so a silent terminal falls back
    instead of hanging startup.

    `stdin`/`stdout` default to sys.stdin/sys.stdout at call time; the
    keyword seam exists because pytest's capture manager owns those globals
    during a test run, so exercising this path under test requires passing
    fakes in rather than patching sys.stdin/sys.stdout.
    """
    resolved_stdin: _TTYStream = sys.stdin if stdin is None else stdin
    resolved_stdout: _TTYStream = sys.stdout if stdout is None else stdout
    if not (resolved_stdin.isatty() and resolved_stdout.isatty()):
        return None
    try:
        import termios
        import tty
    except ImportError:
        # Not POSIX (e.g. Windows has neither module) — no safe way to flip
        # the tty into raw mode here, so there is nothing more to try.
        return None

    fd = resolved_stdin.fileno()
    try:
        original = termios.tcgetattr(fd)
    except (termios.error, OSError):
        return None

    try:
        tty.setraw(fd)
        resolved_stdout.write(_QUERY)
        resolved_stdout.flush()
        response = _read_until_timeout(fd, timeout)
    except (termios.error, OSError):
        return None
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, original)
        except (termios.error, OSError):
            pass  # best-effort restore: a failing restore must not crash startup

    return parse_palette(response)

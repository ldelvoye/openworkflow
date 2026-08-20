"""The terminal's real colors, so a screenshot shows what the user sees."""

from __future__ import annotations

import os
import re
import select
import sys
import time
from dataclasses import dataclass
from math import ceil
from typing import Protocol

from rich.terminal_theme import TerminalTheme

RGB = tuple[int, int, int]

_QUERY_TIMEOUT_SECONDS = 0.3

BLACK: RGB = (0, 0, 0)
WHITE: RGB = (255, 255, 255)

MINIMUM_CONTRAST_RATIO = 4.5
"""W3C AA for body text, and the default of VS Code/Cursor's
`terminal.integrated.minimumContrastRatio`."""

# The terminal lifts a color in 10% steps.
_LIFT_STEP = 0.1

# Batched into one write (16 OSC4 slots + OSC10/11) for a single round trip. ST-terminated;
# _RESPONSE below also accepts a BEL-terminated reply.
_QUERY = "".join(f"\x1b]4;{i};?\x1b\\" for i in range(16)) + "\x1b]10;?\x1b\\" + "\x1b]11;?\x1b\\"

_RESPONSE = re.compile(
    r"\x1b\](?P<code>\d+);(?:(?P<index>\d+);)?rgb:"
    r"(?P<r>[0-9a-fA-F]{1,4})/(?P<g>[0-9a-fA-F]{1,4})/(?P<b>[0-9a-fA-F]{1,4})"
    r"(?:\x1b\\|\x07)"
)


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


def _linear(component: int) -> float:
    """One sRGB channel, gamma removed, as the W3C luminance formula wants it."""
    fraction = component / 255
    if fraction <= 0.03928:
        return fraction / 12.92
    return ((fraction + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: RGB) -> float:
    """W3C relative luminance: 0.0 for black, 1.0 for white."""
    red, green, blue = rgb
    return 0.2126 * _linear(red) + 0.7152 * _linear(green) + 0.0722 * _linear(blue)


def contrast_ratio(one: RGB, other: RGB) -> float:
    """W3C contrast ratio: 1.0 for two identical colors, 21.0 black on white."""
    luminances = (relative_luminance(one), relative_luminance(other))
    return (max(luminances) + 0.05) / (min(luminances) + 0.05)


def _toward_black(rgb: RGB) -> RGB:
    red, green, blue = rgb
    return (
        red - ceil(red * _LIFT_STEP),
        green - ceil(green * _LIFT_STEP),
        blue - ceil(blue * _LIFT_STEP),
    )


def _toward_white(rgb: RGB) -> RGB:
    red, green, blue = rgb
    return (
        red + ceil((255 - red) * _LIFT_STEP),
        green + ceil((255 - green) * _LIFT_STEP),
        blue + ceil((255 - blue) * _LIFT_STEP),
    )


def ensure_contrast(foreground: RGB, background: RGB) -> RGB:
    if contrast_ratio(foreground, background) >= MINIMUM_CONTRAST_RATIO:
        return foreground
    darken = contrast_ratio(BLACK, background) > contrast_ratio(WHITE, background)
    if darken:
        step = _toward_black
        limit = BLACK
    else:
        step = _toward_white
        limit = WHITE
    # Keep nudhing the color until readable
    adjusted = foreground
    while adjusted != limit and contrast_ratio(adjusted, background) < MINIMUM_CONTRAST_RATIO:
        adjusted = step(adjusted)
    return adjusted


def ensure_theme_contrast(theme: TerminalTheme) -> TerminalTheme:
    background: RGB = theme.background_color
    foreground = ensure_contrast(theme.foreground_color, background)
    ansi = [ensure_contrast(theme.ansi_colors[index], background) for index in range(16)]
    return TerminalTheme(background, foreground, ansi[:8], ansi[8:])


def _scale(hex_digits: str) -> int:
    """An OSC color component is 1-4 hex digits scaled to its own width, not 0-255."""
    maximum = 16 ** len(hex_digits) - 1
    return round(int(hex_digits, 16) * 255 / maximum)


def parse_palette(data: str) -> TerminalPalette | None:
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
    """Ask the terminal for its real palette. Must run before SmorgApp exists"""
    resolved_stdin: _TTYStream = sys.stdin if stdin is None else stdin
    resolved_stdout: _TTYStream = sys.stdout if stdout is None else stdout
    if not (resolved_stdin.isatty() and resolved_stdout.isatty()):
        return None
    try:
        import termios
        import tty
    except ImportError:
        # Not POSIX (Windows lacks both modules) — no safe way to set raw mode, so give up here.
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

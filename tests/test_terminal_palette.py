import os
import select
import termios
from functools import partial
from types import SimpleNamespace

import pytest

from smorg.shell.terminal_palette import (
    MINIMUM_CONTRAST_RATIO,
    TerminalPalette,
    contrast_ratio,
    ensure_contrast,
    ensure_theme_contrast,
    parse_palette,
    query_terminal_palette,
)


def _osc4(index: int, hex_component: str = "1a1a") -> str:
    return f"\x1b]4;{index};rgb:{hex_component}/{hex_component}/{hex_component}\x1b\\"


def _ansi_slots() -> str:
    # 16 ANSI slots, each a distinct shade so slot order is verifiable.
    return "".join(_osc4(index, f"{index:02x}{index:02x}") for index in range(16))


def _full_response() -> str:
    # A real terminal's answer to the batched query: all 16 slots plus
    # foreground/background.
    return _ansi_slots() + "\x1b]10;rgb:eeee/eeee/eeee\x1b\\" + "\x1b]11;rgb:1111/1111/1111\x1b\\"


def test_parses_a_complete_response_into_background_foreground_and_16_slots():
    palette = parse_palette(_full_response())

    assert palette is not None
    assert palette.foreground == (238, 238, 238)
    assert palette.background == (17, 17, 17)
    assert len(palette.ansi) == 16
    # Spot-check ordering: slot 0 is darkest, slot 15 is the 16-bit-scaled max
    # of 0x0f0f, both distinguishable from every other slot's shade.
    assert palette.ansi[0] == (0, 0, 0)
    assert palette.ansi[15] == (round(0x0F0F * 255 / 0xFFFF),) * 3


def test_scales_two_digit_and_four_digit_hex_components_to_0_255():
    # Some terminals answer with 4 hex digits per component (16-bit), others
    # with 2 (8-bit) — both must land on the same 0-255 scale.
    two_digit = parse_palette(
        _ansi_slots() + "\x1b]11;rgb:ff/80/00\x1b\\" + "\x1b]10;rgb:ffff/ffff/ffff\x1b\\"
    )
    assert two_digit is not None
    assert two_digit.background == (255, 128, 0)
    assert two_digit.foreground == (255, 255, 255)


def test_accepts_bel_terminated_responses_alongside_st_terminated_ones():
    bel_style = _full_response().replace("\x1b\\", "\x07")
    assert parse_palette(bel_style) is not None


def test_returns_none_for_a_response_missing_background():
    slots = "".join(_osc4(index) for index in range(16))
    incomplete = slots + "\x1b]10;rgb:eeee/eeee/eeee\x1b\\"  # no OSC 11

    assert parse_palette(incomplete) is None


def test_returns_none_for_a_response_missing_an_ansi_slot():
    # Only 15 of the 16 slots answered — a partial palette is not usable.
    slots = "".join(_osc4(index) for index in range(15))
    incomplete = slots + "\x1b]10;rgb:eeee/eeee/eeee\x1b\\" + "\x1b]11;rgb:1111/1111/1111\x1b\\"

    assert parse_palette(incomplete) is None


def test_returns_none_for_silence_or_garbage():
    # A terminal with no OSC support at all might send nothing back, or echo
    # stray bytes — none of it should parse as a palette.
    assert parse_palette("") is None
    assert parse_palette("not an escape sequence at all") is None


def test_ignores_unrelated_bytes_interleaved_with_a_valid_response():
    noisy = "\x1b[24;80R" + _full_response() + "some stray terminal noise"
    assert parse_palette(noisy) is not None


def test_to_terminal_theme_splits_normal_and_bright_at_slot_eight():
    palette = TerminalPalette(
        background=(1, 2, 3),
        foreground=(4, 5, 6),
        ansi=tuple((index, index, index) for index in range(16)),
    )

    theme = palette.to_terminal_theme()

    assert theme.background_color.rgb == "rgb(1,2,3)"
    assert theme.foreground_color.rgb == "rgb(4,5,6)"
    # ansi_colors is a Palette of all 16, normal (0-7) followed by bright (8-15).
    assert theme.ansi_colors[0].rgb == "rgb(0,0,0)"
    assert theme.ansi_colors[8].rgb == "rgb(8,8,8)"


# --- The readability floor a screenshot has to clear ---

# Cursor's cream terminal background, the case that motivated the floor.
CREAM = (0xFB, 0xF5, 0xDF)


@pytest.mark.parametrize(
    "foreground,background",
    [
        ((0, 0, 0), (255, 255, 255)),  # the maximum, 21:1
        ((0xF4, 0x00, 0x5F), (0x0C, 0x0C, 0x0C)),  # 4.67:1, just over the floor
    ],
)
def test_a_foreground_that_already_reads_well_is_returned_untouched(foreground, background):
    assert ensure_contrast(foreground, background) == foreground


@pytest.mark.parametrize(
    "foreground,background",
    [
        ((0xC1, 0xC3, 0xC7), CREAM),  # pale grey on cream: 1.62:1
        ((0xD7, 0xD1, 0x1F), CREAM),  # ANSI yellow on cream: 1.48:1
        ((0, 0, 0), (10, 20, 30)),  # black on near-black: brighten, not darken
        ((128, 128, 128), (128, 128, 128)),  # the worst case: no contrast at all
    ],
)
def test_an_unreadable_foreground_is_lifted_until_it_clears_the_floor(foreground, background):
    lifted = ensure_contrast(foreground, background)

    assert contrast_ratio(lifted, background) >= MINIMUM_CONTRAST_RATIO


def test_lifting_moves_luminance_without_repainting_the_color():
    # A floor met by turning everything grey would pass the test above while
    # throwing away the palette the screenshot exists to show.
    red, green, blue = ensure_contrast((0xD7, 0xD1, 0x1F), CREAM)

    assert red > blue and green > blue  # still yellow, not grey


def test_a_lifted_theme_keeps_its_background_and_clears_the_floor_everywhere():
    unreadable = TerminalPalette(
        background=(0x0C, 0x0C, 0x0C),
        foreground=(0x1A, 0x1A, 0x1A),
        ansi=tuple((index, index, index) for index in range(16)),
    ).to_terminal_theme()

    lifted = ensure_theme_contrast(unreadable)

    background = lifted.background_color
    assert background == unreadable.background_color
    colors = [lifted.foreground_color] + [lifted.ansi_colors[index] for index in range(16)]
    worst = min(contrast_ratio(color, background) for color in colors)
    assert worst >= MINIMUM_CONTRAST_RATIO


def test_query_terminal_palette_returns_none_without_a_real_terminal_on_both_ends(monkeypatch):
    # This is pytest's own environment (stdin/stdout are not a tty), exercised
    # explicitly rather than relied upon implicitly, so the fallback holds
    # regardless of how the suite happens to be invoked. Live palette
    # detection against a real terminal is not something this suite can
    # exercise — see the owner's visual pass in the report.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    assert query_terminal_palette() is None


# --- The raw-mode query/restore path, with termios faked and a real pipe fd ---


class _FakeTTY:
    """Stands in for sys.stdin/sys.stdout: reports as a tty, and (for stdin)
    exposes a real fd — one end of a pipe — so select/os.read behave exactly
    as they would against a real terminal.
    """

    def __init__(self, fd: int | None = None) -> None:
        self._fd = fd
        self.written: list[str] = []

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        assert self._fd is not None
        return self._fd

    def write(self, data: str) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass


@pytest.fixture
def raw_mode(monkeypatch):
    """A pipe fd is not a real tty, so the termios/tty calls are faked; select
    and os.read run for real against the pipe, exercising the actual timing
    and buffering behavior query_terminal_palette relies on.

    The fakes are passed to query_terminal_palette via its stdin/stdout
    kwargs rather than patched onto sys.stdin/sys.stdout: pytest's capture
    manager reassigns those globals between fixture setup and the test body,
    which would clobber a sys-level patch.
    """
    read_fd, write_fd = os.pipe()
    stdin = _FakeTTY(read_fd)
    stdout = _FakeTTY()

    original = object()  # sentinel standing in for "whatever tcgetattr returned"
    restore_calls: list[tuple[int, int, object]] = []
    monkeypatch.setattr("termios.tcgetattr", lambda fd: original)
    monkeypatch.setattr(
        "termios.tcsetattr", lambda fd, when, attrs: restore_calls.append((fd, when, attrs))
    )
    monkeypatch.setattr("tty.setraw", lambda fd: None)

    yield SimpleNamespace(
        read_fd=read_fd,
        write_fd=write_fd,
        original=original,
        restore_calls=restore_calls,
        query=partial(query_terminal_palette, stdin=stdin, stdout=stdout),
    )

    os.close(read_fd)
    os.close(write_fd)


def test_restores_original_termios_settings_after_a_successful_query(raw_mode):
    os.write(raw_mode.write_fd, _full_response().encode("ascii"))

    palette = raw_mode.query(timeout=0.2)

    assert palette is not None
    assert raw_mode.restore_calls == [(raw_mode.read_fd, termios.TCSADRAIN, raw_mode.original)]


def test_restores_original_termios_settings_on_a_timeout_with_no_response(raw_mode):
    palette = raw_mode.query(timeout=0.05)

    assert palette is None
    assert raw_mode.restore_calls == [(raw_mode.read_fd, termios.TCSADRAIN, raw_mode.original)]


def test_restores_original_termios_settings_when_reading_raises_mid_query(raw_mode, monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("simulated read failure")

    monkeypatch.setattr(select, "select", _boom)

    palette = raw_mode.query(timeout=0.2)

    assert palette is None
    assert raw_mode.restore_calls == [(raw_mode.read_fd, termios.TCSADRAIN, raw_mode.original)]


def test_a_termios_error_from_setraw_returns_none_instead_of_raising(raw_mode, monkeypatch):
    # Regression: tty.setraw raises termios.error, not OSError, on failure.
    def _boom(fd):
        raise termios.error("simulated setraw failure")

    monkeypatch.setattr("tty.setraw", _boom)

    palette = raw_mode.query(timeout=0.2)

    assert palette is None
    # The finally-restore still ran even though setraw failed first.
    assert raw_mode.restore_calls == [(raw_mode.read_fd, termios.TCSADRAIN, raw_mode.original)]


def test_a_failing_restore_does_not_crash_or_swallow_a_successful_result(raw_mode, monkeypatch):
    os.write(raw_mode.write_fd, _full_response().encode("ascii"))

    def _boom(*args, **kwargs):
        raise termios.error("simulated restore failure")

    monkeypatch.setattr("termios.tcsetattr", _boom)

    palette = raw_mode.query(timeout=0.2)

    assert palette is not None

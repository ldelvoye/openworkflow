"""Transient staged feedback for the refresh keybind.

Only the r key shows this; tab-switch and focus refreshes stay silent —
feedback answers "did my keypress register?", which those never ask.
"""

from __future__ import annotations

from enum import StrEnum

from rich.text import Text
from textual.timer import Timer
from textual.widgets import Static


class RefreshStage(StrEnum):
    """The stages a keybind-triggered refresh passes through, in order.
    FAILED ends the sequence early; the panel itself shows the error."""

    CONNECTING = "connecting"
    FETCHING = "fetching"
    DONE = "done"
    FAILED = "failed"


DONE_LINGER_SECONDS = 1.0

_LABELS = {
    RefreshStage.CONNECTING: "connecting…",
    RefreshStage.FETCHING: "fetching…",
    RefreshStage.DONE: "refreshed",
}
_FILLED_CELLS = {
    RefreshStage.CONNECTING: 1,
    RefreshStage.FETCHING: 2,
    RefreshStage.DONE: 3,
}
_TOTAL_CELLS = 3


def _stage_text(stage: RefreshStage) -> Text:
    filled = _FILLED_CELLS[stage]
    bar = "▰" * filled + "▱" * (_TOTAL_CELLS - filled)
    return Text(f"{bar} {_LABELS[stage]}", style="dim")


class RefreshIndicator(Static):
    """A one-line overlay above the footer showing refresh progress.

    Lives on its own layer (see SmorgApp.CSS) so appearing never reflows
    the panel; width: auto keeps it covering only the cells it draws.
    """

    DEFAULT_CSS = """
    RefreshIndicator {
        layer: refresh-indicator;
        dock: bottom;
        width: auto;
        height: 1;
        margin-bottom: 1;
    }
    """

    def __init__(self) -> None:
        super().__init__(markup=False)
        self.display = False
        self._hide_timer: Timer | None = None

    def show_stage(self, stage: RefreshStage) -> None:
        if self._hide_timer is not None:
            self._hide_timer.stop()
            self._hide_timer = None
        if stage is RefreshStage.FAILED:
            self.display = False
            return
        self.update(_stage_text(stage))
        self.display = True
        if stage is RefreshStage.DONE:
            self._hide_timer = self.set_timer(DONE_LINGER_SECONDS, self._hide)

    def _hide(self) -> None:
        self._hide_timer = None
        self.display = False

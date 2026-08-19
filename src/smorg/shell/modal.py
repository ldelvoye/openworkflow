"""The centered, round-bordered box shared by every modal screen."""

from __future__ import annotations

from textual.screen import ModalScreen


class ModalBox(ModalScreen[None]):
    """A modal centered on screen, with a round-bordered box for whichever
    container a subclass composes — tag that container with the "box" CSS
    class to opt it into the shared border/padding/background.
    """

    DEFAULT_CSS = """
    ModalBox {
        align: center middle;
    }

    ModalBox > .box {
        width: auto;
        height: auto;
        max-width: 64;
        border: round $primary;
        padding: 1 2;
        /* Solid, not alpha: ModalScreen's alpha backdrop resolves to
         * transparent under :ansi (alpha can't blend over ANSI colors), so
         * this needs its own opaque background instead.
         */
        background: $background;
        &:ansi {
            background: ansi_default;
        }
    }

    /* Static has no width of its own, so inside this auto-width parent it
     * falls back to filling a not-yet-known width — a circular dependency
     * that renders an empty box. Explicit auto width breaks the cycle.
     */
    ModalBox Static { width: auto; }
    """

"""The base every management screen shares, plus the option-list helper they all pick with."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from smorg.shell.modal import ModalBox


def _selected[T](items: Sequence[T], option_id: str | None, id_of: Callable[[T], str]) -> T | None:
    for item in items:
        if id_of(item) == option_id:
            return item
    return None


class ManagementScreen(ModalBox):
    """Base for the modal screens that manage integrations (add and remove). SmorgApp's
    check_action refuses every shell-level action while one of these is the top screen.
    """

    DEFAULT_CSS = """
    ManagementScreen > OptionList {
        max-width: 64;
        border: round $primary;
        &:focus {
            border: round $primary;
        }
    }
    """

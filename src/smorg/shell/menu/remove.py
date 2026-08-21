"""The remove flow: the tab picker, the confirm modal, and the removal worker behind it."""

from __future__ import annotations

from dataclasses import dataclass

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from smorg.auth.store import CredentialStoreError
from smorg.core.config import ConfigError, TabConfig, load_config
from smorg.core.registry import UnknownIntegration, get_integration
from smorg.core.removal import RemovalResult, remove_integration
from smorg.shell.menu.base import ManagementScreen, _selected


@dataclass(frozen=True)
class RemovableTab:
    integration_id: str
    display_name: str
    connection_id: str | None

    @property
    def label(self) -> str:
        if self.connection_id:
            return f"{self.display_name} ({self.connection_id})"
        return self.display_name


def removable_tabs() -> tuple[RemovableTab, ...]:
    """One entry per configured tab, known-to-this-build or not. A config that can't even be
    read yields no commands rather than raising through the palette.
    """
    try:
        config = load_config()
    except ConfigError:
        return ()
    tabs = tuple(_describe_tab(tab) for tab in config.tabs)
    return tabs


def _describe_tab(tab: TabConfig) -> RemovableTab:
    try:
        integration = get_integration(tab.integration)
    except UnknownIntegration:
        return RemovableTab(tab.integration, tab.integration, tab.connection)
    if tab.connection:
        connection_id = tab.connection
    else:
        connection_id = integration.manifest.connection(None).id
    return RemovableTab(tab.integration, integration.manifest.display_name, connection_id)


def _format_removal_toast(display_name: str, result: RemovalResult) -> str:
    if not result.had_credentials:
        return f"removed {display_name}"
    if result.revoked:
        return f"removed {display_name}; token revoked"
    return f"removed {display_name}; token could not be revoked and stays valid until it expires"


class RemoveIntegrationList(ManagementScreen):
    """One row per configured tab"""

    BINDINGS = [Binding("escape", "cancel", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        rows = (Option(tab.label, id=tab.integration_id) for tab in removable_tabs())
        options = OptionList(*rows)
        options.border_title = "remove integration"
        yield options

    def action_cancel(self) -> None:
        self.dismiss()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        chosen = _selected(removable_tabs(), event.option_id, lambda tab: tab.integration_id)
        self.dismiss()
        if chosen is not None:
            self.app.push_screen(RemoveConfirmModal(chosen.integration_id, chosen.display_name))


class RemoveConfirmModal(ManagementScreen):
    """Confirm, then remove: y/n or escape decide. Once confirmed, a worker runs and every key
    is ignored until it reports back; removal is not cancellable.
    """

    BINDINGS = [
        Binding("y", "confirm", "confirm"),
        Binding("n", "cancel", "cancel"),
        Binding("escape", "cancel", "cancel", show=False),
    ]

    def __init__(self, integration_id: str, display_name: str) -> None:
        super().__init__()
        self.integration_id = integration_id
        self.display_name = display_name
        self._removing = False

    def compose(self) -> ComposeResult:
        box = Vertical(Static(self._body_text(), markup=False, id="body"), classes="box")
        box.border_title = "remove integration"
        yield box

    def _body_text(self) -> str:
        if self._removing:
            return f"removing {self.display_name}…"
        return (
            f"Remove {self.display_name}? This deletes its stored token "
            "(revoking it if possible), its tab, and its seen marks.\n\n"
            "y confirm   n/esc cancel"
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if self._removing:
            return False
        return super().check_action(action, parameters)

    def action_cancel(self) -> None:
        self.dismiss()

    def action_confirm(self) -> None:
        self._removing = True
        self.query_one("#body", Static).update(self._body_text())
        self._remove()

    @work(thread=True)
    def _remove(self) -> None:
        try:
            result = remove_integration(self.integration_id)
        except (CredentialStoreError, ConfigError, UnknownIntegration) as error:
            # UnknownIntegration: the tab vanished externally (e.g. a CLI logout) between
            # listing it and confirming — same error toast.
            self.app.call_from_thread(self._fail, str(error))
            return
        self.app.call_from_thread(self._succeed, result)

    def _fail(self, message: str) -> None:
        self.dismiss()
        self.app.notify(message, severity="error")

    async def _succeed(self, result: RemovalResult) -> None:
        # Lazy import: at module scope this would cycle with app.py.
        from smorg.shell.app import SmorgApp

        app = self.app
        assert isinstance(app, SmorgApp)
        # remove_integration() purged the file; forget the live instance too, or the next save()
        # writes these marks back.
        app.seen.forget(self.integration_id)
        await app.drop_tab(self.integration_id)
        self.dismiss()
        app.notify(_format_removal_toast(self.display_name, result))

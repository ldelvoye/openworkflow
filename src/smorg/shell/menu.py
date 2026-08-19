"""The ctrl+p menu's management surface: the "Remove integration" and "Add
integration" commands, the tab/integration/path pickers and modals they lead
to, and the base every management screen shares.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Vertical
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from smorg.auth.login import LoginCancelled, perform_login
from smorg.auth.oauth import OAuthError, ProviderConfig, extra_scopes_warning
from smorg.auth.store import Credentials, CredentialStoreError, set_credentials
from smorg.auth.token import (
    InvalidToken,
    TokenPrompt,
    accepted_token,
    credentials_from_token,
)
from smorg.core.config import ConfigError, TabConfig, add_tab, load_config, save_config
from smorg.core.contract import ConnectionPath
from smorg.core.registry import UnknownIntegration, get_integration, manifests
from smorg.core.removal import RemovalResult, remove_integration, revoke_best_effort
from smorg.shell.modal import ModalBox

REMOVE_COMMAND = "Remove integration"
ADD_COMMAND = "Add integration"


def _selected[T](items: Sequence[T], option_id: str | None, id_of: Callable[[T], str]) -> T | None:
    for item in items:
        if id_of(item) == option_id:
            return item
    return None


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
    """One entry per configured tab, known-to-this-build or not. A config
    that can't even be read yields no commands rather than raising through
    the palette.
    """
    try:
        config = load_config()
    except ConfigError:
        return ()
    return tuple[RemovableTab, ...](_describe_tab(tab) for tab in config.tabs)


def _describe_tab(tab: TabConfig) -> RemovableTab:
    """Known -> the manifest's display name and its resolved connection id
    (a recorded-but-stale id is kept verbatim, never re-resolved). Unknown ->
    the raw integration id, and only a connection id already on record."""
    try:
        integration = get_integration(tab.integration)
    except UnknownIntegration:
        return RemovableTab(tab.integration, tab.integration, tab.connection)
    if tab.connection:
        connection_id = tab.connection
    else:
        connection_id = integration.manifest.connection(None).id
    return RemovableTab(tab.integration, integration.manifest.display_name, connection_id)


def removal_toast(display_name: str, result: RemovalResult) -> str:
    """Turn a successful RemovalResult into the toast text. No prior
    credentials means no token fate to report at all."""
    if not result.had_credentials:
        return f"removed {display_name}"
    if result.revoked:
        return f"removed {display_name}; token revoked"
    return f"removed {display_name}; token could not be revoked and stays valid until it expires"


class ManagementScreen(ModalBox):
    """Base for modal screens that manage integrations — add and remove.
    SmorgApp.check_action refuses every shell-level action while one of
    these is the top screen.
    """

    DEFAULT_CSS = """
    ManagementScreen > OptionList { max-width: 64; }
    """


class RemoveIntegrationList(ManagementScreen):
    """One row per configured tab; enter hands off to RemoveConfirmModal,
    escape cancels. Never both on the stack at once — this screen dismisses
    itself before pushing the confirm modal."""

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
    """Confirm, then remove: y/n or escape decide. Once confirmed, a bounded
    worker runs and every key is inert until it reports back — removal is
    not cancellable."""

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
        # Not cancellable once started: every key, escape included, is inert.
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
            # UnknownIntegration: the tab vanished externally (e.g. a CLI
            # logout) between listing it and confirming — same error toast.
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
        # remove_integration() purged the file; forget the live instance too,
        # or the next save() writes these marks back.
        app.seen.forget(self.integration_id)
        await app.drop_tab(self.integration_id)
        self.dismiss()
        app.notify(removal_toast(self.display_name, result))


@dataclass(frozen=True)
class AddableIntegration:
    integration_id: str
    display_name: str
    connections: tuple[ConnectionPath, ...]


def addable_integrations() -> tuple[AddableIntegration, ...]:
    """Every registered manifest with no configured tab (one tab per
    integration; re-auth of a configured one stays `smorg connect`), each
    carrying its declared connection paths in declaration order. A config
    that can't even be read yields no commands, same as removable_tabs.
    """
    try:
        config = load_config()
    except ConfigError:
        return ()
    configured_ids = {tab.integration for tab in config.tabs}
    return tuple[AddableIntegration, ...](
        AddableIntegration(manifest.id, manifest.display_name, manifest.connections)
        for manifest in manifests()
        if manifest.id not in configured_ids
    )


class AddIntegrationList(ManagementScreen):
    """One row per addable integration; enter hands off to AddConnectionList,
    escape cancels. Never both on the stack at once — this screen dismisses
    itself before pushing the next one."""

    BINDINGS = [Binding("escape", "cancel", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        rows = (
            Option(integration.display_name, id=integration.integration_id)
            for integration in addable_integrations()
        )
        options = OptionList(*rows)
        options.border_title = "add integration"
        yield options

    def action_cancel(self) -> None:
        self.dismiss()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        chosen = _selected(
            addable_integrations(), event.option_id, lambda integration: integration.integration_id
        )
        self.dismiss()
        if chosen is not None:
            self.app.push_screen(AddConnectionList(chosen))


async def open_tab_for(
    screen: ManagementScreen,
    display_name: str,
    tab_config: TabConfig,
    credentials: Credentials,
    on_store_failure: Callable[[], None] | None = None,
    warning: str | None = None,
) -> None:
    """Store credentials, record the tab, and mount it live — the finishing
    half every connect flow shares, whichever way the credentials arrived.

    Credentials are written before the config entry: a recorded tab whose token
    never made it is a broken tab, where a stored token with no tab is an
    orphan `smorg logout` can still clear. `on_store_failure` is how a flow
    that could hand a live token back does so before giving up.
    """
    try:
        set_credentials(tab_config.integration, credentials)
    except CredentialStoreError as error:
        if on_store_failure is not None:
            on_store_failure()
        screen.dismiss()
        screen.app.notify(str(error), severity="error")
        return

    try:
        save_config(add_tab(load_config(), tab_config))
    except ConfigError as error:
        # Credentials stay stored — same gap cli._connect has; no invented
        # recovery here either.
        screen.dismiss()
        screen.app.notify(str(error), severity="error")
        return

    if warning is not None:
        screen.app.notify(warning, severity="warning")

    # Lazy import: at module scope this would cycle with app.py.
    from smorg.shell.app import SmorgApp

    app = screen.app
    assert isinstance(app, SmorgApp)
    await app.add_tab_live(tab_config)
    screen.dismiss()
    app.notify(f"connected {display_name}")


def connect_screen_for(integration: AddableIntegration, path: ConnectionPath) -> ManagementScreen:
    """Which connect flow a chosen path leads to: the browser wait, or one
    field of input. The path decides, and this is the only place that asks."""
    if isinstance(path.method, TokenPrompt):
        return TokenModal(integration.integration_id, integration.display_name, path)
    return ConnectModal(integration.integration_id, integration.display_name, path)


class AddConnectionList(ManagementScreen):
    """One row per the chosen integration's declared connection paths, even
    when there is only one — the flow's shape stays the same regardless of
    how many paths an integration declares. Escape cancels; selecting
    dismisses and pushes whichever connect screen the path calls for."""

    BINDINGS = [Binding("escape", "cancel", "cancel", show=False)]

    def __init__(self, integration: AddableIntegration) -> None:
        super().__init__()
        self.integration = integration

    def compose(self) -> ComposeResult:
        rows = (Option(path.id, id=path.id) for path in self.integration.connections)
        options = OptionList(*rows)
        options.border_title = self.integration.display_name
        yield options

    def action_cancel(self) -> None:
        self.dismiss()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        chosen = _selected(self.integration.connections, event.option_id, lambda path: path.id)
        self.dismiss()
        if chosen is not None:
            self.app.push_screen(connect_screen_for(self.integration, chosen))


class TokenModal(ManagementScreen):
    """Ask for a token the user created in the service themselves, and store it.

    No worker and no cancellation window, unlike ConnectModal: nothing here
    waits on a network, so the only two outcomes are submitted and escaped.
    """

    DEFAULT_CSS = """
    TokenModal > .box { width: 64; }
    TokenModal Input { width: 1fr; }
    """

    BINDINGS = [Binding("escape", "cancel", "cancel", show=False)]

    def __init__(self, integration_id: str, display_name: str, path: ConnectionPath) -> None:
        super().__init__()
        prompt = path.method
        assert isinstance(prompt, TokenPrompt), "TokenModal is only reached for a token path"
        self.integration_id = integration_id
        self.display_name = display_name
        self.path = path
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        # password: the token is a live credential, and a terminal's scrollback
        # outlives this screen.
        entry = Input(password=True, placeholder=self.prompt.label, id="token")
        box = Vertical(
            Static(self.body_text(), markup=False, id="body"),
            entry,
            classes="box",
        )
        box.border_title = "add integration"
        yield box

    def on_mount(self) -> None:
        self.query_one("#token", Input).focus()

    def body_text(self) -> str:
        """Public, like Panel.body_text(), so tests can assert on content directly."""
        return "\n\n".join(
            [
                f"{self.display_name} connects with a token you create yourself.",
                f"create one at: {self.prompt.help_url}",
                f"it needs: {self.prompt.scopes_hint}",
                "enter connect   esc cancel",
            ]
        )

    def action_cancel(self) -> None:
        self.dismiss()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        try:
            token = accepted_token(event.value)
        except InvalidToken as error:
            # Stays open with the field cleared: the fix is another paste, and
            # dismissing would cost the user the whole menu to get back here.
            event.input.value = ""
            self.app.notify(str(error), severity="error")
            return
        tab_config = TabConfig(integration=self.integration_id, connection=self.path.id)
        await open_tab_for(self, self.display_name, tab_config, credentials_from_token(token))


class ConnectModal(ManagementScreen):
    """Run the OAuth connect for one integration/path in-app: a
    @work(thread=True) worker calls perform_login while the browser opens and
    the TUI stays input-blocked. Escape cancels via a threading.Event, but
    only while perform_login is still waiting — once it returns or raises,
    check_action goes inert, same idiom as RemoveConfirmModal's non-cancellable
    removal, so a stray keypress can't race the finalize steps below.
    """

    BINDINGS = [Binding("escape", "cancel", "cancel", show=False)]

    def __init__(self, integration_id: str, display_name: str, path: ConnectionPath) -> None:
        super().__init__()
        provider = path.method
        assert isinstance(provider, ProviderConfig), (
            "ConnectModal is only reached for an OAuth path"
        )
        self.integration_id = integration_id
        self.display_name = display_name
        self.path = path
        self.provider = provider
        self._url: str | None = None
        self._cancellable = True
        self._cancelled_event = threading.Event()

    def compose(self) -> ComposeResult:
        box = Vertical(Static(self._body_text(), markup=False, id="body"), classes="box")
        box.border_title = "add integration"
        yield box

    def on_mount(self) -> None:
        self._connect()

    def _body_text(self) -> str:
        lines = [f"connecting {self.display_name} via {self.path.id}…"]
        if self._url is not None:
            lines.append(f"your browser should have opened; if not, open: {self._url}")
        if self._cancellable:
            lines.append("esc cancel")
        return "\n\n".join(lines)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if not self._cancellable:
            return False
        return super().check_action(action, parameters)

    def action_cancel(self) -> None:
        self._cancelled_event.set()

    @work(thread=True)
    def _connect(self) -> None:
        try:
            with httpx.Client(timeout=30) as client:
                client_id, credentials = perform_login(
                    client,
                    self.provider,
                    None,
                    on_authorize_url=lambda url: self.app.call_from_thread(self._show_url, url),
                    cancelled=self._cancelled_event,
                )
        except LoginCancelled:
            self.app.call_from_thread(self._on_cancelled)
            return
        except OAuthError as error:
            self.app.call_from_thread(self._on_failed, str(error))
            return
        self.app.call_from_thread(self._on_succeeded, client_id, credentials)

    def _show_url(self, url: str) -> None:
        self._url = url
        self.query_one("#body", Static).update(self._body_text())

    def _on_cancelled(self) -> None:
        self._cancellable = False
        self.dismiss()

    def _on_failed(self, message: str) -> None:
        self._cancellable = False
        self.dismiss()
        self.app.notify(message, severity="error")

    async def _on_succeeded(self, client_id: str, credentials: Credentials) -> None:
        self._cancellable = False

        def hand_the_token_back() -> None:
            # The token is live and about to become unreachable — nothing will
            # hold it, so nothing could revoke it later.
            revoke_best_effort(self.provider, client_id, credentials)

        tab_config = TabConfig(
            integration=self.integration_id, client_id=client_id, connection=self.path.id
        )
        warning = extra_scopes_warning(
            self.integration_id, self.display_name, self.provider, credentials
        )
        await open_tab_for(
            self,
            self.display_name,
            tab_config,
            credentials,
            on_store_failure=hand_the_token_back,
            warning=warning,
        )


class MenuCommands(Provider):
    """Top-level management commands for the menu."""

    async def discover(self) -> Hits:
        if removable_tabs():
            yield DiscoveryHit(REMOVE_COMMAND, self._open_remove_list)
        if addable_integrations():
            yield DiscoveryHit(ADD_COMMAND, self._open_add_list)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        if removable_tabs():
            score = matcher.match(REMOVE_COMMAND)
            if score > 0:
                yield Hit(score, matcher.highlight(REMOVE_COMMAND), self._open_remove_list)
        if addable_integrations():
            score = matcher.match(ADD_COMMAND)
            if score > 0:
                yield Hit(score, matcher.highlight(ADD_COMMAND), self._open_add_list)

    def _open_remove_list(self) -> None:
        self.app.push_screen(RemoveIntegrationList())

    def _open_add_list(self) -> None:
        self.app.push_screen(AddIntegrationList())

"""The add flow: the integration and path pickers, and the connect modal each auth path leads to."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from smorg.auth.login import LoginCancelled, perform_login
from smorg.auth.oauth import (
    REGISTERED_REDIRECT_URI,
    OAuthError,
    OAuthMethod,
    StaticProvider,
    extra_scopes_warning,
)
from smorg.auth.store import Credentials, CredentialStoreError, set_credentials
from smorg.auth.token import (
    InvalidToken,
    TokenMethod,
    accepted_token,
    credentials_from_token,
)
from smorg.core.config import ConfigError, TabConfig, add_tab, load_config, save_config
from smorg.core.contract import AuthPath
from smorg.core.registry import manifests
from smorg.core.removal import revoke_best_effort
from smorg.shell.menu.base import ManagementScreen, _selected


@dataclass(frozen=True)
class AddableIntegration:
    integration_id: str
    display_name: str
    connections: tuple[AuthPath, ...]


def addable_integrations() -> tuple[AddableIntegration, ...]:
    """Every registered manifest with no configured tab (one tab per integration; re-auth of a
    configured one stays `smorg connect`), each carrying its declared connection paths in
    declaration order. A config that can't even be read yields no commands, same as
    removable_tabs.
    """
    try:
        config = load_config()
    except ConfigError:
        return ()
    configured_ids = {tab.integration for tab in config.tabs}
    integrations = tuple(
        AddableIntegration(manifest.id, manifest.display_name, manifest.connections)
        for manifest in manifests()
        if manifest.id not in configured_ids
    )
    return integrations


class AddIntegrationList(ManagementScreen):
    """One row per addable integration; enter hands off to AddConnectionList, escape cancels.
    This screen dismisses itself before pushing the next one, so the two are never stacked.
    """

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
    """Store credentials, record the tab, and mount it live. Every connect flow ends here.

    Credentials are written before the config entry: a recorded tab without its token is broken,
    while a stored token without a tab is an orphan that `smorg logout` can still clear.
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
        # Credentials stay stored — same gap cli._connect has.
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


def connect_screen_for(integration: AddableIntegration, path: AuthPath) -> ManagementScreen:
    """Which connect screen a chosen path leads to"""
    if isinstance(path.method, TokenMethod):
        return TokenModal(integration.integration_id, integration.display_name, path)
    if isinstance(path.method.provider, StaticProvider):
        return ClientIdModal(integration.integration_id, integration.display_name, path)
    return ConnectModal(integration.integration_id, integration.display_name, path)


class AddConnectionList(ManagementScreen):
    """One row per declared connection path"""

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

    No worker and no cancellation window, unlike ConnectModal: nothing here waits on a network,
    so the only two outcomes are submitted and escaped.
    """

    DEFAULT_CSS = """
    TokenModal > .box { width: 64; }
    TokenModal Input { width: 1fr; }
    """

    BINDINGS = [Binding("escape", "cancel", "cancel", show=False)]

    def __init__(self, integration_id: str, display_name: str, path: AuthPath) -> None:
        super().__init__()
        prompt = path.method
        assert isinstance(prompt, TokenMethod), "TokenModal is only reached for a token path"
        self.integration_id = integration_id
        self.display_name = display_name
        self.path = path
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        # password: a live credential, and a terminal's scrollback outlives this screen.
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
            # Stays open with the field cleared: the fix is another paste, and dismissing would
            # cost the user the whole menu to get back here.
            event.input.value = ""
            self.app.notify(str(error), severity="error")
            return
        tab_config = TabConfig(integration=self.integration_id, connection=self.path.id)
        await open_tab_for(self, self.display_name, tab_config, credentials_from_token(token))


class ClientIdModal(ManagementScreen):
    """Ask for the client id of the OAuth app the user created themselves, then hand off to
    ConnectModal for the browser flow."""

    DEFAULT_CSS = """
    ClientIdModal > .box { width: 64; }
    ClientIdModal Input { width: 1fr; }
    """

    BINDINGS = [Binding("escape", "cancel", "cancel", show=False)]

    def __init__(self, integration_id: str, display_name: str, path: AuthPath) -> None:
        super().__init__()
        method = path.method
        assert isinstance(method, OAuthMethod) and isinstance(method.provider, StaticProvider), (
            "ClientIdModal is only reached for a static-provider path"
        )
        self.integration_id = integration_id
        self.display_name = display_name
        self.path = path
        self.provider = method.provider

    def compose(self) -> ComposeResult:
        entry = Input(placeholder="client id", id="client-id")
        box = Vertical(Static(self.body_text(), markup=False, id="body"), entry, classes="box")
        box.border_title = "add integration"
        yield box

    def on_mount(self) -> None:
        self.query_one("#client-id", Input).focus()

    def body_text(self) -> str:
        """Public, like Panel.body_text(), so tests can assert on content directly."""
        return "\n\n".join(
            [
                f"{self.display_name} needs an OAuth app you create yourself.",
                f"create one at: {self.provider.help_url}",
                f"set its redirect uri to: {REGISTERED_REDIRECT_URI}",
                self.provider.setup_hint,
                "enter connect   esc cancel",
            ]
        )

    def action_cancel(self) -> None:
        self.dismiss()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        client_id = event.value.strip()
        if not client_id:
            self.app.notify("a client id is required", severity="error")
            return
        self.dismiss()
        self.app.push_screen(
            ConnectModal(self.integration_id, self.display_name, self.path, client_id=client_id)
        )


class ConnectModal(ManagementScreen):
    """Run the OAuth connect for one integration and path in-app while the TUI stays
    input-blocked. Escape cancels only while perform_login is still waiting; after it returns or
    raises, check_action ignores every key so nothing can race the finalize steps.
    """

    BINDINGS = [Binding("escape", "cancel", "cancel", show=False)]

    def __init__(
        self, integration_id: str, display_name: str, path: AuthPath, client_id: str | None = None
    ) -> None:
        super().__init__()
        method = path.method
        assert isinstance(method, OAuthMethod), "ConnectModal is only reached for an OAuth path"
        self.integration_id = integration_id
        self.display_name = display_name
        self.path = path
        self.method = method
        self.client_id = client_id
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
                    self.method,
                    self.client_id,
                    on_authorize_url=lambda url: self.app.call_from_thread(self._show_url, url),
                    cancelled=self._cancelled_event,
                )
        except LoginCancelled:
            self.app.call_from_thread(self._close)
            return
        except OAuthError as error:
            self.app.call_from_thread(self._close, str(error))
            return
        self.app.call_from_thread(self._on_succeeded, client_id, credentials)

    def _show_url(self, url: str) -> None:
        self._url = url
        self.query_one("#body", Static).update(self._body_text())

    def _close(self, error: str | None = None) -> None:
        self._cancellable = False
        self.dismiss()
        if error is not None:
            self.app.notify(error, severity="error")

    async def _on_succeeded(self, client_id: str, credentials: Credentials) -> None:
        self._cancellable = False

        def revoke_token() -> None:
            # The token is live and about to become unreachable — nothing will hold it, so
            # nothing could revoke it later.
            revoke_best_effort(self.method, client_id, credentials)

        tab_config = TabConfig(
            integration=self.integration_id, client_id=client_id, connection=self.path.id
        )
        warning = extra_scopes_warning(
            self.integration_id, self.display_name, self.method, credentials
        )
        await open_tab_for(
            self,
            self.display_name,
            tab_config,
            credentials,
            on_store_failure=revoke_token,
            warning=warning,
        )

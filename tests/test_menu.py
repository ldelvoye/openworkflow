import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from textual.widgets import Static, TabPane

from smorg.auth.login import LoginCancelled
from smorg.auth.oauth import ProviderConfig
from smorg.auth.store import Credentials, CredentialStoreError, get_credentials, set_credentials
from smorg.core.config import Config, TabConfig, load_config, save_config
from smorg.core.contract import ConnectionPath, Item, Manifest
from smorg.core.removal import RemovalResult
from smorg.core.state import SeenState
from smorg.integrations.linear.manifest import LinearIntegration
from smorg.shell.app import SmorgApp
from smorg.shell.menu import (
    ADD_COMMAND,
    REMOVE_COMMAND,
    AddConnectionList,
    AddIntegrationList,
    ConnectModal,
    MenuCommands,
    RemovableTab,
    RemoveConfirmModal,
    RemoveIntegrationList,
    addable_integrations,
    removable_tabs,
)
from smorg.shell.panel import Panel

LIVE = Credentials(
    access_token="at-secret",
    refresh_token="rt-secret",
    expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    scope="read",
)
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)

WIDGET_PROVIDER = ProviderConfig(
    metadata_url="https://widget.example.invalid/.well-known/oauth-authorization-server",
    scopes=("read",),
    client_name="smorg",
)


async def _wait_until(pilot, condition) -> None:
    deadline = time.monotonic() + 8
    while not condition() and time.monotonic() < deadline:
        await pilot.pause(0.05)


def item(identifier: str = "ENG-1") -> Item:
    return Item(id=identifier, updated_at=NOW, url="https://example.invalid/1")


def fake_manifest(
    identifier: str = "widget",
    connections: tuple[ConnectionPath, ...] = (ConnectionPath(id="mcp", provider=WIDGET_PROVIDER),),
) -> Manifest:
    return Manifest(
        id=identifier,
        display_name=identifier.title(),
        connections=connections,
        stale_after=timedelta(minutes=5),
        actions=(),
    )


@dataclass(frozen=True)
class FakeIntegration:
    """Stands in for a real integration, so it satisfies the whole protocol.

    A fake that implements less than the contract can pass a test that a real
    integration would fail.
    """

    manifest: Manifest
    panel_class: type[Panel] = Panel

    def fetch(self, credentials, http):
        return ()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SMORG_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("SMORG_CREDENTIAL_STORE", "file")


@pytest.fixture(autouse=True)
def no_linear_network(monkeypatch):
    # Menu tests configure the real "linear" integration with seeded credentials;
    # CONTRIBUTING mandates no network in tests.
    monkeypatch.setattr(LinearIntegration, "fetch", lambda self, credentials, http: ())

    def unexpected_fetch_detail(self, credentials, http, item):
        raise AssertionError("unexpected fetch_detail in menu tests")

    monkeypatch.setattr(LinearIntegration, "fetch_detail", unexpected_fetch_detail)


@pytest.fixture
def registered(monkeypatch):
    """Swap the registry's allowlist for fake integrations, so add-flow tests
    control display names, ids, and declared connection paths directly."""

    def register(*manifests: Manifest):
        integrations = tuple(FakeIntegration(manifest=entry) for entry in manifests)
        monkeypatch.setattr("smorg.integrations.INTEGRATIONS", integrations)
        return integrations

    return register


@pytest.fixture
def revocation(monkeypatch):
    """Stub the network so a confirmed removal never reaches a real server."""

    def fake_discover(client, provider):
        return object()

    def fake_revoke(client, metadata, client_id, credentials):
        return True

    monkeypatch.setattr("smorg.core.removal.oauth.discover", fake_discover)
    monkeypatch.setattr("smorg.core.removal.oauth.revoke", fake_revoke)


# --- The menu's command list (a pure function; no palette/Provider plumbing) ---


def test_a_configured_known_integration_gets_a_labeled_row():
    save_config(Config(tabs=(TabConfig(integration="linear", connection="mcp"),)))

    tabs = removable_tabs()

    assert tabs == (RemovableTab("linear", "Linear", "mcp"),)
    assert tabs[0].label == "Linear (mcp)"


def test_an_unknown_to_the_build_configured_id_still_gets_a_row():
    save_config(Config(tabs=(TabConfig(integration="jira"),)))

    tabs = removable_tabs()

    assert tabs == (RemovableTab("jira", "jira", None),)
    assert tabs[0].label == "jira"


# --- The top-level "Remove integration" command ---


@pytest.mark.asyncio
async def test_the_remove_command_is_offered_only_when_a_tab_is_configured():
    app = SmorgApp(tabs=())
    async with app.run_test() as pilot:
        provider = MenuCommands(pilot.app.screen)
        hits = [hit async for hit in provider.discover()]
        # "linear" is unconfigured here, so it is addable — ADD_COMMAND may
        # legitimately show up too; only REMOVE_COMMAND is under test.
        assert REMOVE_COMMAND not in [hit.text for hit in hits]

        save_config(Config(tabs=(TabConfig(integration="linear"),)))
        hits = [hit async for hit in provider.discover()]

    assert len(hits) == 1
    assert hits[0].text == REMOVE_COMMAND


# --- The tab picker ---


@pytest.mark.asyncio
async def test_selecting_a_row_hands_off_to_the_confirm_modal_and_dismisses_the_list():
    save_config(Config(tabs=(TabConfig(integration="linear", connection="mcp"),)))

    app = SmorgApp(tabs=(TabConfig("linear"),))
    async with app.run_test() as pilot:
        await pilot.pause()
        list_screen = RemoveIntegrationList()
        app.push_screen(list_screen)
        await pilot.pause()

        await pilot.press("enter")
        await pilot.pause()

        assert list_screen not in app.screen_stack
        assert isinstance(app.screen, RemoveConfirmModal)
        assert app.screen.integration_id == "linear"
        assert app.screen.display_name == "Linear"


@pytest.mark.asyncio
async def test_escape_on_the_list_cancels_without_opening_the_confirm_modal():
    save_config(Config(tabs=(TabConfig(integration="linear"),)))

    app = SmorgApp(tabs=(TabConfig("linear"),))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(RemoveIntegrationList())
        await pilot.pause()

        await pilot.press("escape")
        await pilot.pause()

        assert not isinstance(app.screen, RemoveIntegrationList)
        assert not isinstance(app.screen, RemoveConfirmModal)


# --- The confirm modal ---


@pytest.mark.asyncio
async def test_escape_on_the_confirm_modal_removes_nothing():
    save_config(Config(tabs=(TabConfig(integration="linear"),)))
    set_credentials("linear", LIVE)

    app = SmorgApp(tabs=(TabConfig("linear"),))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(RemoveConfirmModal("linear", "Linear"))
        await pilot.pause()
        assert isinstance(app.screen, RemoveConfirmModal)

        await pilot.press("escape")
        await pilot.pause()

        assert not isinstance(app.screen, RemoveConfirmModal)

    assert get_credentials("linear") is not None
    assert [tab.integration for tab in load_config().tabs] == ["linear"]


@pytest.mark.asyncio
async def test_confirming_removes_the_pane_and_every_stored_trace(revocation):
    save_config(
        Config(
            tabs=(
                TabConfig(integration="alpha"),
                TabConfig(integration="linear", client_id="client-abc", connection="mcp"),
            )
        )
    )
    set_credentials("linear", LIVE)
    state = SeenState.load()
    state.mark_seen("linear", item())
    state.save()

    app = SmorgApp(tabs=(TabConfig("alpha"), TabConfig("linear")))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(RemoveConfirmModal("linear", "Linear"))
        await pilot.pause()

        await pilot.press("y")
        await _wait_until(pilot, lambda: not isinstance(app.screen, RemoveConfirmModal))
        await pilot.pause()

        # Asserted against the default screen explicitly (screen_stack[0]),
        # not an app-wide query — the modal that ran the removal was still
        # on top of it when drop_tab mutated it.
        default_screen = app.screen_stack[0]
        assert app.tab_ids == ("alpha",)
        assert [pane.id for pane in default_screen.query(TabPane)] == ["alpha"]

    assert get_credentials("linear") is None
    assert [tab.integration for tab in load_config().tabs] == ["alpha"]
    assert SeenState.load().is_changed("linear", item()) is True


@pytest.mark.asyncio
async def test_removing_the_last_tab_shows_the_startup_empty_hint():
    save_config(Config(tabs=(TabConfig(integration="linear"),)))
    set_credentials("linear", LIVE)

    app = SmorgApp(tabs=(TabConfig("linear"),))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(RemoveConfirmModal("linear", "Linear"))
        await pilot.pause()

        await pilot.press("y")
        await _wait_until(pilot, lambda: not isinstance(app.screen, RemoveConfirmModal))
        await pilot.pause()

        # Asserted against the default screen explicitly, and against the
        # exact hint widget/content — not a substring that a leftover
        # panel's "not connected" error could also satisfy.
        default_screen = app.screen_stack[0]
        assert list(default_screen.query(TabPane)) == []
        hint = default_screen.query_one("#empty-hint", Static)
        assert hint.display is True
        assert hint.content == app.empty_hint


@pytest.mark.asyncio
async def test_a_later_mark_seen_save_does_not_resurrect_a_removed_integrations_marks():
    """The live-SeenState decision: remove_integration() purges state.json,
    but the app keeps its own SeenState instance across the removal, so it
    must forget the removed integration too — otherwise the next unrelated
    save() would write its marks straight back to disk."""
    save_config(Config(tabs=(TabConfig(integration="alpha"), TabConfig(integration="linear"))))
    set_credentials("linear", LIVE)
    state = SeenState.load()
    state.mark_seen("linear", item())
    state.save()

    app = SmorgApp(tabs=(TabConfig("alpha"), TabConfig("linear")))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(RemoveConfirmModal("linear", "Linear"))
        await pilot.pause()

        await pilot.press("y")
        await _wait_until(pilot, lambda: not isinstance(app.screen, RemoveConfirmModal))
        await pilot.pause()

        app.seen.mark_seen("alpha", item("SURVIVOR-1"))
        app.seen.save()

    survivors = SeenState.load()
    assert survivors.is_changed("linear", item()) is True
    assert survivors.is_changed("alpha", item("SURVIVOR-1")) is False


@pytest.mark.asyncio
async def test_nothing_else_happens_while_a_removal_is_in_flight(monkeypatch):
    release = threading.Event()

    def blocked_removal(integration_id: str) -> RemovalResult:
        release.wait(timeout=5)
        return RemovalResult(supported=True, had_credentials=False, revoked=False, tab_removed=True)

    monkeypatch.setattr("smorg.shell.menu.remove_integration", blocked_removal)

    refreshed: list[str] = []
    monkeypatch.setattr(
        "smorg.shell.app.SmorgApp.refresh_tab",
        lambda self, integration_id, panel, force=False, on_stage=None: refreshed.append(
            integration_id
        ),
    )
    quit_calls: list[None] = []

    async def fake_quit(self) -> None:
        quit_calls.append(None)

    monkeypatch.setattr("smorg.shell.app.SmorgApp.action_quit", fake_quit)

    # "alpha" stays active throughout: removing the non-active "linear" tab
    # keeps which tab a post-unblock "r" should refresh unambiguous.
    app = SmorgApp(tabs=(TabConfig("alpha"), TabConfig("linear")))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(RemoveConfirmModal("linear", "Linear"))
        await pilot.pause()

        await pilot.press("y")
        await pilot.pause()

        refreshed.clear()
        await pilot.press("q")
        await pilot.press("r")
        await pilot.pause()

        assert quit_calls == []
        assert refreshed == []
        assert isinstance(app.screen, RemoveConfirmModal)

        release.set()
        await _wait_until(pilot, lambda: not isinstance(app.screen, RemoveConfirmModal))
        await pilot.pause()

        assert not isinstance(app.screen, RemoveConfirmModal)

        await pilot.press("r")
        await pilot.pause()

    assert refreshed == ["alpha"]


# --- addable_integrations() (a pure function; no palette/Provider plumbing) ---


def test_addable_integrations_excludes_configured_ones_and_lists_paths_in_order(registered):
    mcp = ConnectionPath(id="mcp", provider=WIDGET_PROVIDER)
    api = ConnectionPath(id="api", provider=WIDGET_PROVIDER)
    registered(fake_manifest("widget", connections=(mcp, api)), fake_manifest("gadget"))
    save_config(Config(tabs=(TabConfig(integration="gadget"),)))

    addable = addable_integrations()

    assert [entry.integration_id for entry in addable] == ["widget"]
    assert addable[0].connections == (mcp, api)


def test_addable_integrations_is_empty_once_everything_is_configured(registered):
    registered(fake_manifest("widget"))
    save_config(Config(tabs=(TabConfig(integration="widget"),)))

    assert addable_integrations() == ()


# --- The top-level "Add integration" command ---


@pytest.mark.asyncio
async def test_the_add_command_is_offered_only_when_something_is_addable(registered):
    registered(fake_manifest("widget"))

    app = SmorgApp(tabs=())
    async with app.run_test() as pilot:
        provider = MenuCommands(pilot.app.screen)
        hits = [hit async for hit in provider.discover()]
        assert [hit.text for hit in hits] == [ADD_COMMAND]

        save_config(Config(tabs=(TabConfig(integration="widget"),)))
        hits = [hit async for hit in provider.discover()]

    assert ADD_COMMAND not in [hit.text for hit in hits]


# --- The integration picker (level 1) ---


@pytest.mark.asyncio
async def test_selecting_an_integration_hands_off_to_the_path_list_and_dismisses_itself(
    registered,
):
    registered(fake_manifest("widget"))

    app = SmorgApp(tabs=())
    async with app.run_test() as pilot:
        await pilot.pause()
        list_screen = AddIntegrationList()
        app.push_screen(list_screen)
        await pilot.pause()

        await pilot.press("enter")
        await pilot.pause()

        assert list_screen not in app.screen_stack
        assert isinstance(app.screen, AddConnectionList)
        assert app.screen.integration.integration_id == "widget"


@pytest.mark.asyncio
async def test_escape_on_the_integration_list_cancels_without_opening_the_path_list(registered):
    registered(fake_manifest("widget"))

    app = SmorgApp(tabs=())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(AddIntegrationList())
        await pilot.pause()

        await pilot.press("escape")
        await pilot.pause()

        assert not isinstance(app.screen, AddIntegrationList)
        assert not isinstance(app.screen, AddConnectionList)


# --- The connection path picker (level 2) ---


@pytest.mark.asyncio
async def test_selecting_a_path_hands_off_to_the_connect_modal_and_dismisses_itself(
    registered, monkeypatch
):
    registered(fake_manifest("widget"))
    release = threading.Event()

    def blocked_login(client, provider, client_id, *, on_authorize_url, cancelled=None, **kwargs):
        release.wait(timeout=5)
        raise LoginCancelled("test release")

    monkeypatch.setattr("smorg.shell.menu.perform_login", blocked_login)

    app = SmorgApp(tabs=())
    async with app.run_test() as pilot:
        await pilot.pause()
        widget = addable_integrations()[0]
        path_screen = AddConnectionList(widget)
        app.push_screen(path_screen)
        await pilot.pause()

        await pilot.press("enter")
        await pilot.pause()

        assert path_screen not in app.screen_stack
        assert isinstance(app.screen, ConnectModal)
        assert app.screen.integration_id == "widget"
        assert app.screen.path.id == "mcp"

        release.set()
        await _wait_until(pilot, lambda: not isinstance(app.screen, ConnectModal))
        await pilot.pause()


@pytest.mark.asyncio
async def test_escape_on_the_path_list_cancels_without_opening_the_connect_modal(registered):
    registered(fake_manifest("widget"))

    app = SmorgApp(tabs=())
    async with app.run_test() as pilot:
        await pilot.pause()
        widget = addable_integrations()[0]
        app.push_screen(AddConnectionList(widget))
        await pilot.pause()

        await pilot.press("escape")
        await pilot.pause()

        assert not isinstance(app.screen, AddConnectionList)
        assert not isinstance(app.screen, ConnectModal)


# --- The connect modal ---


@pytest.mark.asyncio
async def test_escape_during_the_wait_cancels_cleanly(registered, monkeypatch):
    registered(fake_manifest("widget"))

    def blocked_login(client, provider, client_id, *, on_authorize_url, cancelled=None, **kwargs):
        on_authorize_url("https://widget.example.invalid/authorize?state=abc")
        assert cancelled is not None
        cancelled.wait(timeout=5)
        raise LoginCancelled("cancelled by the user")

    monkeypatch.setattr("smorg.shell.menu.perform_login", blocked_login)
    notified: list[str] = []
    monkeypatch.setattr(
        "smorg.shell.app.SmorgApp.notify",
        lambda self, message, **kwargs: notified.append(message),
    )

    app = SmorgApp(tabs=())
    async with app.run_test() as pilot:
        await pilot.pause()
        widget = addable_integrations()[0]
        app.push_screen(ConnectModal("widget", "Widget", widget.connections[0]))
        await pilot.pause()

        await pilot.press("escape")
        await _wait_until(pilot, lambda: not isinstance(app.screen, ConnectModal))
        await pilot.pause()

        assert not isinstance(app.screen, ConnectModal)
        default_screen = app.screen_stack[0]
        assert list(default_screen.query(TabPane)) == []

    assert get_credentials("widget") is None
    assert load_config().tabs == ()
    assert notified == []


@pytest.mark.asyncio
async def test_connecting_succeeds_from_the_empty_state(registered, monkeypatch):
    registered(fake_manifest("widget"))
    credentials = Credentials("at-widget", "rt-widget", None, "read")

    def fake_login(client, provider, client_id, *, on_authorize_url, cancelled=None, **kwargs):
        on_authorize_url("https://widget.example.invalid/authorize?state=abc")
        return "client-abc", credentials

    monkeypatch.setattr("smorg.shell.menu.perform_login", fake_login)

    app = SmorgApp(tabs=())
    async with app.run_test() as pilot:
        await pilot.pause()
        widget = addable_integrations()[0]
        app.push_screen(ConnectModal("widget", "Widget", widget.connections[0]))
        await pilot.pause()  # let on_mount dispatch _connect before waiting on it
        await _wait_until(pilot, lambda: not isinstance(app.screen, ConnectModal))
        await pilot.pause()

        # Asserted against the default screen explicitly (screen_stack[0]),
        # not an app-wide query — this is the empty-state -> first-tab
        # transition, so no other screen holds a pane either way.
        default_screen = app.screen_stack[0]
        assert app.tab_ids == ("widget",)
        assert app.active_tab == "widget"
        assert [pane.id for pane in default_screen.query(TabPane)] == ["widget"]

    assert get_credentials("widget") == credentials
    saved = load_config().tabs
    assert len(saved) == 1
    assert saved[0].integration == "widget"
    assert saved[0].client_id == "client-abc"
    assert saved[0].connection == "mcp"


@pytest.mark.asyncio
async def test_a_credential_store_failure_revokes_the_token_and_stores_nothing(
    registered, monkeypatch
):
    registered(fake_manifest("widget"))
    credentials = Credentials("at-widget", "rt-widget", None, "read")

    def fake_login(client, provider, client_id, *, on_authorize_url, cancelled=None, **kwargs):
        return "client-abc", credentials

    monkeypatch.setattr("smorg.shell.menu.perform_login", fake_login)

    def refuse(integration_id, creds):
        raise CredentialStoreError("keychain refused")

    monkeypatch.setattr("smorg.shell.menu.set_credentials", refuse)

    revoked: list[tuple] = []

    def fake_revoke(*args):
        revoked.append(args)
        return True

    monkeypatch.setattr("smorg.shell.menu.revoke_best_effort", fake_revoke)

    app = SmorgApp(tabs=())
    async with app.run_test() as pilot:
        await pilot.pause()
        widget = addable_integrations()[0]
        app.push_screen(ConnectModal("widget", "Widget", widget.connections[0]))
        await pilot.pause()  # let on_mount dispatch _connect before waiting on it
        await _wait_until(pilot, lambda: not isinstance(app.screen, ConnectModal))
        await pilot.pause()

        default_screen = app.screen_stack[0]
        assert app.tab_ids == ()
        assert list(default_screen.query(TabPane)) == []

    assert len(revoked) == 1
    assert load_config().tabs == ()

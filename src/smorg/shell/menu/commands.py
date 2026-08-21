"""The menu's top-level commands: remove, add, and upgrade, gated by what currently applies."""

from __future__ import annotations

import subprocess

from textual.app import App
from textual.command import DiscoveryHit, Hit, Hits, Provider

from smorg.core.text import sanitize_line, truncate
from smorg.core.update import upgrade_command
from smorg.shell.menu.connect import AddIntegrationList, addable_integrations
from smorg.shell.menu.remove import RemoveIntegrationList, removable_tabs

REMOVE_COMMAND = "Remove integration"
ADD_COMMAND = "Add integration"


def _upgrade_label(version: str) -> str:
    return f"Upgrade smorg to {version}"


def _upgrade_failure_toast(command: str, stderr: str) -> str:
    lines = [line for line in stderr.splitlines() if line.strip()]
    if lines:
        tail = lines[-1]
    else:
        tail = ""
    safe_tail = sanitize_line(truncate(tail, 80))
    return f"{command} failed: {safe_tail}"


def _run_upgrade(app: App[object], command: str, version: str) -> None:
    """Run the upgrade command off the UI thread, then toast the outcome. Runs on a worker
    thread; every app touch goes through call_from_thread.
    """
    try:
        result = subprocess.run(command.split(), capture_output=True, text=True)
    except OSError as error:
        app.call_from_thread(
            app.notify, _upgrade_failure_toast(command, str(error)), severity="error"
        )
        return
    if result.returncode == 0:
        app.call_from_thread(app.notify, f"upgraded — restart smorg to use {version}")
    else:
        app.call_from_thread(
            app.notify, _upgrade_failure_toast(command, result.stderr), severity="error"
        )


class MenuCommands(Provider):
    """Top-level management commands for the menu."""

    async def discover(self) -> Hits:
        if removable_tabs():
            yield DiscoveryHit(REMOVE_COMMAND, self._open_remove_list)
        if addable_integrations():
            yield DiscoveryHit(ADD_COMMAND, self._open_add_list)
        available_update = self._available_update()
        if available_update is not None:
            yield DiscoveryHit(_upgrade_label(available_update), self._upgrade)

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
        available_update = self._available_update()
        if available_update is not None:
            label = _upgrade_label(available_update)
            score = matcher.match(label)
            if score > 0:
                yield Hit(score, matcher.highlight(label), self._upgrade)

    def _open_remove_list(self) -> None:
        self.app.push_screen(RemoveIntegrationList())

    def _open_add_list(self) -> None:
        self.app.push_screen(AddIntegrationList())

    def _available_update(self) -> str | None:
        # Lazy import: at module scope this would cycle with app.py.
        from smorg.shell.app import SmorgApp

        app = self.app
        if not isinstance(app, SmorgApp):
            return None
        return app.available_update

    def _upgrade(self) -> None:
        # Lazy import: at module scope this would cycle with app.py.
        from smorg.shell.app import SmorgApp

        app = self.app
        assert isinstance(app, SmorgApp)
        version = app.available_update
        assert version is not None, "_upgrade is only offered when available_update is set"

        command = upgrade_command()
        if command is None:
            app.notify(
                "smorg can't tell how it was installed — "
                f"upgrade to {version} with your own package manager"
            )
            return
        app.run_worker(lambda: _run_upgrade(app, command, version), thread=True)

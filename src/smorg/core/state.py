"""Which items have changed since you last looked at them.

Stored as state.json in the config directory: integration id -> item id -> the updated_at observed
when the item was marked seen. An item counts as changed when its updated_at has moved past that
stamp.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from smorg.core.config import config_dir, ensure_config_dir, write_private_file
from smorg.core.contract import Item


def state_path() -> Path:
    return config_dir() / "state.json"


class SeenState:
    def __init__(self, seen: dict[str, dict[str, str]]) -> None:
        self._seen = seen

    @staticmethod
    def load() -> SeenState:
        path = state_path()
        if not path.exists():
            return SeenState({})
        try:
            raw = json.loads(path.read_text())
        except (ValueError, OSError):
            # Losing highlight history is a cosmetic setback, so a corrupt or unreadable file starts
            # over rather than blocking the dashboard.
            return SeenState({})
        if not isinstance(raw, dict):
            return SeenState({})
        seen = {}
        for integration_id, entries in raw.items():
            if isinstance(entries, dict):
                # A malformed stamp is dropped on its own; it does not take the rest of that
                # integration's otherwise-valid entries with it.
                stamps = {}
                for item_id, stamp in entries.items():
                    if isinstance(item_id, str) and isinstance(stamp, str):
                        stamps[item_id] = stamp
                if stamps:
                    seen[integration_id] = stamps
        return SeenState(seen)

    def is_changed(self, integration_id: str, item: Item) -> bool:
        stamps = self._seen.get(integration_id, {})
        stamp = stamps.get(item.id)
        if stamp is None:
            return True
        try:
            return item.updated_at > datetime.fromisoformat(stamp)
        except (ValueError, TypeError):
            # degrade to "changed" over crashing the dashboard
            return True

    def mark_seen(self, integration_id: str, item: Item) -> None:
        stamps = self._seen.setdefault(integration_id, {})
        stamps[item.id] = item.updated_at.isoformat()

    def mark_all_seen(self, integration_id: str, items: Iterable[Item]) -> None:
        for item in items:
            self.mark_seen(integration_id, item)

    def mark_unseen(self, integration_id: str, item: Item) -> None:
        stamps = self._seen.get(integration_id)
        if stamps is None:
            return
        stamps.pop(item.id, None)

    def forget(self, integration_id: str) -> None:
        self._seen.pop(integration_id, None)

    def save(self) -> None:
        ensure_config_dir()
        write_private_file(state_path(), json.dumps(self._seen))

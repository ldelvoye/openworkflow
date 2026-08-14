"""Which items have changed since you last looked at them.

Stores the updated_at observed when an item was opened, not a read flag. An item
is highlighted when it has moved on since — so the highlight clears itself, and
an untouched backlog does not stay bold forever.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from oflow.config import config_dir, ensure_config_dir, write_private_file
from oflow.contract import Item


def state_path() -> Path:
    return config_dir() / "state.json"


class SeenState:
    def __init__(self, seen: dict[str, dict[str, str]]) -> None:
        self._seen = seen

    @classmethod
    def load(cls) -> SeenState:
        path = state_path()
        if not path.exists():
            return cls({})
        try:
            raw = json.loads(path.read_text())
        except (ValueError, OSError):
            # Losing highlight history is a cosmetic setback, so a corrupt or
            # unreadable file starts over rather than blocking the dashboard.
            return cls({})
        if not isinstance(raw, dict):
            return cls({})
        seen = {}
        for integration_id, entries in raw.items():
            if isinstance(entries, dict):
                # A malformed stamp is dropped on its own; it does not take the
                # rest of that integration's otherwise-valid entries with it.
                validated = {
                    k: v for k, v in entries.items() if isinstance(k, str) and isinstance(v, str)
                }
                if validated:
                    seen[integration_id] = validated
        return cls(seen)

    def is_changed(self, integration_id: str, item: Item) -> bool:
        stamp = self._seen.get(integration_id, {}).get(item.id)
        if stamp is None:
            return True
        try:
            return item.updated_at > datetime.fromisoformat(stamp)
        except (ValueError, TypeError):
            # stamp may be an invalid ISO datetime, or its awareness may not
            # match item.updated_at's — either way, degrade to "changed"
            # rather than crash the dashboard.
            return True

    def mark_seen(self, integration_id: str, item: Item) -> None:
        self._seen.setdefault(integration_id, {})[item.id] = item.updated_at.isoformat()

    def mark_all_seen(self, integration_id: str, items: Iterable[Item]) -> None:
        for item in items:
            self.mark_seen(integration_id, item)

    def save(self) -> None:
        ensure_config_dir()
        write_private_file(state_path(), json.dumps(self._seen))

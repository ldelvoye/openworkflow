from datetime import UTC, datetime, timedelta

import pytest

from oflow.contract import Item
from oflow.state import SeenState

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def item(identifier: str = "ENG-1", updated_at: datetime = NOW) -> Item:
    return Item(id=identifier, updated_at=updated_at, url="https://example.invalid/1")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OFLOW_CONFIG_DIR", str(tmp_path / "cfg"))


def test_an_unseen_item_is_changed():
    assert SeenState.load().is_changed("linear", item()) is True


def test_a_seen_item_is_not_changed():
    state = SeenState.load()
    state.mark_seen("linear", item())
    assert state.is_changed("linear", item()) is False


def test_an_item_updated_since_it_was_seen_is_changed_again():
    state = SeenState.load()
    state.mark_seen("linear", item())
    assert state.is_changed("linear", item(updated_at=NOW + timedelta(minutes=1))) is True


def test_state_is_namespaced_by_integration():
    state = SeenState.load()
    state.mark_seen("linear", item())
    assert state.is_changed("sentry", item()) is True


def test_state_survives_a_round_trip():
    state = SeenState.load()
    state.mark_seen("linear", item())
    state.save()

    assert SeenState.load().is_changed("linear", item()) is False


def test_mark_all_seen_clears_every_highlight():
    state = SeenState.load()
    items = [item("ENG-1"), item("ENG-2")]
    state.mark_all_seen("linear", items)

    assert [state.is_changed("linear", entry) for entry in items] == [False, False]


def test_a_corrupt_state_file_is_treated_as_empty():
    state = SeenState.load()
    state.mark_seen("linear", item())
    state.save()
    from oflow.state import state_path

    state_path().write_text("{not json")

    assert SeenState.load().is_changed("linear", item()) is True

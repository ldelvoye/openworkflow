from datetime import UTC, datetime, timedelta

from oflow.shell.format import age

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def test_age_of_a_future_timestamp_reads_now(monkeypatch):
    """A future stamp is clock skew, not something to render as a large past age."""
    monkeypatch.setattr("oflow.shell.format.now", lambda: NOW)
    assert age(NOW + timedelta(seconds=1)) == "now"


def test_age_of_a_moment_ago_reads_now(monkeypatch):
    monkeypatch.setattr("oflow.shell.format.now", lambda: NOW)
    assert age(NOW - timedelta(seconds=30)) == "now"


def test_age_scales_from_minutes_to_days(monkeypatch):
    monkeypatch.setattr("oflow.shell.format.now", lambda: NOW)
    assert age(NOW - timedelta(minutes=5)) == "5m"
    assert age(NOW - timedelta(hours=3)) == "3h"
    assert age(NOW - timedelta(days=2)) == "2d"

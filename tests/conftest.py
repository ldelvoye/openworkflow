import pytest


@pytest.fixture(autouse=True)
def reset_negotiated_version(monkeypatch):
    """The handshake cache is process-lifetime state; tests must not leak it."""
    monkeypatch.setattr("oflow.integrations.linear.source._negotiated_version", None)

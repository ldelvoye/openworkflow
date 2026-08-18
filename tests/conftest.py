import pytest


@pytest.fixture(autouse=True)
def reset_negotiated_version():
    """The handshake cache is process-lifetime state; tests must not leak it."""
    from smorg.core.mcp import reset_negotiated_versions

    reset_negotiated_versions()

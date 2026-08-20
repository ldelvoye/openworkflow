import importlib.metadata

import pytest

from smorg import is_dev_build


class _StubDistribution:
    """A distribution whose read_text always returns a fixed value."""

    def __init__(self, contents: str | None) -> None:
        self._contents = contents

    def read_text(self, filename: str) -> str | None:
        return self._contents


@pytest.mark.parametrize(
    "raw_direct_url,expected",
    [
        pytest.param('{"dir_info": {"editable": true}}', True, id="editable"),
        pytest.param('{"dir_info": {"editable": false}}', False, id="not-editable"),
        pytest.param("{}", False, id="no-dir-info"),
        pytest.param(None, False, id="file-missing"),
        pytest.param("not json", False, id="malformed-json"),
    ],
)
def test_is_dev_build_reads_direct_url_json(monkeypatch, raw_direct_url, expected):
    stub = _StubDistribution(raw_direct_url)
    monkeypatch.setattr("importlib.metadata.distribution", lambda name: stub)

    assert is_dev_build() is expected


def test_is_dev_build_is_false_when_the_package_is_not_installed(monkeypatch):
    def raise_not_found(name: str):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr("importlib.metadata.distribution", raise_not_found)

    assert is_dev_build() is False

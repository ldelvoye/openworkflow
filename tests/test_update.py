import sys

import httpx
import pytest

from smorg.core.contract import Malformed
from smorg.core.update import PYPI_URL, get_latest_version, is_newer, upgrade_command


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- get_latest_version() ---


def test_get_latest_version_reads_info_version():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == PYPI_URL
        return httpx.Response(200, json={"info": {"version": "1.4.0"}})

    assert get_latest_version(client_for(handler)) == "1.4.0"


def test_a_body_missing_info_version_is_malformed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"info": {"name": "smorg"}})

    with pytest.raises(Malformed):
        get_latest_version(client_for(handler))


def test_a_body_missing_info_entirely_is_malformed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with pytest.raises(Malformed):
        get_latest_version(client_for(handler))


def test_an_http_error_propagates():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(httpx.HTTPStatusError):
        get_latest_version(client_for(handler))


# --- is_newer() ---


def test_a_higher_version_is_newer():
    assert is_newer("1.2.0", "1.1.9") is True


def test_a_lower_version_is_not_newer():
    assert is_newer("1.1.0", "1.2.0") is False


def test_an_equal_version_is_not_newer():
    assert is_newer("1.2.0", "1.2.0") is False


def test_ten_beats_nine_even_though_it_sorts_lower_as_text():
    assert is_newer("1.10.0", "1.9.0") is True


@pytest.mark.parametrize(
    "latest,current",
    [
        pytest.param("1.2.1", "1.2", id="latest-has-an-extra-segment"),
        pytest.param("2.0", "1.9.9", id="current-has-an-extra-segment"),
    ],
)
def test_unequal_length_versions_compare_as_plain_tuples(latest: str, current: str):
    assert is_newer(latest, current) is True


@pytest.mark.parametrize("latest", ["1.2.3rc1", ""])
def test_a_malformed_latest_segment_is_never_newer(latest: str):
    assert is_newer(latest, "1.0.0") is False


@pytest.mark.parametrize("current", ["1.2.3rc1", ""])
def test_a_malformed_current_segment_is_never_newer(current: str):
    assert is_newer("2.0.0", current) is False


# --- upgrade_command() ---


def test_a_uv_tool_install_upgrades_via_uv(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "prefix", "/Users/x/.local/share/uv/tools/smorg")
    assert upgrade_command() == "uv tool upgrade smorg"


def test_a_pipx_install_upgrades_via_pipx(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "prefix", "/Users/x/.local/pipx/venvs/smorg")
    assert upgrade_command() == "pipx upgrade smorg"


def test_an_unrecognized_install_path_upgrades_via_nothing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "prefix", "/Users/x/code/smorg/.venv")
    assert upgrade_command() is None

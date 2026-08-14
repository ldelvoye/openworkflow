import pytest

from oflow.core.config import (
    Config,
    ConfigPermissionError,
    MalformedConfigError,
    TabConfig,
    add_tab,
    config_dir,
    config_path,
    load_config,
    save_config,
)


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OFLOW_CONFIG_DIR", str(tmp_path / "cfg"))


def test_config_dir_honours_env(tmp_path):
    assert config_dir() == tmp_path / "cfg"


def test_load_missing_config_returns_empty():
    assert load_config() == Config(tabs=())


def test_save_then_load_roundtrips():
    config = Config(tabs=(TabConfig(integration="linear", client_id="abc123"),))
    save_config(config)
    assert load_config() == config


def test_save_creates_directory_with_0700():
    save_config(Config(tabs=()))
    assert (config_dir().stat().st_mode & 0o777) == 0o700


def test_config_file_is_written_owner_only():
    save_config(Config(tabs=()))
    assert config_path().stat().st_mode & 0o777 == 0o600


def test_a_hand_broken_config_reports_rather_than_crashes():
    save_config(Config(tabs=()))
    config_path().write_text('tabs = [ { client_id = "no-integration-key" } ]')
    with pytest.raises(MalformedConfigError):
        load_config()


def test_save_refuses_a_wide_config_dir():
    save_config(Config(tabs=()))
    config_dir().chmod(0o755)
    with pytest.raises(ConfigPermissionError, match="755"):
        save_config(Config(tabs=(TabConfig(integration="linear"),)))
    assert config_dir().stat().st_mode & 0o777 == 0o755


def test_add_tab_appends_then_replaces():
    config = add_tab(Config(tabs=()), TabConfig(integration="linear", client_id="a"))
    assert config.tabs == (TabConfig(integration="linear", client_id="a"),)

    config = add_tab(config, TabConfig(integration="linear", client_id="b"))
    assert config.tabs == (TabConfig(integration="linear", client_id="b"),)


def test_tab_order_is_preserved():
    config = Config(tabs=())
    config = add_tab(config, TabConfig(integration="linear", client_id="a"))
    config = add_tab(config, TabConfig(integration="sentry", client_id="b"))
    save_config(config)
    assert [tab.integration for tab in load_config().tabs] == ["linear", "sentry"]

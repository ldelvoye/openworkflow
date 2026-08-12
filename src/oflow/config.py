"""Non-secret configuration: which tabs exist, in what order, and their client ids."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

import tomli_w

CONFIG_DIR_ENV = "OFLOW_CONFIG_DIR"


@dataclass(frozen=True)
class TabConfig:
    integration: str
    client_id: str | None = None


@dataclass(frozen=True)
class Config:
    tabs: tuple[TabConfig, ...] = ()


def config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".config" / "oflow"


def config_path() -> Path:
    return config_dir() / "config.toml"


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        return Config()
    raw = tomllib.loads(path.read_text())
    tabs = tuple(
        TabConfig(integration=entry["integration"], client_id=entry.get("client_id"))
        for entry in raw.get("tabs", [])
    )
    return Config(tabs=tabs)


def save_config(config: Config) -> None:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    payload = {
        "tabs": [
            {"integration": tab.integration}
            | ({"client_id": tab.client_id} if tab.client_id else {})
            for tab in config.tabs
        ]
    }
    temporary = config_path().with_suffix(".toml.tmp")
    temporary.write_bytes(tomli_w.dumps(payload).encode())
    temporary.replace(config_path())


def add_tab(config: Config, tab: TabConfig) -> Config:
    if any(existing.integration == tab.integration for existing in config.tabs):
        return Config(
            tabs=tuple(
                tab if existing.integration == tab.integration else existing
                for existing in config.tabs
            )
        )
    return Config(tabs=config.tabs + (tab,))

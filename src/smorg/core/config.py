"""Non-secret configuration: which tabs exist, in what order, and their client ids.

Also owns the config directory itself, since credentials live alongside this file and both must
agree on who may read it.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import tomli_w

if TYPE_CHECKING:
    # Deferred: contract.py's import chain (auth.oauth -> auth.store) loops back to this module,
    # so importing for real would cycle; Manifest/AuthPath are only used in type positions.
    from smorg.core.contract import AuthPath, Manifest

CONFIG_DIR_ENV = "SMORG_CONFIG_DIR"
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600


@dataclass(frozen=True)
class TabConfig:
    integration: str
    client_id: str | None = None
    connection: str | None = None


@dataclass(frozen=True)
class Config:
    tabs: tuple[TabConfig, ...] = ()


class ConfigError(Exception):
    """Base class for anything that stops configuration being read or written."""


class ConfigPermissionError(ConfigError):
    """The config directory is readable by someone other than its owner."""


class MalformedConfigError(ConfigError):
    """The config file exists but could not be understood."""


def config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".config" / "smorg"


def config_path() -> Path:
    return config_dir() / "config.toml"


def require_private_path(path: Path, expected_mode: int) -> None:
    """Refuse a path that is not exclusively ours."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ConfigPermissionError(f"{path} is a symlink; refusing to follow it")
    if info.st_uid != os.getuid():
        raise ConfigPermissionError(
            f"{path} is owned by uid {info.st_uid}, not by you (uid {os.getuid()})"
        )
    mode = stat.S_IMODE(info.st_mode)
    if mode != expected_mode:
        raise ConfigPermissionError(
            f"{path} has mode {mode:o}, expected {expected_mode:o}. "
            f"Fix it with: chmod {expected_mode:o} {path}"
        )


def require_config_dir_permissions() -> None:
    directory = config_dir()
    # is_symlink covers a dangling symlink, which exists() reports as absent.
    if not directory.exists() and not directory.is_symlink():
        return
    require_private_path(directory, DIRECTORY_MODE)


def ensure_config_dir() -> Path:
    directory = config_dir()
    require_config_dir_permissions()
    if not directory.exists():
        directory.mkdir(parents=True, mode=DIRECTORY_MODE)
        # mkdir's mode is masked by the process umask; chmod is what actually guarantees the bits.
        directory.chmod(DIRECTORY_MODE)
    return directory


def write_private_file(path: Path, data: str) -> None:
    """Replace a file atomically, without ever widening its permissions."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        os.fchmod(descriptor, FILE_MODE)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(data)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(path)


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        return Config()
    try:
        raw = tomllib.loads(path.read_text())
        entries = raw.get("tabs", [])
        tabs = tuple(
            TabConfig(
                integration=entry["integration"],
                client_id=entry.get("client_id"),
                connection=entry.get("connection"),
            )
            for entry in entries
        )
    except (tomllib.TOMLDecodeError, KeyError, TypeError, AttributeError) as error:
        raise MalformedConfigError(
            f"{path} could not be read ({error}). Fix it by hand or delete it to start over."
        ) from error
    return Config(tabs=tabs)


def save_config(config: Config) -> None:
    ensure_config_dir()
    entries = []
    for tab in config.tabs:
        entry = {"integration": tab.integration}
        # TOML has no null: an absent client_id/connection is omitted, not emitted.
        if tab.client_id:
            entry["client_id"] = tab.client_id
        if tab.connection:
            entry["connection"] = tab.connection
        entries.append(entry)
    payload = {"tabs": entries}
    write_private_file(config_path(), tomli_w.dumps(payload))


def add_tab(config: Config, tab: TabConfig) -> Config:
    """Replace this integration's entry if present, else append it, keeping order."""
    for index, existing in enumerate(config.tabs):
        if existing.integration == tab.integration:
            replaced = config.tabs[:index] + (tab,) + config.tabs[index + 1 :]
            return Config(tabs=replaced)
    return Config(tabs=config.tabs + (tab,))


def tab_for(config: Config, integration_id: str) -> TabConfig | None:
    for tab in config.tabs:
        if tab.integration == integration_id:
            return tab
    return None


def resolve_connection(manifest: Manifest, tab: TabConfig | None) -> tuple[AuthPath, str | None]:
    """The connection path a possibly-unconfigured tab uses, and its client id. Raises ValueError
    when tab.connection names a path no longer declared.
    """
    if tab is None:
        recorded_connection = None
        client_id = None
    else:
        recorded_connection = tab.connection
        client_id = tab.client_id
    path = manifest.connection(recorded_connection)
    return path, client_id

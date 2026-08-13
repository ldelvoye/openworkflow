"""Non-secret configuration: which tabs exist, in what order, and their client ids.

Also owns the config directory itself, since credentials live alongside this file
and both must agree on who may read it.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

import tomli_w

CONFIG_DIR_ENV = "OFLOW_CONFIG_DIR"

# The modes every path under the config directory must have. They live together
# because the guarantee is a single one — nothing here is readable by anyone but
# its owner — and splitting them across modules invites the two to drift.
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600


class ConfigError(Exception):
    """Base class for anything that stops configuration being read or written."""


class ConfigPermissionError(ConfigError):
    """The config directory is readable by someone other than its owner."""


class MalformedConfigError(ConfigError):
    """The config file exists but could not be understood."""


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


def require_private_path(path: Path, expected_mode: int) -> None:
    """Refuse a path that is not exclusively ours.

    Problems are reported rather than repaired: a silent chmod would erase the
    only evidence that the credentials stored here had been reachable by anyone
    else. Uses lstat so a symlink is rejected on its own terms instead of being
    followed to whatever it points at.
    """
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
        # mkdir's mode argument is masked by the process umask; chmod is what
        # actually guarantees the bits.
        directory.chmod(DIRECTORY_MODE)
    return directory


def write_private_file(path: Path, data: str) -> None:
    """Replace a file atomically, never widening it even briefly.

    Used for everything under the config directory so one idiom covers both the
    non-secret config and the credentials file — a second, laxer idiom is how a
    plaintext token ends up world-readable for the width of a rename.

    O_EXCL refuses a pre-existing temp file or a symlink planted at that path.
    The creation mode is masked by the umask, which can only narrow it — narrow
    enough to fail our own read checks — so fchmod pins the exact bits.
    """
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
        tabs = tuple(
            TabConfig(integration=entry["integration"], client_id=entry.get("client_id"))
            for entry in raw.get("tabs", [])
        )
    except (tomllib.TOMLDecodeError, KeyError, TypeError, AttributeError) as error:
        raise MalformedConfigError(
            f"{path} could not be read ({error}). Fix it by hand or delete it to start over."
        ) from error
    return Config(tabs=tabs)


def save_config(config: Config) -> None:
    ensure_config_dir()
    payload = {
        "tabs": [
            # TOML has no null, so an absent client_id is omitted rather than emitted.
            {"integration": tab.integration}
            | ({"client_id": tab.client_id} if tab.client_id else {})
            for tab in config.tabs
        ]
    }
    write_private_file(config_path(), tomli_w.dumps(payload))


def add_tab(config: Config, tab: TabConfig) -> Config:
    """Replace this integration's entry if present, else append it, keeping order."""
    for index, existing in enumerate(config.tabs):
        if existing.integration == tab.integration:
            return Config(tabs=config.tabs[:index] + (tab,) + config.tabs[index + 1 :])
    return Config(tabs=config.tabs + (tab,))

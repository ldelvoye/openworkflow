"""Whether the running `smorg` executable is reachable by name, and how to fix it if not."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

_UNSTABLE_PARENTS = {".venv", ".cache"}


@dataclass(frozen=True)
class ShellSetup:
    """The rc file to edit and the exact line to add, for one shell."""

    rc_file: Path
    line: str


def bin_dir_needing_setup() -> Path | None:
    """The directory to add to PATH so `smorg` resolves by name, or None if nothing to do."""
    on_path = shutil.which("smorg")
    if on_path is not None:
        return None

    executable = Path(sys.argv[0]).absolute()
    if not executable.exists():
        return None

    bin_dir = executable.parent
    if _UNSTABLE_PARENTS & set(bin_dir.parts):
        return None

    if (bin_dir.parent / "pyvenv.cfg").exists():
        # A bin dir sitting next to pyvenv.cfg is a virtualenv's own bin.
        return None

    return bin_dir


def shell_setup(shell: str, bin_dir: Path) -> ShellSetup | None:
    shell_name = Path(shell).name
    if shell_name == "zsh":
        rc_file = Path.home() / ".zshrc"
        line = f'export PATH="{bin_dir}:$PATH"'
    elif shell_name == "bash":
        rc_file = Path.home() / ".bashrc"
        line = f'export PATH="{bin_dir}:$PATH"'
    elif shell_name == "fish":
        rc_file = Path.home() / ".config" / "fish" / "config.fish"
        line = f"fish_add_path {bin_dir}"
    else:
        return None
    return ShellSetup(rc_file=rc_file, line=line)


def append_once(rc_file: Path, line: str) -> bool:
    if rc_file.exists():
        existing = rc_file.read_text()
    else:
        existing = None

    if existing is not None and line in existing.splitlines():
        return False

    if existing and not existing.endswith("\n"):
        separator = "\n"
    else:
        separator = ""

    rc_file.parent.mkdir(parents=True, exist_ok=True)
    with rc_file.open("a") as handle:
        handle.write(separator + line + "\n")
    return True

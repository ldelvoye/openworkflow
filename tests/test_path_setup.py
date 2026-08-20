from pathlib import Path

from smorg.core.path_setup import ShellSetup, append_once, bin_dir_needing_setup, shell_setup

# --- shell_setup ---


def test_shell_setup_for_zsh():
    result = shell_setup("/bin/zsh", Path("/opt/smorg/bin"))
    assert result == ShellSetup(
        rc_file=Path.home() / ".zshrc",
        line='export PATH="/opt/smorg/bin:$PATH"',
    )


def test_shell_setup_for_bash():
    result = shell_setup("/bin/bash", Path("/opt/smorg/bin"))
    assert result == ShellSetup(
        rc_file=Path.home() / ".bashrc",
        line='export PATH="/opt/smorg/bin:$PATH"',
    )


def test_shell_setup_for_fish():
    result = shell_setup("/usr/local/bin/fish", Path("/opt/smorg/bin"))
    assert result == ShellSetup(
        rc_file=Path.home() / ".config" / "fish" / "config.fish",
        line="fish_add_path /opt/smorg/bin",
    )


def test_shell_setup_for_an_unknown_shell_is_none():
    assert shell_setup("/bin/tcsh", Path("/opt/smorg/bin")) is None


def test_shell_setup_for_an_empty_shell_is_none():
    assert shell_setup("", Path("/opt/smorg/bin")) is None


# --- append_once ---


def test_append_once_creates_a_missing_file_and_its_parents(tmp_path):
    rc_file = tmp_path / "nested" / "config.fish"

    wrote = append_once(rc_file, "fish_add_path /opt/smorg/bin")

    assert wrote is True
    assert rc_file.read_text() == "fish_add_path /opt/smorg/bin\n"


def test_append_once_is_idempotent_when_the_line_is_already_present(tmp_path):
    rc_file = tmp_path / ".zshrc"
    rc_file.write_text('export PATH="/opt/smorg/bin:$PATH"\n')

    wrote = append_once(rc_file, 'export PATH="/opt/smorg/bin:$PATH"')

    assert wrote is False
    assert rc_file.read_text() == 'export PATH="/opt/smorg/bin:$PATH"\n'


def test_append_once_adds_a_separating_newline_when_the_file_lacks_a_trailing_one(tmp_path):
    rc_file = tmp_path / ".bashrc"
    rc_file.write_text("alias ll='ls -la'")

    wrote = append_once(rc_file, 'export PATH="/opt/smorg/bin:$PATH"')

    assert wrote is True
    assert rc_file.read_text() == "alias ll='ls -la'\nexport PATH=\"/opt/smorg/bin:$PATH\"\n"


def test_append_once_does_not_add_a_separator_when_the_file_already_ends_in_a_newline(tmp_path):
    rc_file = tmp_path / ".bashrc"
    rc_file.write_text("alias ll='ls -la'\n")

    append_once(rc_file, 'export PATH="/opt/smorg/bin:$PATH"')

    assert rc_file.read_text() == "alias ll='ls -la'\nexport PATH=\"/opt/smorg/bin:$PATH\"\n"


# --- bin_dir_needing_setup ---


def test_bin_dir_needing_setup_is_none_when_smorg_is_already_on_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/smorg")

    assert bin_dir_needing_setup() is None


def test_bin_dir_needing_setup_is_none_under_a_venv_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name: None)
    executable = tmp_path / "repo" / ".venv" / "bin" / "smorg"
    executable.parent.mkdir(parents=True)
    executable.write_text("")
    monkeypatch.setattr("sys.argv", [str(executable)])

    assert bin_dir_needing_setup() is None


def test_bin_dir_needing_setup_is_none_under_a_uvx_cache(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name: None)
    executable = tmp_path / ".cache" / "uv" / "tools" / "smorg" / "bin" / "smorg"
    executable.parent.mkdir(parents=True)
    executable.write_text("")
    monkeypatch.setattr("sys.argv", [str(executable)])

    assert bin_dir_needing_setup() is None


def test_bin_dir_needing_setup_returns_the_directory_for_a_plain_install(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name: None)
    executable = tmp_path / "opt" / "smorg" / "bin" / "smorg"
    executable.parent.mkdir(parents=True)
    executable.write_text("")
    monkeypatch.setattr("sys.argv", [str(executable)])

    assert bin_dir_needing_setup() == executable.parent


def test_bin_dir_needing_setup_is_none_next_to_a_pyvenv_cfg(monkeypatch, tmp_path):
    """A bin dir sitting next to pyvenv.cfg is a virtualenv's own bin/, regardless of naming —
    covers a directly-executed pipx/uv tool venv that the .venv/.cache name guard would miss.
    """
    monkeypatch.setattr("shutil.which", lambda name: None)
    venv_dir = tmp_path / "pipx" / "venvs" / "smorg"
    executable = venv_dir / "bin" / "smorg"
    executable.parent.mkdir(parents=True)
    executable.write_text("")
    (venv_dir / "pyvenv.cfg").write_text("")
    monkeypatch.setattr("sys.argv", [str(executable)])

    assert bin_dir_needing_setup() is None


def test_bin_dir_needing_setup_returns_the_symlinks_directory_not_its_target(monkeypatch, tmp_path):
    """A pipx-style launcher symlinks a plain bin dir entry into a venv's internal bin/. The
    directory worth adding to PATH is the one holding the invoked name, not the venv it resolves
    to — prepending the venv's own bin/ to PATH would shadow the user's python.
    """
    monkeypatch.setattr("shutil.which", lambda name: None)
    venv_bin = tmp_path / "pipx" / "venvs" / "smorg" / "bin"
    venv_bin.mkdir(parents=True)
    target = venv_bin / "smorg"
    target.write_text("")
    (venv_bin.parent / "pyvenv.cfg").write_text("")

    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    symlink = local_bin / "smorg"
    symlink.symlink_to(target)
    monkeypatch.setattr("sys.argv", [str(symlink)])

    assert bin_dir_needing_setup() == local_bin

"""Checking PyPI for a newer smorg release, and how to upgrade an existing install."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

from smorg.core.shape import required_string

PYPI_URL = "https://pypi.org/pypi/smorg/json"


def get_latest_version(http: httpx.Client) -> str:
    response = http.get(PYPI_URL)
    response.raise_for_status()
    body = response.json()
    info = body.get("info", {})
    return required_string(info, "version")


def is_newer(latest: str, current: str) -> bool:
    latest_parts = _integer_parts(latest)
    current_parts = _integer_parts(current)
    if latest_parts is None or current_parts is None:
        return False
    return latest_parts > current_parts


def _integer_parts(version: str) -> tuple[int, ...] | None:
    segments = version.split(".")
    parts: list[int] = []
    for segment in segments:
        if not segment.isdigit():
            return None
        parts.append(int(segment))
    return tuple(parts)


def upgrade_command() -> str | None:
    parts = Path(sys.prefix).parts
    consecutive_pairs = list(zip(parts, parts[1:], strict=False))
    if ("uv", "tools") in consecutive_pairs:
        return "uv tool upgrade smorg"
    if "pipx" in parts and "venvs" in parts:
        return "pipx upgrade smorg"
    return None

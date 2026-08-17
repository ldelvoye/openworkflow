"""Turning untrusted server JSON into typed fields, or a Malformed tab.

A field this build depends on is either exactly the type expected or the
whole response is untrusted — there is no silent coercion. Each helper
raises Malformed naming the field, so a shape mismatch degrades one tab
rather than crashing with a bare KeyError or TypeError.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from oflow.core.contract import Malformed
from oflow.core.text import printable


def required_string(raw: dict[str, Any], key: str) -> str:
    """A field a caller renders unconditionally: absent or non-str is Malformed."""
    value = raw.get(key)
    if not isinstance(value, str):
        raise Malformed(f"{key!r} was {type(value).__name__}, expected a string")
    return value


def optional_string(raw: dict[str, Any], key: str) -> str:
    """Optional field: null/absent becomes "", a present wrong type is still Malformed."""
    value = raw.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise Malformed(f"{key!r} was {type(value).__name__}, expected a string")
    return value


def timestamp(raw: dict[str, Any], key: str) -> datetime:
    """A field that must parse as an ISO-8601 timestamp, or the shape is untrusted."""
    try:
        return datetime.fromisoformat(raw[key])
    except (KeyError, TypeError, ValueError) as error:
        raise Malformed(f"{key!r} was not a valid timestamp ({printable(str(error))})") from error

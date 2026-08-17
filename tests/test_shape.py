from datetime import UTC, datetime

import pytest

from oflow.core.contract import Malformed
from oflow.core.shape import optional_string, required_string, timestamp


def test_required_string_returns_the_value():
    assert required_string({"key": "value"}, "key") == "value"


def test_required_string_raises_on_a_missing_key():
    with pytest.raises(Malformed, match="'key'"):
        required_string({}, "key")


def test_required_string_raises_on_a_null_value():
    with pytest.raises(Malformed):
        required_string({"key": None}, "key")


def test_required_string_raises_on_the_wrong_type():
    with pytest.raises(Malformed):
        required_string({"key": 42}, "key")


def test_optional_string_returns_the_value():
    assert optional_string({"key": "value"}, "key") == "value"


def test_optional_string_defaults_to_empty_when_absent():
    assert optional_string({}, "key") == ""


def test_optional_string_defaults_to_empty_when_null():
    assert optional_string({"key": None}, "key") == ""


def test_optional_string_raises_on_the_wrong_type():
    with pytest.raises(Malformed):
        optional_string({"key": 42}, "key")


def test_timestamp_parses_an_iso_string():
    parsed = timestamp({"key": "2026-08-13T12:00:00+00:00"}, "key")
    assert parsed == datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def test_timestamp_raises_on_a_missing_key():
    with pytest.raises(Malformed, match="'key'"):
        timestamp({}, "key")


def test_timestamp_raises_on_an_unparseable_value():
    with pytest.raises(Malformed):
        timestamp({"key": "not a date"}, "key")


def test_timestamp_raises_on_the_wrong_type():
    with pytest.raises(Malformed):
        timestamp({"key": 12345}, "key")

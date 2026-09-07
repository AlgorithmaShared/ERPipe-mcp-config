"""The JSON-text escape hatch must exist on the tools that commit the write.

preview_write and validate_write accept values_json; update_record and
execute_approved_write did not. The agent prompt instructs the model to
*always* send values_json - because the object field arrives empty through the
chat - so the documented flow failed on its final, committing call with a
pydantic validation error the user saw as "es cant delete/verschieben".
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from algorithma_workflows import plugin


def test_json_text_is_accepted() -> None:
    out = plugin._coerce_values(None, '{"start": "2026-09-09 10:00"}')
    assert out == {"start": "2026-09-09 10:00"}


def test_object_still_works() -> None:
    """Direct Python callers pass a dict; that must keep working."""
    out = plugin._coerce_values({"name": "x"}, None)
    assert out == {"name": "x"}


def test_pre_parsed_dict_in_the_json_field() -> None:
    """The MCP layer pre-parses arguments that look like JSON."""
    out = plugin._coerce_values(None, {"name": "x"})
    assert out == {"name": "x"}


def test_object_wins_when_both_given() -> None:
    out = plugin._coerce_values({"a": 1}, '{"b": 2}')
    assert out == {"a": 1}


def test_empty_object_falls_back_to_json_text() -> None:
    """The exact failure: values={} arrives, the real payload is in the text."""
    out = plugin._coerce_values({}, '{"start": "2026-09-09 10:00"}')
    assert out == {"start": "2026-09-09 10:00"}


def test_invalid_json_is_reported_clearly() -> None:
    with pytest.raises(ValueError, match="kein gueltiges JSON"):
        plugin._coerce_values(None, "{not json")


def test_json_array_is_rejected() -> None:
    with pytest.raises(ValueError, match="JSON-Objekt"):
        plugin._coerce_values(None, "[1, 2]")


def test_nothing_given_stays_none() -> None:
    assert plugin._coerce_values(None, None) is None


def test_update_record_signature_accepts_values_json() -> None:
    """Guard the schema itself: the parameter must survive refactors."""
    import inspect

    params = inspect.signature(plugin.update_record).parameters
    assert "values_json" in params
    assert params["values"].default is None, (
        "values must be optional, or values_json is unreachable"
    )

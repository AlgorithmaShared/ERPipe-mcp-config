"""Timezone handling on the generic write path.

The bug these pin: creating an appointment for 10:00 landed correctly, but
moving it to 10:00 landed at 12:00. termin_buchen converted local time to UTC;
update_record passed the naive string straight to Odoo, which reads naive
datetimes as UTC and renders them in the user's timezone (+2h in Zurich in
summer). Same user, same time, two different results depending on which tool
ran.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict

import pytest

from algorithma_workflows import plugin


class _FakeOdoo:
    """Answers get_model_fields the way Odoo does, and records nothing else."""

    def __init__(
        self, fields: Dict[str, Dict[str, str]] | None = None, fail: bool = False
    ):
        self._fields = fields or {}
        self._fail = fail

    def get_model_fields(self, model: str) -> Dict[str, Any]:
        if self._fail:
            raise RuntimeError("fields_get exploded")
        return self._fields


CAL_FIELDS = {
    "start": {"type": "datetime"},
    "stop": {"type": "datetime"},
    "name": {"type": "char"},
    "allday": {"type": "boolean"},
}


@pytest.fixture(autouse=True)
def _zurich(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALGORITHMA_TZ", "Europe/Zurich")


def test_naive_local_time_becomes_utc() -> None:
    """10:00 Zurich in summer is 08:00 UTC - the bug that shifted appointments."""
    odoo = _FakeOdoo(CAL_FIELDS)

    out = plugin.localize_datetime_values(
        odoo,
        "calendar.event",
        {"start": "2026-09-09 10:00", "stop": "2026-09-09 11:00"},
    )

    assert out["start"].startswith("2026-09-09 08:00")
    assert out["stop"].startswith("2026-09-09 09:00")


def test_winter_offset_is_one_hour() -> None:
    """CET, not CEST: the offset is not a constant."""
    odoo = _FakeOdoo(CAL_FIELDS)

    out = plugin.localize_datetime_values(
        odoo, "calendar.event", {"start": "2026-01-15 10:00"}
    )

    assert out["start"].startswith("2026-01-15 09:00")


def test_value_with_offset_is_not_shifted_again() -> None:
    """termin_buchen already converted; converting twice was the risk."""
    odoo = _FakeOdoo(CAL_FIELDS)
    already = plugin.to_odoo_utc_marked(plugin.parse_local_datetime("2026-09-09 10:00"))

    out = plugin.localize_datetime_values(odoo, "calendar.event", {"start": already})

    assert out["start"].startswith("2026-09-09 08:00")


def test_non_datetime_fields_untouched() -> None:
    odoo = _FakeOdoo(CAL_FIELDS)

    out = plugin.localize_datetime_values(
        odoo, "calendar.event", {"name": "Zahnarzttermin", "allday": False}
    )

    assert out == {"name": "Zahnarzttermin", "allday": False}


def test_date_fields_are_left_alone() -> None:
    """A calendar day has no timezone; shifting it moves it to the wrong day."""
    odoo = _FakeOdoo({"date_deadline": {"type": "date"}})

    out = plugin.localize_datetime_values(
        odoo, "project.task", {"date_deadline": "2026-09-09"}
    )

    assert out["date_deadline"] == "2026-09-09"


def test_datetime_on_any_model_is_converted() -> None:
    """Not just calendar.event - the fix is schema-driven, not a field list."""
    odoo = _FakeOdoo({"date_order": {"type": "datetime"}})

    out = plugin.localize_datetime_values(
        odoo, "sale.order", {"date_order": "2026-09-09 14:30"}
    )

    assert out["date_order"].startswith("2026-09-09 12:30")


def test_unparseable_value_passes_through() -> None:
    """Odoo should reject bad input with its own message, not be handed a guess."""
    odoo = _FakeOdoo(CAL_FIELDS)

    out = plugin.localize_datetime_values(
        odoo, "calendar.event", {"start": "next tuesday"}
    )

    assert out["start"] == "next tuesday"


def test_field_lookup_failure_never_blocks_a_write() -> None:
    odoo = _FakeOdoo(fail=True)
    values = {"start": "2026-09-09 10:00"}

    assert plugin.localize_datetime_values(odoo, "calendar.event", values) == values


def test_empty_values_short_circuit() -> None:
    assert (
        plugin.localize_datetime_values(_FakeOdoo(CAL_FIELDS), "calendar.event", {})
        == {}
    )


def test_create_and_move_agree() -> None:
    """The regression itself: both paths must land on the same UTC value."""
    odoo = _FakeOdoo(CAL_FIELDS)

    # what termin_buchen produces
    created = plugin.to_odoo_utc_marked(plugin.parse_local_datetime("2026-09-09 10:00"))
    created_utc = plugin.localize_datetime_values(
        odoo, "calendar.event", {"start": created}
    )["start"]

    # what update_record produces for the same wall-clock time
    moved_utc = plugin.localize_datetime_values(
        odoo, "calendar.event", {"start": "2026-09-09 10:00"}
    )["start"]

    assert created_utc[:16] == moved_utc[:16] == "2026-09-09 08:00"

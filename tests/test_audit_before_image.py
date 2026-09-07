"""Before-image capture on destructive writes.

A delete is the one operation whose evidence disappears with the data. These
tests pin the behaviour that makes "the customer wants it back" answerable:
the audit entry must carry what the record held, the snapshot must respect the
field ACL, and a snapshot failure must never stop the delete.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from odoo_mcp import audit


class _FakeOdoo:
    """Minimal Odoo double: records read/unlink calls, replays canned rows."""

    def __init__(self, rows: List[Dict[str, Any]] | None = None, fail: bool = False):
        self._rows = rows if rows is not None else []
        self._fail = fail
        self.calls: List[tuple[str, str, tuple[Any, ...]]] = []

    def execute_method(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((model, method, args))
        if method == "read":
            if self._fail:
                raise RuntimeError("odoo read blew up")
            return self._rows
        if method == "unlink":
            return True
        raise AssertionError(f"unexpected method {method}")


def test_capture_returns_field_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """The snapshot carries the values, not just the id."""
    odoo = _FakeOdoo([{"id": 7, "name": "Offerte Muller AG", "amount_total": 4200.0}])

    image = audit.capture_before_image(odoo, "sale.order", [7])

    assert image is not None
    assert image[0]["name"] == "Offerte Muller AG"
    assert image[0]["amount_total"] == 4200.0
    assert ("sale.order", "read", ([7],)) in odoo.calls


def test_capture_drops_noise_fields() -> None:
    """Chatter back-references and write bookkeeping stay out of the log."""
    odoo = _FakeOdoo(
        [
            {
                "id": 7,
                "name": "keep me",
                "message_ids": [1, 2, 3],
                "write_date": "2026-09-07",
                "access_token": "secret",
            }
        ]
    )

    image = audit.capture_before_image(odoo, "sale.order", [7])

    assert image is not None
    assert image[0] == {"id": 7, "name": "keep me"}


def test_capture_truncates_huge_values() -> None:
    """One deleted report must not flood the audit log."""
    odoo = _FakeOdoo([{"id": 1, "body": "x" * 9000}])

    image = audit.capture_before_image(odoo, "account.move", [1])

    assert image is not None
    assert len(image[0]["body"]) < 9000
    assert "truncated" in image[0]["body"]


def test_capture_skips_bulk_deletes() -> None:
    """Beyond max_records the snapshot is skipped, not silently partial."""
    odoo = _FakeOdoo([{"id": i} for i in range(200)])

    image = audit.capture_before_image(
        odoo, "res.partner", list(range(200)), max_records=50
    )

    assert image is None


def test_capture_never_raises() -> None:
    """A broken read must not become the reason a delete fails."""
    odoo = _FakeOdoo(fail=True)

    image = audit.capture_before_image(odoo, "sale.order", [7])

    assert image is None


def test_capture_honours_field_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A snapshot cannot surface a field the caller could not read."""

    class _DenyingPolicy:
        def redact_records(
            self, instance: str, model: str, records: Any
        ) -> tuple[List[Dict[str, Any]], List[str]]:
            out = [{k: v for k, v in r.items() if k != "ssnid"} for r in records]
            return out, ["ssnid"]

    monkeypatch.setattr(
        "odoo_mcp.field_policy.get_field_policy", lambda: _DenyingPolicy()
    )
    odoo = _FakeOdoo([{"id": 3, "name": "Employee", "ssnid": "756.1234"}])

    image = audit.capture_before_image(odoo, "hr.employee", [3])

    assert image is not None
    assert "ssnid" not in image[0]
    assert image[0]["name"] == "Employee"


def test_audit_entry_carries_before_image(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The written JSONL line contains the snapshot."""
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv(audit.AUDIT_LOG_ENV, str(log))

    written = audit.record_write_event(
        "execute_approved_write",
        outcome="success",
        model="sale.order",
        operation="unlink",
        record_ids=[7],
        before_image=[{"id": 7, "name": "Offerte Muller AG"}],
    )

    assert written is True
    entry = json.loads(log.read_text(encoding="utf-8").strip())
    assert entry["operation"] == "unlink"
    assert entry["before_image"][0]["name"] == "Offerte Muller AG"


def test_audit_entry_without_before_image_omits_key(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-destructive events keep their existing shape."""
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv(audit.AUDIT_LOG_ENV, str(log))

    audit.record_write_event(
        "validate_write",
        outcome="success",
        model="sale.order",
        operation="create",
    )

    entry = json.loads(log.read_text(encoding="utf-8").strip())
    assert "before_image" not in entry

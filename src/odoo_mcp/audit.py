"""Append-only JSONL audit trail for write-path events.

Opt-in via ODOO_MCP_AUDIT_LOG=<path>. One line per event so operators can
answer "what did the agent change, when, and was it approved?" without
trusting chat transcripts. Tokens are stored as truncated SHA-256 digests,
never in clear text.

Audit failures never block the underlying operation (fail-open); they are
logged as warnings instead. Operators who need fail-closed semantics should
alert on the warning log line.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

AUDIT_LOG_ENV = "ODOO_MCP_AUDIT_LOG"
_write_lock = threading.Lock()


def audit_log_path() -> str | None:
    """Return the configured audit log path, or None when disabled."""
    path = os.environ.get(AUDIT_LOG_ENV, "").strip()
    return path or None


def audit_posture() -> dict[str, Any]:
    """Non-secret audit posture for health_check / runtime_security_report."""
    path = audit_log_path()
    return {"enabled": path is not None, "path": path, "env": AUDIT_LOG_ENV}


def _token_digest(token: str | None) -> str | None:
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def record_write_event(
    event: str,
    *,
    outcome: str,
    model: str | None = None,
    operation: str | None = None,
    record_ids: list[int] | None = None,
    instance: str | None = None,
    token: str | None = None,
    detail: str | None = None,
    principal: str | None = None,
    client_user_id: str | None = None,
    identity: Optional[Mapping[str, Optional[str]]] = None,
    before_image: list[dict[str, Any]] | None = None,
) -> bool:
    """Append one audit line; returns True when a line was written.

    ``principal`` is the acting Odoo login and ``client_user_id`` the opaque
    front-end user id when the server runs in request identity mode; both are
    None in configured mode. ``identity`` is a convenience mapping with those
    two keys (see ``RequestIdentity.audit_fields``). Credentials are never
    accepted here by design.

    ``before_image`` holds the field values a record carried immediately
    before a destructive operation. Without it an unlink entry proves only
    that some record id stopped existing, which cannot tell a customer what
    they lost or let anyone rebuild it. The caller is responsible for passing
    values that already went through the field ACL, so the snapshot never
    exposes a field the acting user could not have read.
    """
    path = audit_log_path()
    if path is None:
        return False
    if identity:
        principal = principal or identity.get("principal")
        client_user_id = client_user_id or identity.get("client_user_id")
    entry: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "outcome": outcome,
        "model": model,
        "operation": operation,
        "record_ids": list(record_ids or []),
        "instance": instance or "default",
        "principal": principal,
        "client_user_id": client_user_id,
        "token_sha256": _token_digest(token),
        "detail": detail,
    }
    if before_image is not None:
        entry["before_image"] = before_image
    try:
        line = json.dumps(entry, sort_keys=True, default=str)
        with _write_lock:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return True
    except OSError as exc:
        logger.warning("audit log write failed (%s): %s", path, exc)
        return False


# Fields that would bloat every snapshot without helping anyone rebuild a
# record: Odoo's own bookkeeping and the chatter back-references.
_SNAPSHOT_SKIP_FIELDS = frozenset(
    {
        "__last_update",
        "access_token",
        "access_url",
        "access_warning",
        "activity_ids",
        "message_follower_ids",
        "message_ids",
        "website_message_ids",
        "write_date",
        "write_uid",
    }
)

# A snapshot is a safety net, not an export. Very large field values (report
# HTML, base64 attachments) are replaced by a marker so the audit log stays
# readable and bounded.
_SNAPSHOT_MAX_VALUE_CHARS = 4000


def _truncate_snapshot_value(value: Any) -> Any:
    """Shorten oversized values so one delete cannot flood the audit log."""
    if isinstance(value, str) and len(value) > _SNAPSHOT_MAX_VALUE_CHARS:
        return (
            value[:_SNAPSHOT_MAX_VALUE_CHARS]
            + f"... [truncated, {len(value)} chars total]"
        )
    return value


def capture_before_image(
    odoo: Any,
    model: str,
    record_ids: list[int],
    *,
    instance: str = "default",
    max_records: int = 50,
) -> list[dict[str, Any]] | None:
    """Read records as they are right now, for the audit trail.

    Called immediately before a destructive operation so the audit entry can
    answer "what was actually in there?" once the rows are gone. Returns None
    when nothing could be read; the caller must treat that as "no snapshot"
    and never as "the record was empty".

    Values pass through the field policy exactly like a normal read, so a
    snapshot can never surface a field the acting user was not allowed to see.

    Failures here never propagate: a missing snapshot must not be the reason a
    user cannot delete their own data. The trade-off is deliberate and it is
    why the caller records ``before_image_captured`` alongside the values.
    """
    if not record_ids:
        return None
    if len(record_ids) > max_records:
        # Bulk deletes are rare and usually scripted; snapshotting thousands of
        # rows into a JSONL line helps nobody. Record the ids and move on.
        logger.info(
            "before-image skipped for %s: %d records exceeds max_records=%d",
            model,
            len(record_ids),
            max_records,
        )
        return None
    try:
        from .field_policy import get_field_policy

        records = odoo.execute_method(model, "read", record_ids)
        if not isinstance(records, list):
            return None
        redacted, _ = get_field_policy().redact_records(instance, model, records)
        return [
            {
                key: _truncate_snapshot_value(value)
                for key, value in record.items()
                if key not in _SNAPSHOT_SKIP_FIELDS
            }
            for record in redacted
            if isinstance(record, dict)
        ]
    except Exception as exc:  # noqa: BLE001 - never block the delete
        logger.warning(
            "before-image capture failed for %s %s: %s", model, record_ids, exc
        )
        return None

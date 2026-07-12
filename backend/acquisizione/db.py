"""SQLite persistence for seller-meeting intake records.

One table — intake_records — lives on the SAME connection as the tenants
registry (see tenants/db.py), reusing its process-wide connection and write
lock, exactly like calls/db.py and leadgen/db.py. Every row carries tenant_id:
it is the scoping column for the agency dashboard, and every read here filters
on it so one tenant can never see another's meeting.

No audio is ever stored — only the transcript text and the derived listing
data (see acquisizione/router.py for the flow: consent → live transcript →
extraction → review → confirm).
"""

import datetime
import json
import logging
import uuid
from typing import Any, Optional

from tenants import db as _tenants_db

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intake_records (
  id                 TEXT PRIMARY KEY,
  tenant_id          TEXT NOT NULL,
  market             TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'transcribing',
  consent_given_at   TEXT,
  consent_method     TEXT,
  transcript         TEXT NOT NULL DEFAULT '',
  listing_fields     TEXT,
  missing_required   TEXT,
  listing_text       TEXT,
  tasks              TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  confirmed_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_intake_records_tenant ON intake_records(tenant_id, created_at);
"""

_JSON_FIELDS = ("listing_fields", "missing_required", "tasks")

_initialized = False


def init() -> None:
    """Create the intake_records table on the shared connection (idempotent)."""
    global _initialized
    conn = _tenants_db.get_connection()
    if _initialized:
        return
    with _tenants_db.write_lock:
        if not _initialized:
            conn.executescript(_SCHEMA)
            conn.commit()
            _initialized = True
            logger.info("Acquisizione table ready (intake_records)")


def _conn():
    if not _initialized:
        init()
    return _tenants_db.get_connection()


def _row_to_dict(row) -> dict[str, Any]:
    record = dict(row)
    for field in _JSON_FIELDS:
        raw = record.get(field)
        record[field] = json.loads(raw) if raw else None
    return record


def create(tenant_id: str, market: str, consent_method: str) -> dict[str, Any]:
    """Insert a new intake record, consent already given. Returns the row."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    record = {
        "id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "market": market,
        "status": "transcribing",
        "consent_given_at": now,
        "consent_method": consent_method,
        "transcript": "",
        "listing_fields": None,
        "missing_required": None,
        "listing_text": None,
        "tasks": None,
        "created_at": now,
        "updated_at": now,
        "confirmed_at": None,
    }
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            "INSERT INTO intake_records "
            "(id, tenant_id, market, status, consent_given_at, consent_method, "
            " transcript, listing_fields, missing_required, listing_text, tasks, "
            " created_at, updated_at, confirmed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["id"], record["tenant_id"], record["market"], record["status"],
                record["consent_given_at"], record["consent_method"], record["transcript"],
                record["listing_fields"], record["missing_required"], record["listing_text"],
                record["tasks"], record["created_at"], record["updated_at"], record["confirmed_at"],
            ),
        )
        conn.commit()
    logger.info("Intake record created: %s (tenant=%s market=%s)", record["id"], tenant_id, market)
    return record


def get(record_id: str, tenant_id: str) -> Optional[dict[str, Any]]:
    """Fetch one record, strictly scoped to tenant_id. None if missing or owned
    by a different tenant."""
    row = _conn().execute(
        "SELECT * FROM intake_records WHERE id = ? AND tenant_id = ?",
        (record_id, tenant_id),
    ).fetchone()
    return _row_to_dict(row) if row else None


def list_for_tenant(tenant_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """Most-recent intake records for ONE tenant. Strictly scoped by tenant_id."""
    limit = max(1, min(limit, 500))
    rows = _conn().execute(
        "SELECT * FROM intake_records WHERE tenant_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (tenant_id, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_transcript(record_id: str, tenant_id: str, transcript: str) -> bool:
    """Autosave the accumulated transcript. Only while still in 'transcribing'
    status. Returns False if the record doesn't exist / isn't owned by the
    tenant / has already moved past the live-meeting stage."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "UPDATE intake_records SET transcript = ?, updated_at = ? "
            "WHERE id = ? AND tenant_id = ? AND status = 'transcribing'",
            (transcript, now, record_id, tenant_id),
        )
        conn.commit()
        return cur.rowcount > 0


def set_processing(record_id: str, tenant_id: str) -> bool:
    """Mark a record as 'processing' just before the extraction call, so a
    concurrent finish request can't race. Only transitions from 'transcribing'."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "UPDATE intake_records SET status = 'processing', updated_at = ? "
            "WHERE id = ? AND tenant_id = ? AND status = 'transcribing'",
            (now, record_id, tenant_id),
        )
        conn.commit()
        return cur.rowcount > 0


def set_review_result(
    record_id: str,
    tenant_id: str,
    listing_fields: dict,
    missing_required: list,
    listing_text: str,
    tasks: list,
) -> bool:
    """Write the extraction output and move the record to 'review'. Called once,
    right after a successful extraction call."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "UPDATE intake_records SET status = 'review', listing_fields = ?, "
            "missing_required = ?, listing_text = ?, tasks = ?, updated_at = ? "
            "WHERE id = ? AND tenant_id = ?",
            (
                json.dumps(listing_fields, ensure_ascii=False),
                json.dumps(missing_required, ensure_ascii=False),
                listing_text,
                json.dumps(tasks, ensure_ascii=False),
                now, record_id, tenant_id,
            ),
        )
        conn.commit()
        return cur.rowcount > 0


def revert_to_transcribing(record_id: str, tenant_id: str) -> None:
    """Roll a 'processing' record back to 'transcribing' after a failed
    extraction, so the agent can retry without losing the transcript."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            "UPDATE intake_records SET status = 'transcribing', updated_at = ? "
            "WHERE id = ? AND tenant_id = ? AND status = 'processing'",
            (now, record_id, tenant_id),
        )
        conn.commit()


def confirm(
    record_id: str,
    tenant_id: str,
    listing_fields: dict,
    listing_text: str,
    tasks: list,
) -> bool:
    """Save the agent's edited fields and move the record to 'confirmed'. Only
    transitions from 'review' — nothing is final until this call."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "UPDATE intake_records SET status = 'confirmed', listing_fields = ?, "
            "listing_text = ?, tasks = ?, updated_at = ?, confirmed_at = ? "
            "WHERE id = ? AND tenant_id = ? AND status = 'review'",
            (
                json.dumps(listing_fields, ensure_ascii=False),
                listing_text,
                json.dumps(tasks, ensure_ascii=False),
                now, now, record_id, tenant_id,
            ),
        )
        conn.commit()
        return cur.rowcount > 0

"""SQLite persistence for the sales rep's meeting notes.

One table — sales_notes — on the SAME connection as the tenants registry (see
tenants/db.py), reusing its process-wide connection and write lock, exactly
like leadgen/db.py and acquisizione/db.py.

Deliberately NOT tenant-scoped: these notes belong to the ApollonIA sales team
and live behind the internal lead-gen dashboard, alongside campaigns and
leads, which are equally global. Nothing here is ever served to an agency.

No audio is ever stored — only the transcript text of the rep's spoken
debrief and the structured note derived from it.
"""

import datetime
import json
import logging
import uuid
from typing import Any, Optional

from salesnotes import schema
from tenants import db as _tenants_db

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sales_notes (
  id          TEXT PRIMARY KEY,
  status      TEXT NOT NULL DEFAULT 'recording',  -- recording | processing | review | saved | abandoned
  language    TEXT NOT NULL DEFAULT 'it',
  transcript  TEXT NOT NULL DEFAULT '',
  title       TEXT,
  customer    TEXT,
  outcome     TEXT,                               -- positive | neutral | negative | NULL
  summary     TEXT,
  details     TEXT,                               -- JSON: went_well, went_wrong, objections, next_steps
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  saved_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_sales_notes_created ON sales_notes(created_at);
"""

_initialized = False


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def init() -> None:
    """Create the sales_notes table on the shared connection (idempotent)."""
    global _initialized
    conn = _tenants_db.get_connection()
    if _initialized:
        return
    with _tenants_db.write_lock:
        if not _initialized:
            conn.executescript(_SCHEMA)
            conn.commit()
            _initialized = True
            logger.info("Sales-notes table ready (sales_notes)")


def _conn():
    if not _initialized:
        init()
    return _tenants_db.get_connection()


def _row_to_dict(row) -> dict[str, Any]:
    """One flat note object: the list fields are unpacked out of `details` and
    always present, so the dashboard never has to guard against a missing key
    on a note written before a field existed."""
    note = dict(row)
    raw = note.pop("details", None)
    stored = {}
    if raw:
        try:
            stored = json.loads(raw)
        except ValueError:
            logger.warning("Note %s has unreadable details JSON", note.get("id"))
    for field in schema.LIST_FIELDS:
        value = stored.get(field)
        note[field] = value if isinstance(value, list) else []
    return note


def create(language: str) -> dict[str, Any]:
    """Open a new note, ready to record into. Returns the row."""
    now = _now()
    note_id = str(uuid.uuid4())
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            "INSERT INTO sales_notes (id, status, language, transcript, created_at, updated_at) "
            "VALUES (?, 'recording', ?, '', ?, ?)",
            (note_id, language, now, now),
        )
        conn.commit()
    logger.info("Sales note created: %s (language=%s)", note_id, language)
    return get(note_id)


def get(note_id: str) -> Optional[dict[str, Any]]:
    row = _conn().execute("SELECT * FROM sales_notes WHERE id = ?", (note_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_notes(limit: int = 100) -> list[dict[str, Any]]:
    """Notes worth showing, newest first: the saved ones plus anything still
    mid-flight. Abandoned notes are kept on disk but never listed."""
    limit = max(1, min(limit, 500))
    rows = _conn().execute(
        "SELECT * FROM sales_notes WHERE status != 'abandoned' "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_resumable(max_age_hours: int = 6) -> Optional[dict[str, Any]]:
    """The most recent note left mid-recording with words already captured —
    a closed browser, a phone that killed the tab.

    Deliberate exits are marked 'abandoned' (see abandon) and never come back,
    so what's left here is only work nobody meant to lose. Ordered by
    updated_at: the last autosave is when the recording actually stopped.
    """
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(hours=max_age_hours)
    ).isoformat()
    row = _conn().execute(
        "SELECT * FROM sales_notes WHERE status = 'recording' AND transcript != '' "
        "AND updated_at >= ? ORDER BY updated_at DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    return _row_to_dict(row) if row else None


def update_transcript(note_id: str, transcript: str) -> bool:
    """Autosave the accumulated transcript. Only while still recording."""
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "UPDATE sales_notes SET transcript = ?, updated_at = ? "
            "WHERE id = ? AND status = 'recording'",
            (transcript, _now(), note_id),
        )
        conn.commit()
        return cur.rowcount > 0


def abandon(note_id: str) -> bool:
    """Mark a recording as deliberately given up on — the rep pressed the
    go-back button, or dismissed the offer to resume it.

    Nothing is deleted: the transcript stays on the row. Only the status
    changes, so the note stops being offered back by get_resumable."""
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "UPDATE sales_notes SET status = 'abandoned', updated_at = ? "
            "WHERE id = ? AND status = 'recording'",
            (_now(), note_id),
        )
        conn.commit()
        return cur.rowcount > 0


def set_processing(note_id: str) -> bool:
    """Mark a note as 'processing' just before the extraction call, so two
    finish requests can't race. Only transitions from 'recording'."""
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "UPDATE sales_notes SET status = 'processing', updated_at = ? "
            "WHERE id = ? AND status = 'recording'",
            (_now(), note_id),
        )
        conn.commit()
        return cur.rowcount > 0


def revert_to_recording(note_id: str) -> None:
    """Roll a 'processing' note back after a failed extraction, so the rep can
    retry without losing the transcript."""
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            "UPDATE sales_notes SET status = 'recording', updated_at = ? "
            "WHERE id = ? AND status = 'processing'",
            (_now(), note_id),
        )
        conn.commit()


def _write_fields(note_id: str, fields: dict[str, Any], status: str, allowed: tuple[str, ...]) -> bool:
    """Shared writer for the extraction result and the rep's edits. `fields` is
    already normalized (see salesnotes/schema.py)."""
    details = {name: fields[name] for name in schema.LIST_FIELDS}
    now = _now()
    placeholders = ",".join("?" for _ in allowed)
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "UPDATE sales_notes SET status = ?, title = ?, customer = ?, outcome = ?, "
            "summary = ?, details = ?, updated_at = ?, "
            "saved_at = CASE WHEN ? = 'saved' THEN COALESCE(saved_at, ?) ELSE saved_at END "
            f"WHERE id = ? AND status IN ({placeholders})",
            (
                status,
                fields["title"], fields["customer"], fields["outcome"], fields["summary"],
                json.dumps(details, ensure_ascii=False),
                now, status, now, note_id, *allowed,
            ),
        )
        conn.commit()
        return cur.rowcount > 0


def set_review_result(note_id: str, fields: dict[str, Any]) -> bool:
    """Write the extraction output and move the note to 'review'."""
    return _write_fields(note_id, fields, "review", ("processing",))


def save(note_id: str, fields: dict[str, Any]) -> bool:
    """Store the rep's edited note as final. Allowed from 'review' (the usual
    path) and from 'saved' — a note stays editable after the fact, since the
    detail that matters often surfaces on the drive back."""
    return _write_fields(note_id, fields, "saved", ("review", "saved"))


def delete(note_id: str) -> bool:
    """Delete a note outright, transcript included. For the mis-recording that
    should never have existed — everything else is kept."""
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute("DELETE FROM sales_notes WHERE id = ?", (note_id,))
        conn.commit()
    if cur.rowcount > 0:
        logger.info("Sales note deleted: %s", note_id)
    return cur.rowcount > 0

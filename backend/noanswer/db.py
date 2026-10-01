"""SQLite persistence for an agency's personal-numbers list.

A number on this list belongs to someone the agency's owner knows personally —
family, friends — whose calls reach Apollonia only because the owner's phone
forwards every unanswered call. Apollonia doesn't treat those callers as leads:
she says the owner can't answer right now and offers to take a message (see
call/router._handle_realtime_incoming).

One table — no_answer_numbers — on the SAME connection as the tenants registry
(see tenants/db.py), reusing its process-wide connection and write lock, like
agents/db.py. Every row carries tenant_id and every read filters on it.

Numbers are matched the way the call path already compares numbers
(call/router._same_number): on the last 9 digits. A contact saved as
"333 123 4567" and a caller arriving as "+393331234567" are the same line, and
a contacts file is full of the former — the owner never typed a country code.
`match_key` is that comparison stored, so a lookup is one indexed query and
the same person can't be listed twice under two spellings.
"""

import datetime
import logging
import uuid
from typing import Any, Optional

from tenants import db as _tenants_db

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS no_answer_numbers (
  id         TEXT PRIMARY KEY,
  tenant_id  TEXT NOT NULL,
  number     TEXT NOT NULL,
  match_key  TEXT NOT NULL,
  name       TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_no_answer_numbers_key
  ON no_answer_numbers(tenant_id, match_key);
"""

# Shortest and longest number accepted, in digits. Below 6 it is a short code
# or a typo, never a person; above 15 it is not a phone number (E.164 caps at 15).
_MIN_DIGITS = 6
_MAX_DIGITS = 15

# Digits compared — the same window as call/router._same_number.
_KEY_DIGITS = 9

_initialized = False


def init() -> None:
    """Create the no_answer_numbers table on the shared connection (idempotent)."""
    global _initialized
    conn = _tenants_db.get_connection()
    if _initialized:
        return
    with _tenants_db.write_lock:
        if not _initialized:
            conn.executescript(_SCHEMA)
            conn.commit()
            _initialized = True
            logger.info("No-answer table ready (no_answer_numbers)")


def _conn():
    if not _initialized:
        init()
    return _tenants_db.get_connection()


def normalize(raw: str) -> Optional[str]:
    """A number as typed or exported → the form it is stored and shown in, or
    None when it isn't a phone number.

    Separators are dropped and the international access prefix 00 becomes +.
    A national-format number ("0331 234567", "333 1234567") is kept as it is:
    its country can't be known, and matching doesn't need it (see match_key).
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    plus = raw.lstrip().startswith("+")
    if not plus and digits.startswith("00"):
        digits, plus = digits[2:], True
    if not (_MIN_DIGITS <= len(digits) <= _MAX_DIGITS):
        return None
    return ("+" if plus else "") + digits


def match_key(number: str) -> str:
    """The part of a number two spellings of the same line have in common."""
    digits = "".join(ch for ch in (number or "") if ch.isdigit())
    return digits[-_KEY_DIGITS:] if len(digits) >= _KEY_DIGITS else digits


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def list_for_tenant(tenant_id: str) -> list[dict[str, Any]]:
    """Every listed number of ONE agency, by name and then number, so the list
    reads like the owner's phone book."""
    rows = _conn().execute(
        "SELECT id, number, name, created_at FROM no_answer_numbers "
        "WHERE tenant_id = ? ORDER BY name = '', lower(name), number",
        (tenant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def listed_keys(tenant_id: str) -> set[str]:
    """The match keys already listed — lets a contacts-file preview mark the
    numbers that are on the list already."""
    rows = _conn().execute(
        "SELECT match_key FROM no_answer_numbers WHERE tenant_id = ?",
        (tenant_id,),
    ).fetchall()
    return {r["match_key"] for r in rows}


def add(tenant_id: str, number: str, name: str = "") -> Optional[dict[str, Any]]:
    """List one number. `number` must already be normalize()d.

    Returns the new row, or None when the same line is already listed (under
    any spelling) — the existing entry and its name are left as they were.
    """
    row = {
        "id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "number": number,
        "match_key": match_key(number),
        "name": (name or "").strip(),
        "created_at": _now(),
    }
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "INSERT OR IGNORE INTO no_answer_numbers "
            "(id, tenant_id, number, match_key, name, created_at) "
            "VALUES (:id, :tenant_id, :number, :match_key, :name, :created_at)",
            row,
        )
        conn.commit()
    if cur.rowcount == 0:
        return None
    logger.info("Tenant %s: %s added to the no-answer list", tenant_id, number)
    return {k: row[k] for k in ("id", "number", "name", "created_at")}


def delete(entry_id: str, tenant_id: str) -> bool:
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "DELETE FROM no_answer_numbers WHERE id = ? AND tenant_id = ?",
            (entry_id, tenant_id),
        )
        conn.commit()
    return cur.rowcount > 0


def find(tenant_id: str, caller: str) -> Optional[dict[str, Any]]:
    """The listed entry a caller's number matches, or None."""
    key = match_key(caller)
    if len(key) < _MIN_DIGITS:
        return None
    row = _conn().execute(
        "SELECT id, number, name FROM no_answer_numbers "
        "WHERE tenant_id = ? AND match_key = ?",
        (tenant_id, key),
    ).fetchone()
    return dict(row) if row else None

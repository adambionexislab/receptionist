"""SQLite-backed tenant registry.

One module-level connection shared across threads (sqlite3 is compiled in
serialized mode), with an explicit lock around writes so concurrent signups
and background scrapes can't interleave inserts/updates.
"""

import datetime
import logging
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

from config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
  id              TEXT PRIMARY KEY,
  created_at      TEXT,
  agency_name     TEXT NOT NULL,
  agent_name      TEXT NOT NULL DEFAULT 'Apollonia',
  twilio_number   TEXT,
  real_number     TEXT,
  immobiliare_url TEXT,
  lead_email      TEXT NOT NULL,
  plan            TEXT,
  billing_period  TEXT,
  management_mode TEXT DEFAULT 'perse_cancellate',
  active          INTEGER DEFAULT 1
)
"""

_COLUMNS = {
    "id",
    "created_at",
    "agency_name",
    "agent_name",
    "twilio_number",
    "real_number",
    "immobiliare_url",
    "lead_email",
    "plan",
    "billing_period",
    "management_mode",
    "active",
}


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                data_dir = Path(settings.DATA_DIR)
                data_dir.mkdir(parents=True, exist_ok=True)
                db_path = data_dir / "receptionist.db"
                conn = sqlite3.connect(str(db_path), check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute(_SCHEMA)
                conn.commit()
                _conn = conn
                logger.info("Tenants DB opened at %s", db_path)
    return _conn


def get_by_twilio_number(number: str) -> Optional[dict]:
    row = _get_conn().execute(
        "SELECT * FROM tenants WHERE twilio_number = ? AND active = 1",
        (number,),
    ).fetchone()
    return dict(row) if row else None


def get_by_id(tenant_id: str) -> Optional[dict]:
    row = _get_conn().execute(
        "SELECT * FROM tenants WHERE id = ?", (tenant_id,)
    ).fetchone()
    return dict(row) if row else None


def get_all_active() -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM tenants WHERE active = 1 ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]


def count() -> int:
    return _get_conn().execute("SELECT COUNT(*) FROM tenants").fetchone()[0]


def create(**fields: Any) -> dict:
    """Insert a tenant. Generates id + created_at; unknown fields are rejected."""
    unknown = set(fields) - _COLUMNS
    if unknown:
        raise ValueError(f"Unknown tenant fields: {unknown}")

    tenant: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "agent_name": "Apollonia",
        "management_mode": "perse_cancellate",
        "active": 1,
        **fields,
    }
    cols = ", ".join(tenant)
    placeholders = ", ".join("?" for _ in tenant)
    conn = _get_conn()
    with _lock:
        conn.execute(
            f"INSERT INTO tenants ({cols}) VALUES ({placeholders})",
            tuple(tenant.values()),
        )
        conn.commit()
    logger.info("Tenant created: %s (%s)", tenant["agency_name"], tenant["id"])
    return tenant


def update_twilio_number(tenant_id: str, number: str) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE tenants SET twilio_number = ? WHERE id = ?",
            (number, tenant_id),
        )
        conn.commit()
    logger.info("Tenant %s assigned Twilio number %s", tenant_id, number)

"""SQLite data layer for the ApollonIA lead-generation pipeline.

Three tables — campaigns, leads, agent_logs — live on the SAME connection as
the tenants registry (see tenants/db.py). We reuse that single process-wide
connection and its write lock rather than opening a second handle to the file,
which is the safest way to avoid "database is locked" on SQLite.

The schema is created lazily on first use (and again, harmlessly, at startup),
so importing this module has no side effects.
"""

import datetime
import logging
from typing import Any, Optional

from tenants import db as _tenants_db

logger = logging.getLogger(__name__)

# NOTE on `max_results`: the prompt's create-campaign body carries {city,
# max_results} but its table sketch omits the column. We persist it here so
# POST /campaigns/{id}/start (and pause→resume) can run without re-supplying it.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  city TEXT NOT NULL,
  status TEXT DEFAULT 'pending',          -- pending | running | paused | completed
  max_results INTEGER DEFAULT 60,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  started_at TIMESTAMP,
  completed_at TIMESTAMP,
  total_found INTEGER DEFAULT 0,
  total_emailed INTEGER DEFAULT 0,
  total_responded INTEGER DEFAULT 0,
  total_booked INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id INTEGER REFERENCES campaigns(id),
  agency_name TEXT,
  address TEXT,
  phone TEXT,
  website TEXT,
  email TEXT,
  google_place_id TEXT UNIQUE,
  email_status TEXT DEFAULT 'pending',     -- pending | no_email | sent | bounced
  response_status TEXT DEFAULT 'none',     -- none | interested | not_interested | booked
  email_sent_at TIMESTAMP,
  responded_at TIMESTAMP,
  notes TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id INTEGER REFERENCES campaigns(id),
  lead_id INTEGER REFERENCES leads(id),
  event TEXT NOT NULL,                      -- place_found | email_found | email_sent | response_received | ...
  detail TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_leads_campaign ON leads(campaign_id);
CREATE INDEX IF NOT EXISTS idx_logs_campaign ON agent_logs(campaign_id);
"""

_initialized = False


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def init() -> None:
    """Create the lead-gen tables on the shared connection (idempotent)."""
    global _initialized
    conn = _tenants_db.get_connection()
    if _initialized:
        return
    with _tenants_db.write_lock:
        if not _initialized:
            conn.executescript(_SCHEMA)
            conn.commit()
            _initialized = True
            logger.info("Lead-gen tables ready (campaigns, leads, agent_logs)")


def _conn():
    if not _initialized:
        init()
    return _tenants_db.get_connection()


# ── campaigns ────────────────────────────────────────────────────────────────
def create_campaign(city: str, max_results: int = 60) -> dict:
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "INSERT INTO campaigns (city, max_results) VALUES (?, ?)",
            (city, max_results),
        )
        conn.commit()
        campaign_id = cur.lastrowid
    return get_campaign(campaign_id)


def get_campaign(campaign_id: int) -> Optional[dict]:
    row = _conn().execute(
        "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
    ).fetchone()
    return dict(row) if row else None


def list_campaigns() -> list[dict]:
    rows = _conn().execute(
        "SELECT * FROM campaigns ORDER BY created_at DESC, id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def set_status(campaign_id: int, status: str) -> None:
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            "UPDATE campaigns SET status = ? WHERE id = ?", (status, campaign_id)
        )
        conn.commit()


def mark_started(campaign_id: int) -> None:
    """Set status=running and stamp started_at the first time only."""
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            "UPDATE campaigns SET status = 'running', "
            "started_at = COALESCE(started_at, ?) WHERE id = ?",
            (_now(), campaign_id),
        )
        conn.commit()


def mark_completed(campaign_id: int) -> None:
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            "UPDATE campaigns SET status = 'completed', completed_at = ? WHERE id = ?",
            (_now(), campaign_id),
        )
        conn.commit()


def increment_campaign(campaign_id: int, field: str, by: int = 1) -> None:
    """Bump one of the total_* counters. `field` is validated against an allowlist."""
    allowed = {"total_found", "total_emailed", "total_responded", "total_booked"}
    if field not in allowed:
        raise ValueError(f"Unknown counter field: {field}")
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            f"UPDATE campaigns SET {field} = {field} + ? WHERE id = ?",
            (by, campaign_id),
        )
        conn.commit()


# ── leads ────────────────────────────────────────────────────────────────────
def lead_exists(google_place_id: str) -> bool:
    row = _conn().execute(
        "SELECT 1 FROM leads WHERE google_place_id = ?", (google_place_id,)
    ).fetchone()
    return row is not None


def add_lead(
    campaign_id: int,
    agency_name: Optional[str] = None,
    address: Optional[str] = None,
    phone: Optional[str] = None,
    website: Optional[str] = None,
    email: Optional[str] = None,
    google_place_id: Optional[str] = None,
    email_status: str = "pending",
) -> Optional[int]:
    """Insert a lead. Returns the new lead id, or None if the place_id already
    exists (UNIQUE constraint — INSERT OR IGNORE makes this race-safe)."""
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "INSERT OR IGNORE INTO leads "
            "(campaign_id, agency_name, address, phone, website, email, "
            " google_place_id, email_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (campaign_id, agency_name, address, phone, website, email,
             google_place_id, email_status),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        return cur.lastrowid


def get_lead(lead_id: int) -> Optional[dict]:
    row = _conn().execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    return dict(row) if row else None


def get_pending_email_leads(campaign_id: int) -> list[dict]:
    rows = _conn().execute(
        "SELECT * FROM leads WHERE campaign_id = ? "
        "AND email IS NOT NULL AND email != '' AND email_status = 'pending' "
        "ORDER BY id",
        (campaign_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_leads(
    campaign_id: int,
    page: int = 1,
    limit: int = 50,
    status: Optional[str] = None,
) -> dict:
    """Paginated leads for a campaign. `status` filters on email_status when it
    matches a known email state, otherwise on response_status."""
    conn = _conn()
    where = ["campaign_id = ?"]
    params: list[Any] = [campaign_id]
    if status:
        if status in ("pending", "no_email", "sent", "bounced"):
            where.append("email_status = ?")
        else:
            where.append("response_status = ?")
        params.append(status)
    where_sql = " AND ".join(where)

    total = conn.execute(
        f"SELECT COUNT(*) FROM leads WHERE {where_sql}", params
    ).fetchone()[0]

    page = max(1, page)
    limit = max(1, min(limit, 200))
    offset = (page - 1) * limit
    rows = conn.execute(
        f"SELECT * FROM leads WHERE {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return {
        "leads": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "limit": limit,
    }


def mark_email_sent(lead_id: int) -> None:
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            "UPDATE leads SET email_status = 'sent', email_sent_at = ? WHERE id = ?",
            (_now(), lead_id),
        )
        conn.commit()


def set_email_status(lead_id: int, status: str) -> None:
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            "UPDATE leads SET email_status = ? WHERE id = ?", (status, lead_id)
        )
        conn.commit()


def set_lead_response(lead_id: int, response_status: str, notes: Optional[str] = None) -> None:
    conn = _conn()
    with _tenants_db.write_lock:
        if notes is not None:
            conn.execute(
                "UPDATE leads SET response_status = ?, responded_at = ?, notes = ? "
                "WHERE id = ?",
                (response_status, _now(), notes, lead_id),
            )
        else:
            conn.execute(
                "UPDATE leads SET response_status = ?, responded_at = ? WHERE id = ?",
                (response_status, _now(), lead_id),
            )
        conn.commit()


# ── agent_logs ───────────────────────────────────────────────────────────────
def log_event(
    campaign_id: Optional[int],
    event: str,
    detail: Optional[str] = None,
    lead_id: Optional[int] = None,
) -> None:
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            "INSERT INTO agent_logs (campaign_id, lead_id, event, detail) "
            "VALUES (?, ?, ?, ?)",
            (campaign_id, lead_id, event, detail),
        )
        conn.commit()


def get_logs(campaign_id: int, limit: int = 100) -> list[dict]:
    limit = max(1, min(limit, 500))
    rows = _conn().execute(
        "SELECT * FROM agent_logs WHERE campaign_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (campaign_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ── app_settings (simple key/value store) ────────────────────────────────────
def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    row = _conn().execute(
        "SELECT value FROM app_settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, _now()),
        )
        conn.commit()

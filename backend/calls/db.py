"""SQLite persistence for handled calls and the contacts they produce.

Two tables — call_sessions and contacts — live on the SAME connection as the
tenants registry (see tenants/db.py), reusing its process-wide connection and
write lock, exactly like the lead-gen layer in leadgen/db.py. Both tables carry
tenant_id on every row: it is the scoping column for the agency dashboard, and
every read here filters on it.

Until this module shipped, calls were emailed to the agency and discarded, so
there is no history before go-live — these tables start filling from the first
call after deploy (see call/router._persist_call).

  call_sessions — one row per accepted call. Backs the "minutes this month"
                  metric (duration_seconds) and a future call history.
  contacts      — one row per call that produced something to follow up on
                  (a name and/or a callback number). Backs the contacts list.
"""

import datetime
import logging
from typing import Any, Optional
from zoneinfo import ZoneInfo

from tenants import db as _tenants_db

logger = logging.getLogger(__name__)

# The "calendar month" for the minutes metric is a Rome month (same offset as
# Bratislava, so it's correct for both IT and SK tenants). started_at is stored
# as a UTC ISO string, so we compare against UTC bounds derived from the Rome
# month — the strings share the "+00:00" offset, keeping the comparison sound.
_ROME = ZoneInfo("Europe/Rome")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS call_sessions (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id        TEXT NOT NULL,
  call_id          TEXT,
  caller_number    TEXT,
  started_at       TIMESTAMP,
  ended_at         TIMESTAMP,
  duration_seconds INTEGER DEFAULT 0,
  locale           TEXT DEFAULT 'it',
  outcome          TEXT,                    -- lead | message | call
  summary          TEXT,
  created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contacts (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id       TEXT NOT NULL,
  call_session_id INTEGER REFERENCES call_sessions(id),
  name            TEXT,
  phone           TEXT,
  interest        TEXT,                     -- interested listing address(es)
  summary         TEXT,                     -- one-line call summary
  details         TEXT,                     -- JSON snapshot for a detail view
  created_at      TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_call_sessions_tenant ON call_sessions(tenant_id, started_at);
CREATE INDEX IF NOT EXISTS idx_contacts_tenant ON contacts(tenant_id, created_at);
"""

_initialized = False


def init() -> None:
    """Create the call/contact tables on the shared connection (idempotent)."""
    global _initialized
    conn = _tenants_db.get_connection()
    if _initialized:
        return
    with _tenants_db.write_lock:
        if not _initialized:
            conn.executescript(_SCHEMA)
            conn.commit()
            _initialized = True
            logger.info("Call tables ready (call_sessions, contacts)")


def _conn():
    if not _initialized:
        init()
    return _tenants_db.get_connection()


def add_call_session(
    tenant_id: str,
    call_id: Optional[str],
    caller_number: Optional[str],
    started_at: Optional[str],
    ended_at: Optional[str],
    duration_seconds: int,
    locale: str,
    outcome: str,
    summary: Optional[str],
) -> int:
    """Record one accepted call. Returns the new row id."""
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "INSERT INTO call_sessions "
            "(tenant_id, call_id, caller_number, started_at, ended_at, "
            " duration_seconds, locale, outcome, summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, call_id, caller_number, started_at, ended_at,
             duration_seconds, locale, outcome, summary),
        )
        conn.commit()
        return cur.lastrowid


def add_contact(
    tenant_id: str,
    call_session_id: Optional[int],
    name: Optional[str],
    phone: Optional[str],
    interest: Optional[str],
    summary: Optional[str],
    details: Optional[str],
    created_at: Optional[str],
) -> int:
    """Record one follow-up-worthy caller. Returns the new row id."""
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "INSERT INTO contacts "
            "(tenant_id, call_session_id, name, phone, interest, summary, "
            " details, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, call_session_id, name, phone, interest, summary,
             details, created_at),
        )
        conn.commit()
        return cur.lastrowid


def _month_bounds_utc(
    now: Optional[datetime.datetime] = None,
) -> tuple[int, int, str, str]:
    """(year, month, start_utc_iso, next_month_utc_iso) for the Rome calendar
    month containing `now` (defaults to the current instant). The two ISO bounds
    are UTC so they compare directly against the stored started_at strings."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    now_rome = now.astimezone(_ROME)
    start_rome = now_rome.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_rome.month == 12:
        next_rome = start_rome.replace(year=start_rome.year + 1, month=1)
    else:
        next_rome = start_rome.replace(month=start_rome.month + 1)
    start_utc = start_rome.astimezone(datetime.timezone.utc).isoformat()
    next_utc = next_rome.astimezone(datetime.timezone.utc).isoformat()
    return start_rome.year, start_rome.month, start_utc, next_utc


def monthly_call_stats(
    tenant_id: str, now: Optional[datetime.datetime] = None
) -> dict[str, int]:
    """Total call seconds and call count for ONE tenant in the current calendar
    month. Strictly scoped by tenant_id. Data only exists from go-live forward —
    there is no history before call persistence shipped."""
    year, month, start_utc, next_utc = _month_bounds_utc(now)
    conn = _conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(duration_seconds), 0) AS secs, COUNT(*) AS calls "
        "FROM call_sessions "
        "WHERE tenant_id = ? AND started_at >= ? AND started_at < ?",
        (tenant_id, start_utc, next_utc),
    ).fetchone()
    crow = conn.execute(
        "SELECT COUNT(*) AS c FROM contacts "
        "WHERE tenant_id = ? AND created_at >= ? AND created_at < ?",
        (tenant_id, start_utc, next_utc),
    ).fetchone()
    return {
        "year": year,
        "month": month,
        "seconds": int(row["secs"] or 0),
        "calls": int(row["calls"] or 0),
        "contacts": int(crow["c"] or 0),
    }


def list_contacts(tenant_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """Most-recent contacts for ONE tenant, joined to their call for outcome and
    duration. Strictly scoped by tenant_id — this is client data."""
    limit = max(1, min(limit, 500))
    rows = _conn().execute(
        "SELECT c.id, c.name, c.phone, c.interest, c.summary, c.created_at, "
        "       cs.outcome, cs.duration_seconds, cs.caller_number "
        "FROM contacts c "
        "LEFT JOIN call_sessions cs ON cs.id = c.call_session_id "
        "WHERE c.tenant_id = ? "
        "ORDER BY c.created_at DESC, c.id DESC "
        "LIMIT ?",
        (tenant_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]

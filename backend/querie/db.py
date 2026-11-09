"""SQLite storage for Querie sign-ups (waitlist and trial checkouts) and PDF report leads.

One row per email address: a waitlister who later starts a trial is the same
row moving from status 'waitlist' to 'checkout_started' to 'trial_started'.
Lives on the shared tenants connection, like leadgen/db.py, to avoid a second
handle to the same file.
"""

import datetime
import json
import logging
from typing import Optional

from tenants import db as _tenants_db

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS querie_signups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE,              -- lowercased
  name TEXT NOT NULL,
  phone TEXT NOT NULL,
  company TEXT,
  status TEXT NOT NULL,                    -- waitlist | checkout_started | trial_started
  founder INTEGER DEFAULT 0,               -- 1 = checkout opened with the founder coupon
  consent_at TEXT NOT NULL,
  consent_version TEXT NOT NULL,
  attribution TEXT,                        -- JSON: utm_*, fbclid, ttclid
  user_agent TEXT,
  stripe_session_id TEXT,
  stripe_customer_id TEXT,
  stripe_subscription_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_querie_signups_session ON querie_signups(stripe_session_id);

-- Emails left on /querie/pdf_report for the PDF report.
CREATE TABLE IF NOT EXISTS querie_report_leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE,              -- lowercased
  consent_at TEXT NOT NULL,
  consent_version TEXT NOT NULL,
  attribution TEXT,                        -- JSON: utm_*, fbclid, ttclid
  user_agent TEXT,
  downloads INTEGER NOT NULL DEFAULT 0,
  marketing INTEGER NOT NULL DEFAULT 0,    -- 1 = ticked "send me occasional news"
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Anonymous funnel for /querie/pdf_report: daily counts per step and browser
-- kind. No emails, no IDs, no cookies — only how far visitors get.
CREATE TABLE IF NOT EXISTS querie_report_funnel (
  day TEXT NOT NULL,                       -- YYYY-MM-DD, Europe/Bratislava
  event TEXT NOT NULL,
  browser TEXT NOT NULL,                   -- facebook | instagram | other | bot
  count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, event, browser)
);
"""

_initialized = False


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def init() -> None:
    global _initialized
    conn = _tenants_db.get_connection()
    if _initialized:
        return
    with _tenants_db.write_lock:
        if not _initialized:
            conn.executescript(_SCHEMA)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(querie_report_leads)")}
            if "marketing" not in columns:
                conn.execute("ALTER TABLE querie_report_leads ADD COLUMN marketing INTEGER NOT NULL DEFAULT 0")
                # report-1 had one required tick that covered the occasional news too.
                conn.execute("UPDATE querie_report_leads SET marketing = 1 WHERE consent_version LIKE 'report-1%'")
            conn.commit()
            _initialized = True
            logger.info("Querie sign-up table ready")


def _conn():
    if not _initialized:
        init()
    return _tenants_db.get_connection()


def upsert_signup(
    *,
    email: str,
    name: str,
    phone: str,
    company: Optional[str],
    status: str,
    consent_version: str,
    attribution: Optional[dict] = None,
    user_agent: Optional[str] = None,
) -> dict:
    """Insert or refresh the row for this email. A row that already reached
    'trial_started' keeps that status — signing up again must not demote it."""
    email = email.strip().lower()
    stamp = _now()
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            """
            INSERT INTO querie_signups
              (email, name, phone, company, status, consent_at, consent_version,
               attribution, user_agent, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
              name = excluded.name,
              phone = excluded.phone,
              company = COALESCE(excluded.company, querie_signups.company),
              status = CASE WHEN querie_signups.status = 'trial_started'
                            THEN querie_signups.status ELSE excluded.status END,
              consent_at = excluded.consent_at,
              consent_version = excluded.consent_version,
              attribution = COALESCE(querie_signups.attribution, excluded.attribution),
              user_agent = excluded.user_agent,
              updated_at = excluded.updated_at
            """,
            (
                email, name, phone, company or None, status, stamp, consent_version,
                json.dumps(attribution, ensure_ascii=False) if attribution else None,
                user_agent, stamp, stamp,
            ),
        )
        conn.commit()
    return get_by_email(email)


def get_by_email(email: str) -> Optional[dict]:
    row = _conn().execute(
        "SELECT * FROM querie_signups WHERE email = ?", (email.strip().lower(),)
    ).fetchone()
    return dict(row) if row else None


def set_checkout_session(email: str, session_id: str, founder: bool) -> None:
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            "UPDATE querie_signups SET stripe_session_id = ?, founder = ?, updated_at = ? WHERE email = ?",
            (session_id, 1 if founder else 0, _now(), email.strip().lower()),
        )
        conn.commit()


def mark_trial_started(
    session_id: str, email: Optional[str], customer_id: Optional[str], subscription_id: Optional[str]
) -> bool:
    """Find the row by checkout session (or by email as a fallback) and mark
    the trial as started. Returns False when no row matched."""
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            """
            UPDATE querie_signups
               SET status = 'trial_started', stripe_customer_id = ?,
                   stripe_subscription_id = ?, updated_at = ?
             WHERE stripe_session_id = ? OR (? IS NOT NULL AND email = ?)
            """,
            (customer_id, subscription_id, _now(), session_id,
             email, (email or "").strip().lower()),
        )
        conn.commit()
    return cur.rowcount > 0


def all_signups() -> list[dict]:
    rows = _conn().execute("SELECT * FROM querie_signups ORDER BY created_at, id").fetchall()
    return [dict(r) for r in rows]


# ── PDF report leads ─────────────────────────────────────────────────────────
def upsert_report_lead(
    *, email: str, consent_version: str, marketing: bool = False,
    attribution: Optional[dict] = None, user_agent: Optional[str] = None,
) -> dict:
    """One row per email; asking for the report again refreshes the consent.
    A marketing opt-in is kept when a later request leaves the box unticked."""
    email = email.strip().lower()
    stamp = _now()
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            """
            INSERT INTO querie_report_leads
              (email, consent_at, consent_version, marketing, attribution, user_agent, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
              consent_at = excluded.consent_at,
              consent_version = excluded.consent_version,
              marketing = MAX(querie_report_leads.marketing, excluded.marketing),
              attribution = COALESCE(querie_report_leads.attribution, excluded.attribution),
              user_agent = excluded.user_agent,
              updated_at = excluded.updated_at
            """,
            (
                email, stamp, consent_version, 1 if marketing else 0,
                json.dumps(attribution, ensure_ascii=False) if attribution else None,
                user_agent, stamp, stamp,
            ),
        )
        conn.commit()
    row = conn.execute("SELECT * FROM querie_report_leads WHERE email = ?", (email,)).fetchone()
    return dict(row)


def count_report_download(email: str) -> None:
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            "UPDATE querie_report_leads SET downloads = downloads + 1, updated_at = ? WHERE email = ?",
            (_now(), email.strip().lower()),
        )
        conn.commit()


def all_report_leads() -> list[dict]:
    rows = _conn().execute("SELECT * FROM querie_report_leads ORDER BY created_at, id").fetchall()
    return [dict(r) for r in rows]


def count_funnel(day: str, event: str, browser: str) -> None:
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            """
            INSERT INTO querie_report_funnel (day, event, browser, count) VALUES (?, ?, ?, 1)
            ON CONFLICT(day, event, browser) DO UPDATE SET count = count + 1
            """,
            (day, event, browser),
        )
        conn.commit()


def funnel_counts() -> list[dict]:
    rows = _conn().execute("SELECT * FROM querie_report_funnel ORDER BY day, browser, event").fetchall()
    return [dict(r) for r in rows]

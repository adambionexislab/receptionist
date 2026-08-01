"""SQLite persistence for an agency's human agents (its staff, not Apollonia).

One table — agency_agents — lives on the SAME connection as the tenants
registry (see tenants/db.py), reusing its process-wide connection and write
lock, exactly like calls/db.py and acquisizione/db.py. Every row carries
tenant_id, and every read here filters on it, so one agency can never see
another's agents.

Each agent gets a per-tenant `number` (1, 2, 3, …). A new agent takes the
LOWEST number not currently in use, so the agency's numbering stays dense:
with #1 and #3 taken, the next hire is #2. Deleting an agent therefore frees
their number for whoever is added next, and never renumbers anyone else.
"""

import datetime
import logging
import uuid
from typing import Any, Optional

from listings import db as listings_db
from tenants import db as _tenants_db

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agency_agents (
  id         TEXT PRIMARY KEY,
  tenant_id  TEXT NOT NULL,
  number     INTEGER NOT NULL,
  name       TEXT NOT NULL,
  email      TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agency_agents_number
  ON agency_agents(tenant_id, number);
"""

# Fields the dashboard may write. `number` is assigned here and is deliberately
# not editable — it identifies the agent.
_EDITABLE = ("name", "email")

_initialized = False


def init() -> None:
    """Create the agency_agents table on the shared connection (idempotent)."""
    global _initialized
    conn = _tenants_db.get_connection()
    if _initialized:
        return
    with _tenants_db.write_lock:
        if not _initialized:
            conn.executescript(_SCHEMA)
            conn.commit()
            _initialized = True
            logger.info("Agents table ready (agency_agents)")


def _conn():
    if not _initialized:
        init()
    return _tenants_db.get_connection()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def list_for_tenant(tenant_id: str) -> list[dict[str, Any]]:
    """Every agent of ONE agency, in assignment order. Scoped by tenant_id."""
    rows = _conn().execute(
        "SELECT * FROM agency_agents WHERE tenant_id = ? ORDER BY number",
        (tenant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get(agent_id: str, tenant_id: str) -> Optional[dict[str, Any]]:
    """One agent, strictly scoped to tenant_id. None if missing or owned by a
    different tenant."""
    row = _conn().execute(
        "SELECT * FROM agency_agents WHERE id = ? AND tenant_id = ?",
        (agent_id, tenant_id),
    ).fetchone()
    return dict(row) if row else None


def get_many(agent_ids: list[str], tenant_id: str) -> dict[str, dict[str, Any]]:
    """Resolve several agents at once, keyed by id — how the post-call lead
    email turns the agent_ids on a caller's listings into names and addresses.
    Ids belonging to another tenant (or already deleted) are simply absent from
    the result, so a stale id can never leak another agency's agent.
    """
    ids = [a for a in dict.fromkeys(agent_ids) if a]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = _conn().execute(
        f"SELECT * FROM agency_agents WHERE tenant_id = ? AND id IN ({placeholders})",
        (tenant_id, *ids),
    ).fetchall()
    return {row["id"]: dict(row) for row in rows}


def create(tenant_id: str, name: str, email: str) -> dict[str, Any]:
    """Add an agent and hand them the lowest number this tenant isn't using.

    The scan for that number and the INSERT share one write_lock block, so two
    concurrent adds can't be handed the same number.
    """
    now = _now()
    conn = _conn()
    with _tenants_db.write_lock:
        taken = {
            row[0] for row in conn.execute(
                "SELECT number FROM agency_agents WHERE tenant_id = ?", (tenant_id,)
            )
        }
        number = 1
        while number in taken:
            number += 1
        agent = {
            "id": str(uuid.uuid4()),
            "tenant_id": tenant_id,
            "number": number,
            "name": name,
            "email": email,
            "created_at": now,
            "updated_at": now,
        }
        conn.execute(
            "INSERT INTO agency_agents "
            "(id, tenant_id, number, name, email, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                agent["id"], agent["tenant_id"], agent["number"], agent["name"],
                agent["email"], agent["created_at"], agent["updated_at"],
            ),
        )
        conn.commit()
    logger.info("Agent #%d created for tenant %s", number, tenant_id)
    return agent


def update(agent_id: str, tenant_id: str, fields: dict) -> Optional[dict[str, Any]]:
    """Edit an agent's name/email. Unknown fields are ignored; `number` can't be
    changed. Returns the updated row, or None if it isn't this tenant's."""
    changes = {k: v for k, v in fields.items() if k in _EDITABLE}
    if not changes:
        return get(agent_id, tenant_id)

    assignments = ", ".join(f"{k} = ?" for k in changes)
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            f"UPDATE agency_agents SET {assignments}, updated_at = ? "
            "WHERE id = ? AND tenant_id = ?",
            (*changes.values(), _now(), agent_id, tenant_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
    return get(agent_id, tenant_id)


def delete(agent_id: str, tenant_id: str) -> bool:
    """Remove an agent. A hard delete — unlike listings there is no scrape that
    could resurrect the row, and the freed number is not reused.

    Any listings this agent handled are unassigned in the SAME transaction, so
    no listing is ever left pointing at a row that no longer exists: their leads
    fall back to the agency inbox until someone is assigned again.
    """
    # Must run before the lock block: init() takes write_lock itself, which is
    # not reentrant.
    listings_db.init()
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "DELETE FROM agency_agents WHERE id = ? AND tenant_id = ?",
            (agent_id, tenant_id),
        )
        freed = (
            listings_db.unassign_agent(tenant_id, agent_id, conn=conn)
            if cur.rowcount
            else 0
        )
        conn.commit()
    if cur.rowcount:
        logger.info(
            "Agent %s deleted (tenant %s) — %d listing(s) left unassigned",
            agent_id, tenant_id, freed,
        )
    return cur.rowcount > 0

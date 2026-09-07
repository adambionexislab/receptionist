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

# Columns added after the table first shipped; CREATE TABLE IF NOT EXISTS won't
# alter an existing table, so each is applied with an idempotent ALTER on
# startup (see _migrate) — same pattern as calls/db.py and listings/db.py.
#
# branch_id is nullable and stays that way: an agency with no branches, and an
# agent who belongs to none of them, are both normal (see branches/db.py).
_ADDED_COLUMNS = {
    "branch_id": "TEXT",
}

# Fields the dashboard may write. `number` is assigned here and is deliberately
# not editable — it identifies the agent.
_EDITABLE = ("name", "email", "branch_id")

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
            _migrate(conn)
            conn.commit()
            _initialized = True
            logger.info("Agents table ready (agency_agents)")


def _migrate(conn) -> None:
    """Add columns introduced after the table first shipped. Idempotent: each
    column is added only if a pre-existing table is missing it."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(agency_agents)")}
    for column, ddl in _ADDED_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE agency_agents ADD COLUMN {column} {ddl}")
            logger.info("Migrated agency_agents table: added column %s", column)


def _conn():
    if not _initialized:
        init()
    return _tenants_db.get_connection()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def list_for_tenant(
    tenant_id: str, branch_id: Optional[str] = None
) -> list[dict[str, Any]]:
    """Every agent of ONE agency, in assignment order. Scoped by tenant_id.

    With `branch_id`, only that branch's agents — the dashboard's branch view.
    Numbers stay the agency's (an agent is #4 of the agency, not #1 of their
    office), so a filtered list is a subset of the same numbering, never a
    renumbering.
    """
    sql = "SELECT * FROM agency_agents WHERE tenant_id = ?"
    params: list[Any] = [tenant_id]
    if branch_id:
        sql += " AND branch_id = ?"
        params.append(branch_id)
    rows = _conn().execute(sql + " ORDER BY number", tuple(params)).fetchall()
    return [dict(r) for r in rows]


def ids_for_branch(tenant_id: str, branch_id: str) -> list[str]:
    """The ids of one branch's agents — what everything else scoped to a branch
    is resolved through (its listings, and the calls whose leads went to it)."""
    rows = _conn().execute(
        "SELECT id FROM agency_agents WHERE tenant_id = ? AND branch_id = ?",
        (tenant_id, branch_id),
    ).fetchall()
    return [row["id"] for row in rows]


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


def create(
    tenant_id: str, name: str, email: str, branch_id: Optional[str] = None
) -> dict[str, Any]:
    """Add an agent and hand them the lowest number this tenant isn't using.

    The scan for that number and the INSERT share one write_lock block, so two
    concurrent adds can't be handed the same number.

    `branch_id` is the office they work out of (None = none, which is what
    every agent of an agency that has not created any branch looks like). The
    caller resolves it against this tenant first — see the dashboard router.
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
            "branch_id": branch_id or None,
            "created_at": now,
            "updated_at": now,
        }
        conn.execute(
            "INSERT INTO agency_agents "
            "(id, tenant_id, number, name, email, branch_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                agent["id"], agent["tenant_id"], agent["number"], agent["name"],
                agent["email"], agent["branch_id"], agent["created_at"],
                agent["updated_at"],
            ),
        )
        conn.commit()
    logger.info("Agent #%d created for tenant %s", number, tenant_id)
    return agent


def update(agent_id: str, tenant_id: str, fields: dict) -> Optional[dict[str, Any]]:
    """Edit an agent's name/email/branch. Unknown fields are ignored; `number`
    can't be changed. Returns the updated row, or None if it isn't this
    tenant's.

    branch_id is the one editable field that may legitimately be set to None
    (moving an agent out of every office), so it is passed through as-is rather
    than being treated as "not sent" — the router decides which of the two a
    PATCH meant.
    """
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


def unassign_branch(tenant_id: str, branch_id: str, conn=None) -> int:
    """Clear branch_id on every agent of `tenant_id` attached to `branch_id`,
    and return how many were detached. Called when a branch is closed, so its
    people don't point at an office that no longer exists.

    Pass `conn` when the caller already holds tenants.db.write_lock (it is a
    plain, non-reentrant Lock): the UPDATE then runs inside the caller's
    transaction and committing is the caller's job. Without it, this takes the
    lock and commits on its own. Same contract as listings.db.unassign_agent.
    """
    sql = (
        "UPDATE agency_agents SET branch_id = NULL, updated_at = ? "
        "WHERE tenant_id = ? AND branch_id = ?"
    )
    params = (_now(), tenant_id, branch_id)
    if conn is not None:
        return conn.execute(sql, params).rowcount
    own = _conn()
    with _tenants_db.write_lock:
        cur = own.execute(sql, params)
        own.commit()
    return cur.rowcount


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

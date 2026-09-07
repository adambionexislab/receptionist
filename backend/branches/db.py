"""SQLite persistence for an agency's branches (its offices, "sedi"/"pobočky").

One table — branches — lives on the SAME connection as the tenants registry
(see tenants/db.py), reusing its process-wide connection and write lock,
exactly like agents/db.py and calls/db.py. Every row carries tenant_id, and
every read here filters on it, so one agency can never see another's offices.

A branch is a reporting scope, not a second tenant: the subscription, its
allowances and the phone number stay with the agency. What a branch owns is
its people — agency_agents.branch_id — and everything the dashboard scopes to
a branch is resolved through them (a call is a branch's call because the lead
went to one of its agents; a listing is a branch's listing because one of its
agents handles it).

Nothing is required to belong to a branch. An agency that never creates one
sees exactly what it saw before, and an agent with branch_id NULL shows up
only in the whole-agency view.
"""

import datetime
import logging
import uuid
from typing import Any, Optional

from agents import db as agents_db
from tenants import db as _tenants_db

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS branches (
  id         TEXT PRIMARY KEY,
  tenant_id  TEXT NOT NULL,
  name       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_branches_tenant ON branches(tenant_id, created_at);
"""

# Fields the dashboard may write. A branch is just a name today; keeping the
# tuple means adding an address or a phone number later is a one-line change.
_EDITABLE = ("name",)

_initialized = False


def init() -> None:
    """Create the branches table on the shared connection (idempotent)."""
    global _initialized
    conn = _tenants_db.get_connection()
    if _initialized:
        return
    with _tenants_db.write_lock:
        if not _initialized:
            conn.executescript(_SCHEMA)
            conn.commit()
            _initialized = True
            logger.info("Branches table ready (branches)")


def _conn():
    if not _initialized:
        init()
    return _tenants_db.get_connection()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def list_for_tenant(tenant_id: str) -> list[dict[str, Any]]:
    """Every branch of ONE agency, oldest first — the order they were opened,
    which is the order the switcher lists them in. Scoped by tenant_id."""
    rows = _conn().execute(
        "SELECT * FROM branches WHERE tenant_id = ? ORDER BY created_at, name",
        (tenant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get(branch_id: str, tenant_id: str) -> Optional[dict[str, Any]]:
    """One branch, strictly scoped to tenant_id. None if missing or owned by a
    different tenant — this is what stops a branch id from another agency being
    used as a filter."""
    if not branch_id:
        return None
    row = _conn().execute(
        "SELECT * FROM branches WHERE id = ? AND tenant_id = ?",
        (branch_id, tenant_id),
    ).fetchone()
    return dict(row) if row else None


def create(tenant_id: str, name: str) -> dict[str, Any]:
    """Open a branch."""
    now = _now()
    branch = {
        "id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "name": name,
        "created_at": now,
        "updated_at": now,
    }
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            "INSERT INTO branches (id, tenant_id, name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                branch["id"], branch["tenant_id"], branch["name"],
                branch["created_at"], branch["updated_at"],
            ),
        )
        conn.commit()
    logger.info("Branch %r created for tenant %s", name, tenant_id)
    return branch


def update(branch_id: str, tenant_id: str, fields: dict) -> Optional[dict[str, Any]]:
    """Rename a branch. Unknown fields are ignored. Returns the updated row, or
    None if it isn't this tenant's."""
    changes = {k: v for k, v in fields.items() if k in _EDITABLE}
    if not changes:
        return get(branch_id, tenant_id)

    assignments = ", ".join(f"{k} = ?" for k in changes)
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            f"UPDATE branches SET {assignments}, updated_at = ? "
            "WHERE id = ? AND tenant_id = ?",
            (*changes.values(), _now(), branch_id, tenant_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
    return get(branch_id, tenant_id)


def delete(branch_id: str, tenant_id: str) -> bool:
    """Close a branch. Its agents are detached in the SAME transaction, so no
    agent is ever left pointing at an office that no longer exists — they keep
    their listings and their leads, and simply stop being counted under any
    branch until they are moved to another one.

    Past calls and tool uses keep the branch_id they were recorded with: the
    row is a record of where the work happened, and a closed office does not
    change last month's numbers. They stop being reachable through the
    switcher, and are still counted in the whole-agency view.
    """
    # Must run before the lock block: init() takes write_lock itself, which is
    # not reentrant.
    agents_db.init()
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "DELETE FROM branches WHERE id = ? AND tenant_id = ?",
            (branch_id, tenant_id),
        )
        detached = (
            agents_db.unassign_branch(tenant_id, branch_id, conn=conn)
            if cur.rowcount
            else 0
        )
        conn.commit()
    if cur.rowcount:
        logger.info(
            "Branch %s deleted (tenant %s) — %d agent(s) detached",
            branch_id, tenant_id, detached,
        )
    return cur.rowcount > 0

"""SQLite ledger of AI-tool usage, and the plan credits it consumes.

One table — tool_usage — lives on the SAME connection as the tenants registry
(see tenants/db.py), reusing its process-wide connection and write lock,
exactly like calls/db.py. Every row carries tenant_id, and every read filters
on it.

Each use of an AI tool costs a fixed slice of the tenant's monthly credit
allowance: €0.50 for one enhanced photo, €1.00 for one ApollonIA Meeting.
The allowance comes from the subscription (Base €15 / Pro €30 / Max €60).

Nothing here is a stored counter that gets decremented. The allowance resets
by construction, because what has been consumed is always summed over the
current billing period (billing/period.py) — so the first of the month is a
full balance again with no reset job to run, and no way for a missed run to
strand a tenant at zero. Past months stay readable for invoicing.

Running out does not disable anything: once a period's uses exceed the
allowance the excess is recorded as overage, which the dashboard shows and the
agency is invoiced for at the end of the month.

Money is integer euro cents throughout. Credits get compared, summed and
invoiced, and none of that survives float rounding.
"""

import datetime
import logging
from typing import Any, Optional

from billing.period import month_bounds_utc
from tenants import db as _tenants_db

logger = logging.getLogger(__name__)

# What one use of each tool costs, in euro cents. The key is what callers pass
# to record() and what comes back in the per-tool breakdown.
TOOL_PRICES_CENTS = {
    "photo": 50,     # one enhanced/staged property photo
    "meeting": 100,  # one transcribed + extracted seller meeting
}

# Monthly credit allowance included with each subscription tier, in euro cents.
PLAN_ALLOWANCES_CENTS = {
    "base": 1500,
    "pro": 3000,
    "max": 6000,
}

# Used for a tenant whose plan we can't read (created before plans were stored,
# or holding a label we don't recognise). The smallest allowance is the safe
# guess: it never hands out credits the agency isn't paying for, and the
# mismatch shows up on their dashboard rather than silently in our margin.
_FALLBACK_PLAN = "base"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_usage (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id  TEXT NOT NULL,
  tool       TEXT NOT NULL,          -- photo | meeting
  cost_cents INTEGER NOT NULL,       -- price at the time of use, in euro cents
  ref        TEXT,                   -- the thing charged for, when it has an id
  created_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tool_usage_tenant ON tool_usage(tenant_id, created_at);

-- One charge per referenced thing. A meeting is billed once even though its
-- transcription session is reopened many times (the 60-minute session cap, a
-- dropped connection, resuming a stranded meeting all re-mint a token for the
-- same record id). Partial, so the ref-less rows — a photo is not tied to any
-- record — are never deduplicated against each other.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_usage_ref
  ON tool_usage(tenant_id, tool, ref) WHERE ref IS NOT NULL;
"""

_initialized = False


def init() -> None:
    """Create the usage table on the shared connection (idempotent)."""
    global _initialized
    conn = _tenants_db.get_connection()
    if _initialized:
        return
    with _tenants_db.write_lock:
        if not _initialized:
            conn.executescript(_SCHEMA)
            conn.commit()
            _initialized = True
            logger.info("Usage table ready (tool_usage)")


def _conn():
    if not _initialized:
        init()
    return _tenants_db.get_connection()


def normalize_plan(plan: Optional[str]) -> str:
    """The allowance key for a tenant's stored plan.

    Tenants store "Base"/"Pro"/"Max" (signup/router.py), but a plan copied from
    a Stripe label can carry the billing period too, in whichever language that
    label happens to be written in — "Base (Mensile)", "Base (Monthly)". Match
    on the leading tier word and ignore the rest, so the allowance never
    depends on the wording of a label maintained outside this code.
    """
    name = (plan or "").strip().lower()
    for key in PLAN_ALLOWANCES_CENTS:
        if name.startswith(key):
            return key
    if name:
        # An unrecognised plan string is a config problem worth seeing: the
        # tenant is being metered against the smallest allowance.
        logger.warning("Unknown plan %r — falling back to %s allowance", plan, _FALLBACK_PLAN)
    return _FALLBACK_PLAN


def allowance_cents(plan: Optional[str]) -> int:
    """Monthly credit allowance, in euro cents, for a tenant's plan."""
    return PLAN_ALLOWANCES_CENTS[normalize_plan(plan)]


def record(tenant_id: str, tool: str, ref: Optional[str] = None) -> bool:
    """Charge one use of `tool` against this tenant's credits.

    Returns True when it was charged, False when `ref` names something this
    tenant was already billed for (see the partial unique index above) — the
    caller can meter as often as it likes without double-charging.

    Called only after the tool has actually done its work: a failed photo
    enhancement or a session that never opened costs the agency nothing.
    """
    if tool not in TOOL_PRICES_CENTS:
        raise ValueError(f"Unknown tool: {tool}")

    conn = _conn()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "INSERT OR IGNORE INTO tool_usage "
            "(tenant_id, tool, cost_cents, ref, created_at) VALUES (?, ?, ?, ?, ?)",
            (tenant_id, tool, TOOL_PRICES_CENTS[tool], ref, now),
        )
        conn.commit()
    charged = cur.rowcount > 0
    if charged:
        logger.info(
            "Tool usage: tenant=%s tool=%s cost=%d cents ref=%s",
            tenant_id, tool, TOOL_PRICES_CENTS[tool], ref or "—",
        )
    return charged


def monthly_usage(
    tenant_id: str, now: Optional[datetime.datetime] = None
) -> dict[str, Any]:
    """What ONE tenant spent on AI tools in the current billing period, with a
    per-tool count. Strictly scoped by tenant_id."""
    year, month, start_utc, next_utc = month_bounds_utc(now)
    rows = _conn().execute(
        "SELECT tool, COUNT(*) AS uses, COALESCE(SUM(cost_cents), 0) AS cents "
        "FROM tool_usage "
        "WHERE tenant_id = ? AND created_at >= ? AND created_at < ? "
        "GROUP BY tool",
        (tenant_id, start_utc, next_utc),
    ).fetchall()

    # Every known tool is present at zero, so the dashboard doesn't have to
    # distinguish "not used" from "not in the response".
    uses = {tool: 0 for tool in TOOL_PRICES_CENTS}
    used_cents = 0
    for row in rows:
        used_cents += int(row["cents"] or 0)
        # A tool that has since been renamed/removed still counts toward the
        # bill, it just has no slot in the breakdown.
        if row["tool"] in uses:
            uses[row["tool"]] = int(row["uses"] or 0)
    return {"year": year, "month": month, "used_cents": used_cents, "uses": uses}


def monthly_credits(
    tenant_id: str, plan: Optional[str], now: Optional[datetime.datetime] = None
) -> dict[str, Any]:
    """This period's credit balance for ONE tenant: what the plan includes,
    what's left of it, and what has been spent beyond it.

    remaining and overage are each floored at zero and never both positive —
    together they say "you have X left" or "you're X over", which is exactly
    the pair of cards the dashboard shows.
    """
    usage = monthly_usage(tenant_id, now)
    allowance = allowance_cents(plan)
    used = usage["used_cents"]
    return {
        "plan": normalize_plan(plan).capitalize(),
        "year": usage["year"],
        "month": usage["month"],
        "allowance_cents": allowance,
        "used_cents": used,
        "remaining_cents": max(0, allowance - used),
        "overage_cents": max(0, used - allowance),
        "uses": usage["uses"],
    }

"""What a subscription includes, what has been used against it, and what that
puts on the end-of-month invoice.

A plan includes two separate allowances, and going past either one is billed:

  AI tools    — a credit balance (Base €15 / Pro €30 / Max €60) spent at €0.50
                per enhanced photo and €1.00 per ApollonIA Meeting. Every use
                is a row in tool_usage, this module's one table.
  Call minutes— included minutes (Base 500 / Pro 1000 / Max 2000), then €0.25
                a minute. Those minutes are counted where the calls are, in
                calls/db.py; only the pricing of them lives here.

tool_usage lives on the SAME connection as the tenants registry (see
tenants/db.py), reusing its process-wide connection and write lock, exactly
like calls/db.py. Every row carries tenant_id, and every read filters on it.

Nothing here is a stored counter that gets decremented. Both allowances reset
by construction, because what has been consumed is always summed over the
current billing period (billing/period.py) — so the first of the month is a
full balance again with no reset job to run, and no way for a missed run to
strand a tenant at zero. Past months stay readable for invoicing.

Running out does not disable anything: the excess becomes overage, which the
dashboard shows and the agency is invoiced for at the end of the month.

Money is integer euro cents throughout. It gets compared, summed and invoiced,
and none of that survives float rounding.
"""

import datetime
import logging
from typing import Any, Optional

from tenants import db as _tenants_db

logger = logging.getLogger(__name__)

# What one use of each tool costs, in euro cents. The key is what callers pass
# to record() and what comes back in the per-tool breakdown.
TOOL_PRICES_CENTS = {
    "photo": 50,     # one enhanced/staged property photo
    "meeting": 100,  # one transcribed + extracted seller meeting
}

# AI-tool credit allowance included with each subscription tier, in euro cents.
PLAN_ALLOWANCES_CENTS = {
    "base": 1500,
    "pro": 3000,
    "max": 6000,
}

# Call minutes included with each tier. Keyed identically to the credits above,
# so a new tier is added to both or neither.
PLAN_MINUTES = {
    "base": 500,
    "pro": 1000,
    "max": 2000,
}

# What each call minute past the included ones costs, in euro cents.
MINUTE_OVERAGE_CENTS = 25

# Used for a tenant whose plan we can't read (created before plans were stored,
# or holding a label we don't recognise). The smallest tier is the safe guess:
# it never hands out minutes or credits the agency isn't paying for, and the
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
    """Monthly AI-tool credit allowance, in euro cents, for a tenant's plan."""
    return PLAN_ALLOWANCES_CENTS[normalize_plan(plan)]


def minute_allowance(plan: Optional[str]) -> int:
    """Call minutes included per month with a tenant's plan."""
    return PLAN_MINUTES[normalize_plan(plan)]


def billable_minutes(seconds: int) -> int:
    """Whole billable minutes for a period's total call seconds.

    Every second in the period is summed first and converted here, once — no
    call is rounded up to a minute of its own, so a month of short calls is
    billed for the time it actually took.

    Integer arithmetic rounded half up, deliberately not Python's round():
    round() rounds halves to even, so round(1.5) == 2 but round(2.5) == 2, and
    an invoice must not depend on the parity of the minute it lands on. Here
    29s → 0, 30s → 1, 150s → 3, always.
    """
    return (max(0, int(seconds)) + 30) // 60


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


def monthly_usage(tenant_id: str, start_utc: str, end_utc: str) -> dict[str, Any]:
    """What ONE tenant spent on AI tools between two UTC ISO instants — their
    subscription month, resolved by the caller (see billing/period.py) — with a
    per-tool count. `end_utc` is exclusive. Strictly scoped by tenant_id."""
    rows = _conn().execute(
        "SELECT tool, COUNT(*) AS uses, COALESCE(SUM(cost_cents), 0) AS cents "
        "FROM tool_usage "
        "WHERE tenant_id = ? AND created_at >= ? AND created_at < ? "
        "GROUP BY tool",
        (tenant_id, start_utc, end_utc),
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
    return {"used_cents": used_cents, "uses": uses}


def monthly_credits(
    tenant_id: str,
    plan: Optional[str],
    minutes: int,
    start_utc: str,
    end_utc: str,
) -> dict[str, Any]:
    """This period's bill for ONE tenant: both allowances, what's left of each,
    and what the excess adds up to.

    `minutes` is the tenant's call minutes for the same period, counted by
    calls/db.py over these same bounds and passed in — the exact number the
    dashboard shows on its minutes card, so the card and the invoice can never
    disagree about how many minutes were charged.

    Within each allowance, remaining and over are floored at zero and never
    both positive: together they say "you have X left" or "you're X over".
    The top-level overage_cents is what actually gets invoiced — both kinds of
    excess, in one number, because the agency is billed once.
    """
    usage = monthly_usage(tenant_id, start_utc, end_utc)
    tool_allowance = allowance_cents(plan)
    tool_used = usage["used_cents"]
    tool_over = max(0, tool_used - tool_allowance)

    included_minutes = minute_allowance(plan)
    minutes = max(0, int(minutes))
    minutes_over = max(0, minutes - included_minutes)
    minute_over_cents = minutes_over * MINUTE_OVERAGE_CENTS

    return {
        "plan": normalize_plan(plan).capitalize(),
        "period_start": start_utc,
        "period_end": end_utc,
        "tools": {
            "allowance_cents": tool_allowance,
            "used_cents": tool_used,
            "remaining_cents": max(0, tool_allowance - tool_used),
            "overage_cents": tool_over,
            "uses": usage["uses"],
        },
        "minutes": {
            "included": included_minutes,
            "used": minutes,
            "remaining": max(0, included_minutes - minutes),
            "over": minutes_over,
            "price_cents": MINUTE_OVERAGE_CENTS,
            "overage_cents": minute_over_cents,
        },
        "overage_cents": tool_over + minute_over_cents,
    }

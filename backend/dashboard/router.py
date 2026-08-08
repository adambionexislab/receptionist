"""Agency-facing dashboard: login + tenant-scoped views.

Auth is a single per-tenant access code (see tenants.access_code). POST
/dashboard/login exchanges the code for a signed session cookie; every data
route depends on `current_tenant`, which reads that cookie and resolves the
tenant so all queries are strictly scoped to one tenant_id.

The page itself is one static SPA served at /dashboard (and aliased at
/sk/dashboard for URL continuity with the Slovak site). Locale is driven by the
logged-in tenant's `locale`, not the URL, so there is no separate Slovak page.
"""

import logging
import re
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from agents import db as agents_db
from calls import db as calls_db
from config import settings
from dashboard import session as sess
from listings import db as listings_db
from tenants import db
from usage import db as usage_db

logger = logging.getLogger(__name__)

router = APIRouter()

_PAGE = Path(__file__).parent / "index.html"


class LoginRequest(BaseModel):
    code: str


def current_tenant(request: Request) -> dict:
    """FastAPI dependency: resolve the logged-in tenant from the session cookie.

    Raises 401 when the cookie is missing/invalid/expired, or when the tenant it
    names no longer exists or has been deactivated. Data routes depend on this,
    so an unauthenticated request can never reach tenant-scoped data.
    """
    tenant_id = sess.read(request.cookies.get(sess.COOKIE_NAME))
    tenant = db.get_by_id(tenant_id) if tenant_id else None
    if not tenant or not tenant.get("active"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return tenant


@router.post("/dashboard/login")
def login(data: LoginRequest):
    tenant = db.get_by_access_code(data.code)
    if not tenant:
        # Same response for unknown/blank codes — don't reveal which codes exist.
        raise HTTPException(status_code=401, detail="Invalid code")
    resp = JSONResponse(
        {"agency_name": tenant["agency_name"], "locale": tenant.get("locale") or "it"}
    )
    resp.set_cookie(value=sess.issue(tenant["id"]), **sess.cookie_kwargs())
    logger.info("Dashboard login for tenant %s (%s)", tenant["id"], tenant["agency_name"])
    return resp


@router.post("/dashboard/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(key=sess.COOKIE_NAME, path="/")
    return resp


@router.get("/dashboard/api/me")
def me(tenant: dict = Depends(current_tenant)):
    return {
        "agency_name": tenant["agency_name"],
        "agent_name": tenant.get("agent_name") or "Apollonia",
        "locale": tenant.get("locale") or "it",
        "features": {"acquisizione": settings.ACQUISIZIONE_ENABLED},
    }


@router.get("/dashboard/api/contacts")
def contacts(tenant: dict = Depends(current_tenant)):
    """Contacts captured by Apollonia for THIS tenant, most recent first.
    Strictly scoped to the logged-in tenant's id."""
    return {"contacts": calls_db.list_contacts(tenant["id"])}


@router.get("/dashboard/api/summary")
def summary(tenant: dict = Depends(current_tenant)):
    """This billing period's numbers for THIS tenant: call activity, and the
    two allowances the plan includes (minutes and AI-tool credits) with what
    the excess will be invoiced. Calls are counted only since call persistence
    went live (there is no earlier history); tool credits are summed over the
    same window, so both allowances reset together. Strictly scoped to the
    logged-in tenant's id.

    The minutes the dashboard displays are the minutes billed: the period's
    seconds are converted once (usage_db.billable_minutes) and that one number
    feeds both the card and the credits block, so they can't disagree.

    Money is sent as integer euro cents — the browser divides for display, and
    nothing that gets invoiced is ever rounded through a float on the way here.
    """
    stats = calls_db.monthly_call_stats(tenant["id"])
    minutes = usage_db.billable_minutes(stats["seconds"])
    return {
        "year": stats["year"],
        "month": stats["month"],
        "minutes": minutes,
        "seconds": stats["seconds"],
        "calls": stats["calls"],
        "contacts": stats["contacts"],
        "credits": usage_db.monthly_credits(tenant["id"], tenant.get("plan"), minutes),
    }


@router.get("/dashboard/api/listings")
def listings(tenant: dict = Depends(current_tenant)):
    """The tenant's current listings — the same rows the phone agent searches
    (listings/db.py), so what the agency edits here is what Apollonia says on
    the phone. Strictly scoped to the logged-in tenant's id."""
    return {"listings": listings_db.list_for_tenant(tenant["id"])}


class ListingCreate(BaseModel):
    """A listing the agency enters by hand, rather than one the portal scrape
    or an Acquisizione meeting produced. Only the address is required — it is
    what the phone agent matches a caller's "I'm calling about Via Roma" on,
    and a listing without one can never be found."""
    address: str
    zone: str = ""
    type: Literal["vendita", "affitto"] = "vendita"
    rooms: int = 0
    size_sqm: int = 0
    price: int = 0
    currency: str = "EUR"
    available: bool = True
    text: str = ""
    agent_id: Optional[str] = None


@router.post("/dashboard/api/listings", status_code=201)
def create_listing(data: ListingCreate, tenant: dict = Depends(current_tenant)):
    """Add a listing by hand. Stored as source='manual' (see listings/db.py),
    so a later Immobiliare.it scrape never overwrites or removes it — it isn't
    on the portal, and only the agency knows about it."""
    fields = data.model_dump()
    agent_id = (fields.pop("agent_id") or "").strip() or None
    if not fields["address"].strip():
        raise HTTPException(status_code=422, detail="Address is required")
    # An id from another tenant would route this agency's leads to a stranger.
    if agent_id and not agents_db.get(agent_id, tenant["id"]):
        raise HTTPException(status_code=422, detail="Unknown agent")
    return listings_db.create_manual(tenant["id"], fields, agent_id)


class ListingUpdate(BaseModel):
    """Agent-editable listing fields. All optional — a PATCH may send only
    what changed. Unknown fields are ignored by listings_db.update."""
    address: Optional[str] = None
    zone: Optional[str] = None
    type: Optional[Literal["vendita", "affitto"]] = None
    rooms: Optional[int] = None
    size_sqm: Optional[int] = None
    price: Optional[int] = None
    currency: Optional[str] = None
    available: Optional[bool] = None
    text: Optional[str] = None
    # Which agent handles the property. Sent as "" to unassign, so this one is
    # excluded from the exclude_none filtering below and handled separately.
    agent_id: Optional[str] = None


@router.patch("/dashboard/api/listings/{listing_id}")
def update_listing(
    listing_id: str, data: ListingUpdate, tenant: dict = Depends(current_tenant)
):
    """Edit one listing. Marks it agent-owned so a later Immobiliare.it scrape
    won't revert the change (see listings/db.py).

    The agent assignment is applied through listings_db.set_agent instead, which
    deliberately does NOT mark the row edited: assigning an agent must not stop
    the portal scrape from refreshing the listing's price and description.
    """
    sent = data.model_dump(exclude_unset=True)
    fields = {
        k: v for k, v in sent.items() if k != "agent_id" and v is not None
    }
    # Same rule as creating one: a listing the phone agent can't match an
    # address against is a listing no caller can ever be told about.
    if "address" in fields and not fields["address"].strip():
        raise HTTPException(status_code=422, detail="Address is required")

    updated = None
    if "agent_id" in sent:
        agent_id = (sent["agent_id"] or "").strip() or None
        # An id from another tenant would silently route that agency's leads to
        # a stranger — resolve it against this tenant before storing it.
        if agent_id and not agents_db.get(agent_id, tenant["id"]):
            raise HTTPException(status_code=422, detail="Unknown agent")
        updated = listings_db.set_agent(listing_id, tenant["id"], agent_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="Not found")

    if fields:
        updated = listings_db.update(listing_id, tenant["id"], fields)
    elif updated is None:
        updated = listings_db.get(listing_id, tenant["id"])
    if updated is None:
        raise HTTPException(status_code=404, detail="Not found")
    return updated


@router.delete("/dashboard/api/listings/{listing_id}")
def delete_listing(listing_id: str, tenant: dict = Depends(current_tenant)):
    """Remove one listing so the phone agent stops offering it. Soft-deleted,
    so a later scrape can't resurrect it (see listings/db.py)."""
    if not listings_db.delete(listing_id, tenant["id"]):
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}


# ── agents (the agency's own staff, not Apollonia) ──────────────────────────
# Deliberately not a full email validator (that needs a dependency and rejects
# valid-but-unusual addresses): just enough to catch a typo'd address before it
# lands on a card.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AgentCreate(BaseModel):
    name: str
    email: str


class AgentUpdate(BaseModel):
    """All optional — a PATCH may send only what changed. `number` is assigned
    by the server and is not editable."""
    name: Optional[str] = None
    email: Optional[str] = None


def _clean_agent_fields(fields: dict) -> dict:
    """Trim and validate the agent fields present in `fields` (422 on bad input).

    Both name and email are required, so a PATCH may omit a field but may not
    blank one out.
    """
    cleaned = {k: (v or "").strip() for k, v in fields.items()}
    if "name" in cleaned and not cleaned["name"]:
        raise HTTPException(status_code=422, detail="Name is required")
    if "email" in cleaned:
        if not cleaned["email"]:
            raise HTTPException(status_code=422, detail="Email is required")
        if not _EMAIL_RE.match(cleaned["email"]):
            raise HTTPException(status_code=422, detail="Invalid email")
    return cleaned


@router.get("/dashboard/api/agents")
def agents(tenant: dict = Depends(current_tenant)):
    """The agency's agents, in the order they were added, each with how many
    listings they handle — the dashboard shows that count before confirming a
    deletion, since deleting an agent leaves their listings unassigned (and
    their leads going to the agency inbox). Strictly scoped to the logged-in
    tenant's id."""
    rows = agents_db.list_for_tenant(tenant["id"])
    for agent in rows:
        agent["listing_count"] = listings_db.count_for_agent(tenant["id"], agent["id"])
    return {"agents": rows}


@router.post("/dashboard/api/agents")
def create_agent(data: AgentCreate, tenant: dict = Depends(current_tenant)):
    """Add an agent. The server assigns their number (see agents/db.py)."""
    fields = _clean_agent_fields(data.model_dump())
    return agents_db.create(tenant["id"], fields["name"], fields["email"])


@router.patch("/dashboard/api/agents/{agent_id}")
def update_agent(
    agent_id: str, data: AgentUpdate, tenant: dict = Depends(current_tenant)
):
    fields = _clean_agent_fields(data.model_dump(exclude_unset=True, exclude_none=True))
    updated = agents_db.update(agent_id, tenant["id"], fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="Not found")
    return updated


@router.delete("/dashboard/api/agents/{agent_id}")
def delete_agent(agent_id: str, tenant: dict = Depends(current_tenant)):
    if not agents_db.delete(agent_id, tenant["id"]):
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}


# ── page (served before the catch-all StaticFiles mount in main.py) ──────────
@router.get("/dashboard")
@router.get("/sk/dashboard")
def dashboard_page():
    return FileResponse(str(_PAGE))

"""Agency-facing dashboard: login + tenant-scoped views.

Auth is a single per-tenant access code (see tenants.access_code). POST
/dashboard/login exchanges the code for a signed session cookie; every data
route depends on `current_tenant`, which reads that cookie and resolves the
tenant so all queries are strictly scoped to one tenant_id.

Inside a tenant there is a second, non-security scope: the branch (the agency's
office — see branches/db.py). The page sends the selected one in an X-Branch-Id
header on every request and `current_branch` resolves it against the tenant, so
a branch id from another agency is a 404 rather than a filter. No branch header
means the whole agency, which is what every response looked like before
branches existed.

The page itself is one static SPA served at /dashboard (and aliased at
/sk/dashboard for URL continuity with the Slovak site). Locale is driven by the
logged-in tenant's `locale`, not the URL, so there is no separate Slovak page.
"""

import asyncio
import logging
import re
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from agents import db as agents_db
from billing import period
from branches import db as branches_db
from calls import db as calls_db
from config import settings
from dashboard import session as sess
from geo import geocode
from listings import db as listings_db
from listings import store
from tenants import db
from usage import db as usage_db

logger = logging.getLogger(__name__)

router = APIRouter()

_PAGE = Path(__file__).parent / "index.html"

# Header the page sends the selected branch in. A header rather than a query
# parameter so the one place the page makes requests (its api() helper) can set
# it for every endpoint at once — including the AI tools, which meter what they
# spend against the branch that was selected when they ran.
BRANCH_HEADER = "X-Branch-Id"


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


def current_branch(
    request: Request, tenant: dict = Depends(current_tenant)
) -> Optional[dict]:
    """FastAPI dependency: the branch the page is scoped to, or None for the
    whole agency.

    Resolved against the logged-in tenant, so an id belonging to another agency
    (or to a branch that has since been closed) is a 404 and never a filter
    that silently matches nothing. This is a view scope, not a permission
    boundary: everyone who can log in can see every branch, and switching to
    the whole agency shows all of it.
    """
    branch_id = (request.headers.get(BRANCH_HEADER) or "").strip()
    if not branch_id:
        return None
    branch = branches_db.get(branch_id, tenant["id"])
    if branch is None:
        raise HTTPException(status_code=404, detail="Unknown branch")
    return branch


def _branch_id(branch: Optional[dict]) -> Optional[str]:
    return branch["id"] if branch else None


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
        "features": {
            "acquisizione": settings.ACQUISIZIONE_ENABLED,
            "video_tour": settings.VIDEO_TOUR_ENABLED,
        },
    }


@router.get("/dashboard/api/contacts")
def contacts(
    tenant: dict = Depends(current_tenant),
    branch: Optional[dict] = Depends(current_branch),
):
    """Contacts captured by Apollonia for THIS tenant, most recent first.
    Strictly scoped to the logged-in tenant's id, and to the selected branch
    when there is one."""
    return {
        "contacts": calls_db.list_contacts(tenant["id"], branch_id=_branch_id(branch))
    }


@router.get("/dashboard/api/summary")
def summary(
    tenant: dict = Depends(current_tenant),
    branch: Optional[dict] = Depends(current_branch),
):
    """This billing period's numbers for THIS tenant: call activity, and the
    two allowances the plan includes (minutes and AI-tool credits) with what
    the excess will be invoiced. Strictly scoped to the logged-in tenant's id.

    The period is the tenant's subscription month, resolved once here and
    passed to both data modules, so every number on the page covers exactly the
    same window and both allowances reset on the day the agency is charged.
    Calls are counted only since call persistence went live — there is no
    earlier history.

    The minutes the dashboard displays are the minutes billed: the period's
    seconds are converted once (usage_db.billable_minutes) and that one number
    feeds both the card and the credits block, so they can't disagree.

    Money is sent as integer euro cents — the browser divides for display, and
    nothing that gets invoiced is ever rounded through a float on the way here.

    WITH A BRANCH SELECTED the numbers are that office's activity, and the
    billing block is deliberately NOT part of the response. Allowances and
    overage belong to the subscription, which is the agency's: there is no such
    thing as a branch's remaining credits, and showing one would invent a
    second bill. What the branch view carries instead is what that office used,
    plus the agency's totals for the same period, so its share is readable
    without pretending to be an invoice. The whole-agency view is the one that
    bills, and it is unchanged.
    """
    start_utc, end_utc = period.tenant_month_utc(tenant)
    if branch is None:
        stats = calls_db.monthly_call_stats(tenant["id"], start_utc, end_utc)
        minutes = usage_db.billable_minutes(stats["seconds"])
        return {
            "scope": "agency",
            "period_start": start_utc,
            "period_end": end_utc,
            "minutes": minutes,
            "seconds": stats["seconds"],
            "calls": stats["calls"],
            "contacts": stats["contacts"],
            "credits": usage_db.monthly_credits(
                tenant["id"], tenant.get("plan"), minutes, start_utc, end_utc
            ),
        }

    stats = calls_db.monthly_call_stats(tenant["id"], start_utc, end_utc, branch["id"])
    tools = usage_db.monthly_usage(tenant["id"], start_utc, end_utc, branch["id"])
    agency_stats = calls_db.monthly_call_stats(tenant["id"], start_utc, end_utc)
    agency_tools = usage_db.monthly_usage(tenant["id"], start_utc, end_utc)
    return {
        "scope": "branch",
        "branch": {"id": branch["id"], "name": branch["name"]},
        "period_start": start_utc,
        "period_end": end_utc,
        "minutes": usage_db.billable_minutes(stats["seconds"]),
        "seconds": stats["seconds"],
        "calls": stats["calls"],
        "contacts": stats["contacts"],
        "tools": tools,
        # The same two figures for the whole agency, so each branch card can
        # say "x of the agency's y" instead of standing on its own.
        "agency": {
            "minutes": usage_db.billable_minutes(agency_stats["seconds"]),
            "tools_used_cents": agency_tools["used_cents"],
        },
    }


@router.get("/dashboard/api/listings")
def listings(
    tenant: dict = Depends(current_tenant),
    branch: Optional[dict] = Depends(current_branch),
):
    """The tenant's current listings — the same rows the phone agent searches
    (listings/db.py), so what the agency edits here is what Apollonia says on
    the phone. Strictly scoped to the logged-in tenant's id.

    A branch's listings are the ones its agents handle. This filter is the
    dashboard's alone: the phone agent always searches the agency's whole
    inventory, because a caller ringing the agency's number has not chosen an
    office. A listing with no agent belongs to no branch and is therefore only
    visible in the whole-agency view — which is where it should be looked at
    anyway, since its leads go to the agency inbox.
    """
    rows = listings_db.list_for_tenant(tenant["id"])
    if branch is not None:
        owned = set(agents_db.ids_for_branch(tenant["id"], branch["id"]))
        rows = [l for l in rows if l.get("agent_id") in owned]
    return {"listings": rows}


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
    # Which office they work out of. Omitted → the branch the page is scoped to
    # (adding someone from the Milano view puts them in Milano); "" → none.
    branch_id: Optional[str] = None


class AgentUpdate(BaseModel):
    """All optional — a PATCH may send only what changed. `number` is assigned
    by the server and is not editable."""
    name: Optional[str] = None
    email: Optional[str] = None
    # Sent as "" to move an agent out of every office, so — like a listing's
    # agent_id — this one is handled separately from the exclude_none fields.
    branch_id: Optional[str] = None


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


def _resolve_branch_id(value: Optional[str], tenant_id: str) -> Optional[str]:
    """Validate a branch id sent for an agent. "" / None → no office.

    An id from another agency would put a stranger's office on this agency's
    reporting, so it is resolved against the tenant before it is stored — the
    same rule listings apply to agent_id.
    """
    branch_id = (value or "").strip() or None
    if branch_id and not branches_db.get(branch_id, tenant_id):
        raise HTTPException(status_code=422, detail="Unknown branch")
    return branch_id


@router.get("/dashboard/api/agents")
def agents(
    tenant: dict = Depends(current_tenant),
    branch: Optional[dict] = Depends(current_branch),
):
    """The agency's agents, in the order they were added, each with how many
    listings they handle — the dashboard shows that count before confirming a
    deletion, since deleting an agent leaves their listings unassigned (and
    their leads going to the agency inbox). Strictly scoped to the logged-in
    tenant's id, and to the selected branch when there is one."""
    rows = agents_db.list_for_tenant(tenant["id"], branch_id=_branch_id(branch))
    for agent in rows:
        agent["listing_count"] = listings_db.count_for_agent(tenant["id"], agent["id"])
    return {"agents": rows}


@router.post("/dashboard/api/agents")
def create_agent(
    data: AgentCreate,
    tenant: dict = Depends(current_tenant),
    branch: Optional[dict] = Depends(current_branch),
):
    """Add an agent. The server assigns their number (see agents/db.py).

    An agent added while the page is scoped to a branch joins that branch
    unless the form says otherwise — otherwise they would be added to the very
    view they just disappeared from.
    """
    sent = data.model_dump()
    fields = _clean_agent_fields({"name": sent["name"], "email": sent["email"]})
    branch_id = (
        _resolve_branch_id(sent["branch_id"], tenant["id"])
        if sent["branch_id"] is not None
        else _branch_id(branch)
    )
    return agents_db.create(
        tenant["id"], fields["name"], fields["email"], branch_id=branch_id
    )


@router.patch("/dashboard/api/agents/{agent_id}")
def update_agent(
    agent_id: str, data: AgentUpdate, tenant: dict = Depends(current_tenant)
):
    sent = data.model_dump(exclude_unset=True)
    fields = _clean_agent_fields(
        {k: v for k, v in sent.items() if k != "branch_id" and v is not None}
    )
    if "branch_id" in sent:
        fields["branch_id"] = _resolve_branch_id(sent["branch_id"], tenant["id"])
    updated = agents_db.update(agent_id, tenant["id"], fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="Not found")
    return updated


@router.delete("/dashboard/api/agents/{agent_id}")
def delete_agent(agent_id: str, tenant: dict = Depends(current_tenant)):
    if not agents_db.delete(agent_id, tenant["id"]):
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}


# ── branches (the agency's offices) ─────────────────────────────────────────
class BranchCreate(BaseModel):
    name: str
    # The office's street address. Not decoration: geocoding it is what gives
    # the branch its district and coordinates, and those are what route a
    # seller call to it (see branches/routing.py).
    address: str = ""
    # Places this office covers, one per line. Optional — the district derived
    # from the address already covers the ordinary case. This is the override
    # for when it doesn't, and the way two offices in one city are separated.
    areas: str = ""


class BranchUpdate(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    areas: Optional[str] = None


def _clean_branch_name(name: Optional[str]) -> str:
    """A branch is picked from a list by its name, so a nameless one would be an
    unlabelled entry in the switcher nobody could identify."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise HTTPException(status_code=422, detail="Name is required")
    return cleaned


async def _locate_branch(branch: dict, tenant: dict) -> dict:
    """Geocode an office's address into the district and coordinates that route
    calls to it, and store them.

    Runs on every save that touched the address, and clears the derived fields
    when it can't resolve one — stale coordinates from a previous address would
    quietly route seller calls to where the office used to be, which is worse
    than not routing them at all.

    Never raises: an office with no coordinates still works everywhere else in
    the dashboard, it just stops being reachable by the last two steps of the
    routing chain. The agency sees that on the card.
    """
    address = (branch.get("address") or "").strip()
    lat = lng = None
    district = ""
    if address:
        try:
            located = await geocode.lookup(address, tenant.get("locale") or "it")
            lat, lng = located["lat"], located["lng"]
            district = located.get("district") or ""
        except Exception as exc:
            logger.info(
                "Could not locate branch %s at %r: %s", branch["id"], address, exc
            )
    await asyncio.to_thread(
        branches_db.set_location, branch["id"], tenant["id"], lat, lng, district
    )
    branch["lat"], branch["lng"], branch["district"] = lat, lng, district
    return branch


@router.get("/dashboard/api/branches")
def branches(tenant: dict = Depends(current_tenant)):
    """The agency's offices, oldest first, each with how many agents work
    there — shown on the card, and before confirming a deletion, since closing
    a branch detaches its agents (see branches/db.py).

    Deliberately NOT filtered by the selected branch: this is the list the
    switcher is built from, and it has to keep offering the others.
    """
    rows = branches_db.list_for_tenant(tenant["id"])
    for branch in rows:
        branch["agent_count"] = len(
            agents_db.ids_for_branch(tenant["id"], branch["id"])
        )
    return {"branches": rows}


@router.post("/dashboard/api/branches", status_code=201)
async def create_branch(data: BranchCreate, tenant: dict = Depends(current_tenant)):
    branch = await asyncio.to_thread(
        branches_db.create,
        tenant["id"],
        _clean_branch_name(data.name),
        (data.address or "").strip(),
        (data.areas or "").strip(),
    )
    branch = await _locate_branch(branch, tenant)
    branch["agent_count"] = 0
    return branch


@router.patch("/dashboard/api/branches/{branch_id}")
async def update_branch(
    branch_id: str, data: BranchUpdate, tenant: dict = Depends(current_tenant)
):
    sent = data.model_dump(exclude_unset=True)
    fields: dict = {}
    if "name" in sent:
        fields["name"] = _clean_branch_name(sent["name"])
    if "address" in sent:
        fields["address"] = (sent["address"] or "").strip()
    if "areas" in sent:
        fields["areas"] = (sent["areas"] or "").strip()

    updated = await asyncio.to_thread(
        branches_db.update, branch_id, tenant["id"], fields
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Not found")
    # Only when the address actually changed: geocoding is a paid round trip,
    # and renaming an office must not spend one.
    if "address" in fields:
        updated = await _locate_branch(updated, tenant)
    updated["agent_count"] = len(agents_db.ids_for_branch(tenant["id"], branch_id))
    return updated


@router.get("/dashboard/api/branches/coverage-suggestions")
def branch_coverage_suggestions(tenant: dict = Depends(current_tenant)):
    """Which places each office already works, derived from the listings its own
    agents handle: {branch_id: ["Bratislava", "Pezinok", ...]}.

    Nobody should have to type a coverage list that the agency's own inventory
    already describes. A listing has a zone and an agent; that agent has an
    office; so the split is sitting in the data — this just reads it back so
    the Sedi tab can offer it as a starting point.

    Note what this does NOT cover, and why the district lookup exists: a seller
    calls about a village the agency has no listings in yet. Deriving from
    inventory is strong exactly where the agency already operates and blind
    everywhere else, which is the opposite of the geocoded-district step, and
    that is why routing has both.
    """
    branch_of_agent = {
        agent["id"]: agent.get("branch_id")
        for agent in agents_db.list_for_tenant(tenant["id"])
    }
    found: dict[str, list[str]] = {}
    for listing in listings_db.list_for_tenant(tenant["id"], available_only=False):
        branch_id = branch_of_agent.get(listing.get("agent_id"))
        zone = (listing.get("zone") or "").strip()
        if not branch_id or not zone:
            continue
        zones = found.setdefault(branch_id, [])
        # Case/diacritic-insensitive de-dup, keeping the first spelling seen so
        # the agency reads back its own wording rather than a normalised one.
        if not any(store.normalize_place(zone) == store.normalize_place(z) for z in zones):
            zones.append(zone)
    return {"suggestions": {bid: sorted(zones) for bid, zones in found.items()}}


@router.delete("/dashboard/api/branches/{branch_id}")
def delete_branch(branch_id: str, tenant: dict = Depends(current_tenant)):
    """Close a branch. Its agents stay — they keep their listings and their
    leads, and simply belong to no office until they are moved to another one.
    Past calls and tool uses keep pointing at it and stay in the agency's
    totals; they just stop being reachable through the switcher."""
    if not branches_db.delete(branch_id, tenant["id"]):
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}


# ── page (served before the catch-all StaticFiles mount in main.py) ──────────
@router.get("/dashboard")
@router.get("/sk/dashboard")
def dashboard_page():
    return FileResponse(str(_PAGE))

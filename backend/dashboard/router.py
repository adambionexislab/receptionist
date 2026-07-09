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
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from dashboard import session as sess
from tenants import db

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
    }


# ── page (served before the catch-all StaticFiles mount in main.py) ──────────
@router.get("/dashboard")
@router.get("/sk/dashboard")
def dashboard_page():
    return FileResponse(str(_PAGE))

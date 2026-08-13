"""Internal lead-gen dashboard: login + the page itself.

Everything behind this login is ApollonIA's own: prospecting campaigns, the
agencies we've cold-emailed, and the sales rep's meeting notes. None of it is
tenant data, so auth here is one shared staff password rather than the
per-agency access codes of dashboard/router.py.

The page used to be a separate Render static site calling this API from another
origin. It is served from this app instead so the session cookie is
first-party: a third-party cookie is dropped outright by Safari, and the rep
records her notes on a phone.

`require_staff` is the dependency every protected route hangs off — it is
attached to whole routers (see routers/leads.py and salesnotes/router.py)
rather than route by route, so a new endpoint on either is private by default
and has to be moved out deliberately to be public.
"""

import asyncio
import hmac
import logging
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from config import settings
from leadgen import session as sess

logger = logging.getLogger(__name__)

router = APIRouter()

_PAGE = Path(__file__).parent / "index.html"

# ── brute-force throttle ─────────────────────────────────────────────────────
# One shared password on a publicly reachable endpoint that gates real spend
# (OpenAI transcription, Google Places, outbound email), so guessing has to be
# expensive. Two independent brakes, because either alone is weak: a per-IP
# lockout is only as good as the client address (X-Forwarded-For is the
# platform's on Render, but a direct caller can invent one), and a per-attempt
# delay caps the rate on any single connection no matter what it claims to be.
_MAX_FAILURES = 8
_WINDOW_SECONDS = 15 * 60
_FAIL_DELAY_SECONDS = 1.0
_failures: dict[str, list[float]] = {}


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _recent_failures(key: str) -> list[float]:
    cutoff = time.time() - _WINDOW_SECONDS
    recent = [t for t in _failures.get(key, []) if t > cutoff]
    if recent:
        _failures[key] = recent
    else:
        _failures.pop(key, None)
    return recent


def _record_failure(key: str) -> None:
    _failures.setdefault(key, []).append(time.time())


class LoginRequest(BaseModel):
    password: str


@router.post("/leadgen/login")
async def login(data: LoginRequest, request: Request):
    key = _client_key(request)
    if len(_recent_failures(key)) >= _MAX_FAILURES:
        logger.warning("Lead-gen login locked out for %s", key)
        raise HTTPException(status_code=429, detail="Too many attempts, try again later")

    expected = settings.LEADGEN_PASSWORD or ""
    # Fail closed: with no password configured the dashboard is unreachable
    # rather than open. An unset env var must never mean "let everyone in".
    if not expected:
        logger.error("LEADGEN_PASSWORD not set — refusing all lead-gen logins")
        raise HTTPException(status_code=503, detail="Not configured")

    if not hmac.compare_digest(data.password, expected):
        _record_failure(key)
        await asyncio.sleep(_FAIL_DELAY_SECONDS)
        raise HTTPException(status_code=401, detail="Invalid password")

    _failures.pop(key, None)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(value=sess.issue(), **sess.cookie_kwargs())
    logger.info("Lead-gen login from %s", key)
    return resp


@router.post("/leadgen/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(key=sess.COOKIE_NAME, path="/")
    return resp


def require_staff(request: Request) -> None:
    """FastAPI dependency: 401 unless this browser holds a valid staff session.

    Attached to whole routers, so every lead-gen and notes endpoint is private
    without anyone having to remember to protect it.
    """
    if not sess.valid(request.cookies.get(sess.COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="Not authenticated")


@router.get("/leadgen/api/me")
def me(request: Request):
    """Whether this browser is still logged in — the page's boot check, which
    decides between the login screen and the dashboard. Deliberately not
    behind require_staff: a plain false is the answer, not a 401."""
    return {"authenticated": sess.valid(request.cookies.get(sess.COOKIE_NAME))}


# ── page (served before the catch-all StaticFiles mount in main.py) ──────────
# Public on purpose: the HTML holds no data, and it has to load in order to
# show the login form. Everything it then asks for is behind require_staff.
@router.get("/leadgen")
@router.get("/leadgen/")
def leadgen_page():
    return FileResponse(str(_PAGE))

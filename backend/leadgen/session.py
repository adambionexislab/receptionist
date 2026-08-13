"""Signed session cookie for the internal lead-gen dashboard.

Login is one shared staff password (settings.LEADGEN_PASSWORD). Once entered,
the browser gets an HMAC-signed cookie so the device stays logged in — the rep
records a note straight after a meeting, on a phone, and a password prompt at
that moment is a note that doesn't get recorded.

Mirrors dashboard/session.py (the agency-facing equivalent) deliberately rather
than sharing code with it: the two are separate audiences with separate
lifetimes, and a bug that let one cookie be accepted as the other would hand an
agency the whole prospecting pipeline. Different cookie name AND a different
signed payload key, so neither can ever be read as the other.

Signing reuses the same manual HMAC-SHA256 scheme as the webhook handlers, so
no new dependency is pulled in.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

COOKIE_NAME = "apollonia_staff"
# 30 days, same as the agency dashboard: this is an internal tool used in short
# bursts between meetings, and re-typing a shared password every visit is how a
# shared password ends up written on a sticky note.
_MAX_AGE = 30 * 24 * 3600

# What the payload has to say for this cookie to be a staff session. The agency
# cookie carries {"t": tenant_id} instead, so neither validates as the other
# even if both were signed with the same key.
_SUBJECT = "leadgen-staff"

# Ephemeral fallback key, generated once per process. Only used when neither
# SESSION_SECRET nor ADMIN_TOKEN is set (dev): sessions reset on restart.
_EPHEMERAL_KEY = secrets.token_hex(32)


def _signing_key() -> str:
    key = settings.SESSION_SECRET or settings.ADMIN_TOKEN
    if key:
        return key
    logger.warning(
        "Neither SESSION_SECRET nor ADMIN_TOKEN set — signing lead-gen "
        "sessions with an ephemeral key (all sessions drop on restart)"
    )
    return _EPHEMERAL_KEY


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def issue() -> str:
    """Build a signed cookie value marking this browser as logged-in staff."""
    payload = json.dumps(
        {"s": _SUBJECT, "exp": int(time.time()) + _MAX_AGE},
        separators=(",", ":"),
    ).encode()
    sig = hmac.new(_signing_key().encode(), payload, hashlib.sha256).digest()
    return f"{_b64e(payload)}.{_b64e(sig)}"


def valid(cookie_value: Optional[str]) -> bool:
    """True only for a cookie this server signed, that hasn't expired, and that
    carries the staff subject. Absent, tampered with, expired or issued for a
    different audience all read the same: not logged in."""
    if not cookie_value or "." not in cookie_value:
        return False
    payload_b64, _, sig_b64 = cookie_value.partition(".")
    try:
        payload = _b64d(payload_b64)
        sig = _b64d(sig_b64)
    except Exception:
        return False
    expected = hmac.new(_signing_key().encode(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        data = json.loads(payload)
    except Exception:
        return False
    if data.get("s") != _SUBJECT:
        return False
    return int(data.get("exp", 0)) >= time.time()


def cookie_kwargs() -> dict:
    """Shared Set-Cookie attributes. The dashboard is served by this same app
    (see leadgen/router.py), so the cookie is first-party and SameSite=Lax both
    works everywhere — iPhone Safari included — and blocks the cookie from
    riding along on a cross-site POST, which is what stands in for CSRF
    protection here. Secure only over HTTPS, so http://localhost still works."""
    return {
        "key": COOKIE_NAME,
        "max_age": _MAX_AGE,
        "httponly": True,
        "samesite": "lax",
        "secure": settings.PUBLIC_BASE_URL.startswith("https"),
        "path": "/",
    }

"""Signed session cookie for the agency dashboard.

Login is a single per-tenant access code (tenants.access_code). Once entered,
we hand the browser an HMAC-signed cookie carrying the tenant_id so it doesn't
re-enter the code every visit. The cookie is a bearer credential — deliberately
low-ceremony, matching the "just some stats behind the login" threat model —
but it is signed so it can't be forged into another tenant's id.

Signing reuses the same manual HMAC-SHA256 scheme the webhook handlers use, so
no new dependency (itsdangerous et al.) is pulled in.
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

COOKIE_NAME = "apollonia_session"
# 30 days: agencies check stats occasionally, so a long-lived session spares
# them re-pasting the code. They can log out to clear it early.
_MAX_AGE = 30 * 24 * 3600

# Ephemeral fallback key, generated once per process. Only used when neither
# SESSION_SECRET nor ADMIN_TOKEN is set (dev): sessions then reset on restart.
_EPHEMERAL_KEY = secrets.token_hex(32)


def _signing_key() -> str:
    key = settings.SESSION_SECRET or settings.ADMIN_TOKEN
    if key:
        return key
    logger.warning(
        "Neither SESSION_SECRET nor ADMIN_TOKEN set — signing dashboard "
        "sessions with an ephemeral key (all sessions drop on restart)"
    )
    return _EPHEMERAL_KEY


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def issue(tenant_id: str) -> str:
    """Build a signed cookie value binding this browser to `tenant_id`."""
    payload = json.dumps(
        {"t": tenant_id, "exp": int(time.time()) + _MAX_AGE},
        separators=(",", ":"),
    ).encode()
    sig = hmac.new(_signing_key().encode(), payload, hashlib.sha256).digest()
    return f"{_b64e(payload)}.{_b64e(sig)}"


def read(cookie_value: Optional[str]) -> Optional[str]:
    """Verify a cookie value and return its tenant_id, or None if it is absent,
    tampered with, or expired."""
    if not cookie_value or "." not in cookie_value:
        return None
    payload_b64, _, sig_b64 = cookie_value.partition(".")
    try:
        payload = _b64d(payload_b64)
        sig = _b64d(sig_b64)
    except Exception:
        return None
    expected = hmac.new(_signing_key().encode(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if int(data.get("exp", 0)) < time.time():
        return None
    tenant_id = data.get("t")
    return tenant_id if isinstance(tenant_id, str) else None


def cookie_kwargs() -> dict:
    """Shared Set-Cookie attributes. Secure only over HTTPS so the cookie still
    works on http://localhost during local dev."""
    return {
        "key": COOKIE_NAME,
        "max_age": _MAX_AGE,
        "httponly": True,
        "samesite": "lax",
        "secure": settings.PUBLIC_BASE_URL.startswith("https"),
        "path": "/",
    }

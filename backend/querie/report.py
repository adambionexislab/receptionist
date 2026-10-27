"""The PDF report behind /querie/pdf_report.

The PDF is not in the public assets mount: it is handed out through a link
signed for the email that asked for it, so the same link works on the page and
in the email we send, and can be counted per lead.
"""

import base64
import hashlib
import hmac
from pathlib import Path
from typing import Optional

from config import settings
from querie import config as qc


def _signing_key() -> Optional[bytes]:
    key = settings.SESSION_SECRET or settings.ADMIN_TOKEN
    return key.encode() if key else None


def _sig(email: str, key: bytes) -> str:
    return hmac.new(key, f"querie-report:{email}".encode(), hashlib.sha256).hexdigest()[:32]


def download_token(email: str) -> Optional[str]:
    key = _signing_key()
    if not key:
        return None
    email = email.strip().lower()
    encoded = base64.urlsafe_b64encode(email.encode()).decode().rstrip("=")
    return f"{encoded}.{_sig(email, key)}"


def verify_token(token: str) -> Optional[str]:
    """The email a token was issued for, or None if it is forged or malformed."""
    key = _signing_key()
    if not key or not token or "." not in token:
        return None
    encoded, sig = token.rsplit(".", 1)
    try:
        email = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(sig, _sig(email, key)):
        return None
    return email


def download_url(email: str, absolute: bool = False) -> Optional[str]:
    token = download_token(email)
    if not token:
        return None
    path = f"{qc.REPORT_PATH}/stiahnut?t={token}"
    return f"{qc.SITE_URL}{path}" if absolute else path


def pdf_path() -> Optional[Path]:
    path = qc.REPORT_DIR / qc.REPORT_FILE
    return path if path.is_file() else None

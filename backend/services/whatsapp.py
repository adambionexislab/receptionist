"""WhatsApp lead alerts via Meta's WhatsApp Cloud API.

Every tenant's alerts go out from ONE shared ApollonIA sender number
(WHATSAPP_PHONE_NUMBER_ID). A message we start, rather than a reply inside a
24h window the recipient opened, must be a Meta-approved template, so this
sends the template WHATSAPP_TEMPLATE in the tenant's locale language. It must
be approved (category "utility") in every locale under that same name, with a
body taking exactly two NAMED parameters (WhatsApp Manager's "name" variable
type, which is what the template was created with):

    {{a}} — how to reach the caller: their number, plus " · <e-mail>"
            when they gave one (tenants that collect e-mail)
    {{b}} — the one-line call summary

Named parameters must be sent with their parameter_name, or Meta rejects the
message — so renaming a variable in WhatsApp Manager means changing
_CONTACT_PARAM / _SUMMARY_PARAM below too.

Template parameters may not contain newlines, tabs or runs of more than four
spaces, and the whole body is capped at 1024 characters — so the alert is a
short ping. The full lead stays in the email, which is always sent as well.

Async httpx, matching call/router._send_lead_email.
"""

import logging
import re
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

# Room for the template's own fixed text inside Meta's 1024-char body cap.
_MAX_SUMMARY = 700

# The template's variable names, exactly as defined in WhatsApp Manager.
_CONTACT_PARAM = "a"
_SUMMARY_PARAM = "b"

_SEPARATORS_RE = re.compile(r"[\s().\-/]+")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_number(raw: Optional[str]) -> Optional[str]:
    """A WhatsApp number as "+<country code><number>", or None if it isn't one.

    Only international form is accepted ("+39 333 123 4567" or
    "0039 333 1234567"): an agency may have staff on numbers from more than one
    country, so a bare national number can't be completed safely. "" → "".
    """
    value = _SEPARATORS_RE.sub("", (raw or "").strip())
    if not value:
        return ""
    if value.startswith("00"):
        value = "+" + value[2:]
    if not value.startswith("+"):
        return None
    digits = value[1:]
    # E.164: at most 15 digits, and no country code starts with 0.
    if not digits.isdigit() or not 8 <= len(digits) <= 15 or digits[0] == "0":
        return None
    return "+" + digits


def enabled() -> bool:
    return bool(settings.WHATSAPP_TOKEN and settings.WHATSAPP_PHONE_NUMBER_ID)


def _param(text: str, limit: int) -> str:
    """Flatten text into something Meta accepts as a template parameter."""
    flat = _WHITESPACE_RE.sub(" ", text or "").strip() or "-"
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


async def send_lead_alert(
    to: list[str], contact: str, summary: str, locale: Optional[str]
) -> int:
    """Send the lead template to every number in `to`; return how many went.

    Never raises: a failed alert must not cost the lead, whose email is sent
    independently of this.
    """
    if not enabled() or not to:
        return 0
    url = (
        f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/"
        f"{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    )
    parameters = [
        {
            "type": "text",
            "parameter_name": _CONTACT_PARAM,
            "text": _param(contact, 160),
        },
        {
            "type": "text",
            "parameter_name": _SUMMARY_PARAM,
            "text": _param(summary, _MAX_SUMMARY),
        },
    ]
    sent = 0
    async with httpx.AsyncClient(timeout=30) as client:
        for number in to:
            try:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"},
                    json={
                        "messaging_product": "whatsapp",
                        "to": number.lstrip("+"),
                        "type": "template",
                        "template": {
                            "name": settings.WHATSAPP_TEMPLATE,
                            "language": {"code": locale or "it"},
                            "components": [
                                {"type": "body", "parameters": parameters}
                            ],
                        },
                    },
                )
                response.raise_for_status()
                sent += 1
            except httpx.HTTPStatusError as exc:
                # Meta's error body names the actual problem (template not
                # approved in this language, number not on WhatsApp, …).
                logger.error(
                    "WhatsApp lead alert to %s failed: %s %s",
                    number, exc.response.status_code, exc.response.text[:500],
                )
            except Exception as exc:
                logger.error("WhatsApp lead alert to %s failed: %s", number, exc)
    return sent

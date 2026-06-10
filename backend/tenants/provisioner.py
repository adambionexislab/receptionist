"""Buys a Twilio number for a tenant and points its voice webhook at us."""

import logging

from config import settings
from tenants import db

logger = logging.getLogger(__name__)


class ProvisioningError(Exception):
    pass


def provision_twilio_number(tenant_id: str) -> str:
    """Buy one available US local number, wire its voice webhook to
    /call/inbound, save it on the tenant, and return it in E.164 format.

    Raises ProvisioningError on any failure so the signup endpoint can
    report it instead of silently activating a tenant with no number.
    """
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        raise ProvisioningError("Twilio credentials not configured")
    if not settings.PUBLIC_BASE_URL:
        raise ProvisioningError("PUBLIC_BASE_URL not configured")

    from twilio.rest import Client as TwilioClient

    client = TwilioClient(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)

    try:
        available = client.available_phone_numbers("US").local.list(limit=1)
    except Exception as exc:
        raise ProvisioningError(f"Twilio number search failed: {exc}") from exc
    if not available:
        raise ProvisioningError("No US local numbers available on Twilio")

    candidate = available[0].phone_number
    try:
        purchased = client.incoming_phone_numbers.create(
            phone_number=candidate,
            voice_url=f"{settings.PUBLIC_BASE_URL}/call/inbound",
            voice_method="POST",
        )
    except Exception as exc:
        raise ProvisioningError(f"Twilio number purchase failed: {exc}") from exc

    number = purchased.phone_number
    db.update_twilio_number(tenant_id, number)
    logger.info("Provisioned Twilio number %s for tenant %s", number, tenant_id)
    return number

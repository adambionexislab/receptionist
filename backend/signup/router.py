"""Signup → auto-provisioning.

POST /signup (the landing-page form posts JSON here) now provisions a tenant
end-to-end: DB record, Twilio number, listings scrape, carrier-forwarding
instructions emailed to the agency. The original side effects (signups CSV +
owner notification email) are kept.
"""

import asyncio
import csv
import datetime
import logging
import threading
from pathlib import Path
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from config import settings
from listings.store import tenant_stores
from tenants import db
from tenants.provisioner import ProvisioningError, provision_twilio_number

logger = logging.getLogger(__name__)

router = APIRouter()

_DATA_DIR    = Path(__file__).parent.parent / "data"
_SIGNUPS_CSV = _DATA_DIR / "signups.csv"
_ROME        = ZoneInfo("Europe/Rome")
_csv_lock    = threading.Lock()

_PLAN_PRICES = {"Base": "€145/mese", "Pro": "€395/mese", "Max": "€795/mese"}

# Form values → tenant management_mode
_MODALITA_MAP = {
    "Tutte le chiamate": "tutte",
    "Solo chiamate perse/cancellate": "perse_cancellate",
}


class SignupData(BaseModel):
    studio_name: str
    immobiliare_url: str
    phone: str
    email: str
    plan: Literal["Base", "Pro", "Max"]
    pagamento: Literal["Mensile", "Annuale"]
    modalita: Literal["Tutte le chiamate", "Solo chiamate perse/cancellate"]

    @field_validator("studio_name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Il nome dello studio non può essere vuoto")
        return v

    @field_validator("immobiliare_url")
    @classmethod
    def url_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("L'URL non può essere vuoto")
        if not v.startswith(("http://", "https://")):
            v = "https://" + v
        return v

    @field_validator("phone")
    @classmethod
    def phone_valid(cls, v: str) -> str:
        v = v.strip()
        digits = "".join(c for c in v if c.isdigit())
        if len(digits) < 6:
            raise ValueError("Numero di telefono non valido")
        return v

    @field_validator("email")
    @classmethod
    def email_valid(cls, v: str) -> str:
        v = v.strip()
        if not v or "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Indirizzo email non valido")
        return v


# ─────────────────────────────────────────────────────────────────────────────
# STRIPE HOOK — intentionally a NO-OP for now.
#
# When billing goes live, replace the body of this function with the real
# integration: create a Stripe Customer for tenant["lead_email"], then a
# Subscription for the price matching (tenant["plan"], tenant["billing_period"])
# — e.g. via a Stripe Checkout Session whose URL is returned to the frontend.
# No card data is collected and no live subscription is created today.
# ─────────────────────────────────────────────────────────────────────────────
def create_stripe_subscription(tenant: dict) -> None:
    logger.info(
        "Stripe stub: skipping subscription for tenant %s (plan=%s, billing=%s)",
        tenant["id"], tenant.get("plan"), tenant.get("billing_period"),
    )


def _ussd_code(management_mode: str, twilio_number: str) -> str:
    if management_mode == "tutte":
        return f"**21*{twilio_number}#"
    return f"**004*{twilio_number}**30#"


def _write_csv(data: SignupData) -> None:
    _DATA_DIR.mkdir(exist_ok=True)
    first = not _SIGNUPS_CSV.exists()
    with _csv_lock:
        with open(_SIGNUPS_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if first:
                w.writerow(["timestamp", "studio_name", "immobiliare_url", "phone", "email", "plan", "pagamento", "modalita"])
            w.writerow([
                datetime.datetime.now(_ROME).isoformat(),
                data.studio_name,
                data.immobiliare_url,
                data.phone,
                data.email,
                data.plan,
                data.pagamento,
                data.modalita,
            ])


async def _send_email(to: str, subject: str, body: str) -> None:
    if not settings.RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not configured — email to %s skipped", to)
        return
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
            json={
                "from": settings.RESEND_FROM,
                "to": [to],
                "subject": subject,
                "text": body,
            },
        )
        resp.raise_for_status()


async def _send_welcome_email(data: SignupData, twilio_number: str, ussd_code: str) -> None:
    body = "\n".join([
        f"Gentile {data.studio_name},",
        "",
        "il tuo account ApollonIA è attivo!",
        "",
        f"Il numero Apollonia assegnato a {data.studio_name} è: {twilio_number}",
        "",
        "── Come attivare l'inoltro delle chiamate ──",
        f"Per attivare l'inoltro, digita questo codice dal telefono {data.phone}:",
        "",
        f"    {ussd_code}",
        "",
        "e premi chiama.",
        "",
        "Per disattivare l'inoltro: ##21#  (oppure ##004#)",
        "",
        "Chiama il tuo numero per testare Apollonia.",
        "",
        "A presto,",
        "il team ApollonIA",
    ])
    try:
        await _send_email(
            data.email,
            f"ApollonIA è attiva per {data.studio_name}",
            body,
        )
        logger.info("Welcome email sent to %s", data.email)
    except Exception as exc:
        logger.error("Failed to send welcome email to %s: %s", data.email, exc)


async def _send_notification(data: SignupData, twilio_number: Optional[str], error: Optional[str] = None) -> None:
    """Internal notification to the platform owner (LEAD_EMAIL)."""
    if not settings.LEAD_EMAIL:
        logger.warning("LEAD_EMAIL not configured — signup notification skipped")
        return

    price = _PLAN_PRICES.get(data.plan, "")
    body = "\n".join([
        f"Nuova attivazione ApollonIA\n",
        f"Piano:              {data.plan} ({price})",
        f"Pagamento:          {data.pagamento}",
        f"Studio:             {data.studio_name}",
        f"Email:              {data.email}",
        f"Telefono:           {data.phone}",
        f"URL immobiliare.it: {data.immobiliare_url}",
        f"Modalità:           {data.modalita}",
        f"Numero Twilio:      {twilio_number or 'NON ASSEGNATO'}",
        f"Esito:              {('ERRORE: ' + error) if error else 'attivato automaticamente'}",
        f"\nTimestamp: {datetime.datetime.now(_ROME).strftime('%d/%m/%Y %H:%M')} (Rome)",
    ])

    try:
        await _send_email(
            settings.LEAD_EMAIL,
            f"[ApollonIA] Nuovo signup {data.plan} — {data.studio_name}"
            + (" (PROVISIONING FALLITO)" if error else ""),
            body,
        )
        logger.info("Signup notification sent for studio=%s plan=%s", data.studio_name, data.plan)
    except Exception as exc:
        logger.error("Failed to send signup notification: %s", exc)


@router.post("/signup")
async def create_signup(data: SignupData):
    await asyncio.to_thread(_write_csv, data)

    # 1-2. Create the tenant record.
    tenant = await asyncio.to_thread(
        db.create,
        agency_name=data.studio_name,
        agent_name="Apollonia",
        real_number=data.phone,
        immobiliare_url=data.immobiliare_url,
        lead_email=data.email,
        plan=data.plan,
        billing_period=data.pagamento.lower(),
        management_mode=_MODALITA_MAP[data.modalita],
    )

    # 3. Buy and wire a Twilio number.
    try:
        twilio_number = await asyncio.to_thread(provision_twilio_number, tenant["id"])
    except ProvisioningError as exc:
        logger.error("Provisioning failed for tenant %s: %s", tenant["id"], exc)
        asyncio.create_task(_send_notification(data, None, error=str(exc)))
        return JSONResponse(
            status_code=502,
            content={
                "status": "error",
                "detail": "Attivazione non riuscita. Ti contatteremo a breve per completarla.",
            },
        )

    # 4. Kick off the first listings scrape for this tenant (best-effort).
    asyncio.create_task(tenant_stores.load(tenant["id"], data.immobiliare_url))

    # 5. Carrier call-forwarding code.
    ussd_code = _ussd_code(tenant["management_mode"], twilio_number)

    # 6. Emails: welcome to the agency, notification to the owner.
    asyncio.create_task(_send_welcome_email(data, twilio_number, ussd_code))
    asyncio.create_task(_send_notification(data, twilio_number))

    # 7. Billing hook (no-op for now — see create_stripe_subscription above).
    create_stripe_subscription(tenant)

    # 8.
    return {
        "tenant_id": tenant["id"],
        "twilio_number": twilio_number,
        "status": "active",
        "ussd_code": ussd_code,
    }

"""Stripe Checkout integration.

POST /create-checkout-session — the pricing modal posts the selected plan's
Stripe price_id and the customer's email; we open a Stripe Checkout Session
(subscription mode) and return its hosted-page URL for the browser to redirect
to. POST /webhook — Stripe calls this after payment; we verify the signature
and log completed checkouts so the team can provision the number manually.

The Stripe secret key and webhook secret are read from the environment and are
never sent to the browser. Only price IDs (which are not secret) reach the JS.
"""

import datetime
import logging
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
import stripe
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

_ROME = ZoneInfo("Europe/Rome")

# price_id → human-readable plan label. Doubles as an allowlist: only these six
# prices may be checked out, so a tampered request can't substitute an arbitrary
# (e.g. €0) price. The label is stored on the session so the webhook can log it.
# NOTE: live-mode price IDs (require sk_live_ keys + live webhook secret).
# Test-mode IDs are recorded in project memory for switching back.
_PRICE_TO_PLAN = {
    "price_1TgQYV17IdyiO0xX72ug1Pig": "Base (Mensile)",
    "price_1TgQVJ17IdyiO0xXUcMAkGrW": "Pro (Mensile)",
    "price_1TgQaJ17IdyiO0xXBB4Dl52v": "Max (Mensile)",
    "price_1Tfofo17IdyiO0xXUnRm0P96": "Base (Annuale)",
    "price_1Tfoif17IdyiO0xXewui5ZYo": "Pro (Annuale)",
    "price_1TgQbj17IdyiO0xX6lzHNXk6": "Max (Annuale)",
}

# Which market the checkout started from ("it" = homepage, "sk" = /sk/ page).
# It drives the Stripe Checkout UI language and where the cancel button returns.
# Anything else falls back to Italian. The same six prices serve both markets.
_ALLOWED_LOCALES = {"it", "sk"}


class CheckoutRequest(BaseModel):
    price_id: str
    customer_email: str
    # Optional context captured by the form. Forwarded to Stripe as metadata so
    # the team has everything needed to provision the number after payment.
    studio_name: Optional[str] = None
    immobiliare_url: Optional[str] = None
    phone: Optional[str] = None
    plan: Optional[str] = None
    pagamento: Optional[str] = None
    modalita: Optional[str] = None
    # Originating market ("it" homepage or "sk" /sk/ page). Sets the Stripe
    # Checkout UI language and the cancel-return page; validated server-side.
    locale: Optional[str] = "it"


@router.post("/create-checkout-session")
async def create_checkout_session(data: CheckoutRequest):
    if not settings.STRIPE_SECRET_KEY:
        logger.error("STRIPE_SECRET_KEY not configured — cannot create checkout session")
        raise HTTPException(status_code=503, detail="Pagamenti non disponibili al momento.")

    if data.price_id not in _PRICE_TO_PLAN:
        raise HTTPException(status_code=400, detail="Piano non valido.")

    stripe.api_key = settings.STRIPE_SECRET_KEY
    base = settings.PUBLIC_BASE_URL.rstrip("/")

    checkout_locale = data.locale if data.locale in _ALLOWED_LOCALES else "it"
    # Return a cancelled Slovak checkout to /sk/, everything else to the homepage.
    cancel_path = "/sk/" if checkout_locale == "sk" else "/"

    metadata = {
        "plan_label": _PRICE_TO_PLAN[data.price_id],
        "studio_name": data.studio_name or "",
        "immobiliare_url": data.immobiliare_url or "",
        "phone": data.phone or "",
        "plan": data.plan or "",
        "pagamento": data.pagamento or "",
        "modalita": data.modalita or "",
    }

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            # No payment_method_types: let Stripe pick eligible methods dynamically.
            line_items=[{"price": data.price_id, "quantity": 1}],
            customer_email=data.customer_email,
            # Show Stripe's hosted checkout in the market's language.
            locale=checkout_locale,
            success_url=f"{base}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}{cancel_path}",
            metadata=metadata,
            subscription_data={"metadata": metadata},
        )
    except stripe.error.StripeError as exc:
        logger.error("Stripe checkout session creation failed: %s", exc)
        raise HTTPException(status_code=502, detail="Errore nella creazione del pagamento.")

    return {"url": session.url}


async def _send_email(to: str, subject: str, body: str) -> None:
    """Send a plain-text email via Resend's HTTP API (Render blocks SMTP)."""
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


async def _send_signup_notification(session: dict, meta: dict, email: Optional[str]) -> None:
    """Notify the platform owner (LEAD_EMAIL) that a checkout completed."""
    if not settings.LEAD_EMAIL:
        logger.warning("LEAD_EMAIL not configured — payment notification skipped")
        return

    plan = meta.get("plan_label") or meta.get("plan") or "?"
    studio = meta.get("studio_name") or "—"
    body = "\n".join([
        "Nuovo pagamento ApollonIA\n",
        f"Piano:              {plan}",
        f"Email cliente:      {email or '—'}",
        f"Studio:             {studio}",
        f"Telefono:           {meta.get('phone') or '—'}",
        f"URL immobiliare.it: {meta.get('immobiliare_url') or '—'}",
        f"Modalità:           {meta.get('modalita') or '—'}",
        f"Stripe session:     {session.get('id')}",
        f"Stripe customer:    {session.get('customer') or '—'}",
        f"Subscription:       {session.get('subscription') or '—'}",
        f"\nTimestamp: {datetime.datetime.now(_ROME).strftime('%d/%m/%Y %H:%M')} (Rome)",
        "\nContatta lo studio entro 24 ore per configurare il numero Apollonia.",
    ])
    try:
        await _send_email(
            settings.LEAD_EMAIL,
            f"[ApollonIA] Nuovo pagamento {plan} — {studio}",
            body,
        )
        logger.info("Payment notification sent for studio=%s plan=%s", studio, plan)
    except Exception as exc:
        logger.error("Failed to send payment notification: %s", exc)


async def _send_customer_confirmation(meta: dict, email: Optional[str]) -> None:
    """Confirm the payment to the customer who just checked out."""
    if not email:
        logger.warning("No customer email on session — confirmation skipped")
        return

    studio = meta.get("studio_name")
    greeting = f"Dear {studio}," if studio else "Hello,"
    # Use the language-neutral plan tier ("Base"/"Pro"/"Max"), not the Italian
    # plan_label, so this English email doesn't embed "(Mensile)".
    plan = meta.get("plan") or ""
    body = "\n".join([
        greeting,
        "",
        "thank you! We've received your payment" + (f" for the {plan} plan." if plan else "."),
        "",
        "Our team will contact you within 24 hours to set up your Apollonia "
        "number and activate call forwarding.",
        "",
        "If you have any questions, just reply to this email.",
        "",
        "Best regards,",
        "The ApollonIA team",
    ])
    try:
        await _send_email(email, "ApollonIA — payment confirmed", body)
        logger.info("Customer confirmation sent to %s", email)
    except Exception as exc:
        logger.error("Failed to send customer confirmation to %s: %s", email, exc)


@router.post("/webhook")
async def stripe_webhook(request: Request):
    if not settings.STRIPE_WEBHOOK_SECRET:
        logger.error("STRIPE_WEBHOOK_SECRET not configured — rejecting webhook")
        raise HTTPException(status_code=503, detail="Webhook not configured")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        logger.warning("Stripe webhook: invalid payload")
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        logger.warning("Stripe webhook: invalid signature")
        raise HTTPException(status_code=400, detail="Invalid signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        meta = session.get("metadata") or {}
        email = (
            session.get("customer_email")
            or (session.get("customer_details") or {}).get("email")
        )
        plan = meta.get("plan_label") or meta.get("plan") or "?"
        logger.info(
            "checkout.session.completed — email=%s plan=%s studio=%s phone=%s",
            email, plan, meta.get("studio_name"), meta.get("phone"),
        )
        await _send_signup_notification(session, meta, email)
        await _send_customer_confirmation(meta, email)

    return {"status": "ok"}

"""Stripe Checkout integration.

POST /create-checkout-session — the pricing modal posts the selected plan's
Stripe price_id and the customer's email; we open a Stripe Checkout Session
(subscription mode) and return its hosted-page URL for the browser to redirect
to. POST /webhook — Stripe calls this after payment; we verify the signature
and log completed checkouts so the team can provision the number manually.

The Stripe secret key and webhook secret are read from the environment and are
never sent to the browser. Only price IDs (which are not secret) reach the JS.
"""

import logging
from typing import Optional

import stripe
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

# price_id → human-readable plan label. Doubles as an allowlist: only these six
# prices may be checked out, so a tampered request can't substitute an arbitrary
# (e.g. €0) price. The label is stored on the session so the webhook can log it.
# NOTE: test-mode price IDs (work with sk_test_ keys). Swap to the live-mode
# IDs when going live with sk_live_ keys.
_PRICE_TO_PLAN = {
    "price_1TiAhL17IdyiO0xXtrRZmfxq": "Base (Mensile)",
    "price_1TiAhQ17IdyiO0xXJmdu5JdY": "Pro (Mensile)",
    "price_1TiAhT17IdyiO0xXXoLEgZBb": "Max (Mensile)",
    "price_1TiAhN17IdyiO0xXeK6a3Sdu": "Base (Annuale)",
    "price_1TiAhR17IdyiO0xXPZPN9SaP": "Pro (Annuale)",
    "price_1TiAhV17IdyiO0xXKCIUZrCe": "Max (Annuale)",
}


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


@router.post("/create-checkout-session")
async def create_checkout_session(data: CheckoutRequest):
    if not settings.STRIPE_SECRET_KEY:
        logger.error("STRIPE_SECRET_KEY not configured — cannot create checkout session")
        raise HTTPException(status_code=503, detail="Pagamenti non disponibili al momento.")

    if data.price_id not in _PRICE_TO_PLAN:
        raise HTTPException(status_code=400, detail="Piano non valido.")

    stripe.api_key = settings.STRIPE_SECRET_KEY
    base = settings.PUBLIC_BASE_URL.rstrip("/")

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
            success_url=f"{base}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/",
            metadata=metadata,
            subscription_data={"metadata": metadata},
        )
    except stripe.error.StripeError as exc:
        logger.error("Stripe checkout session creation failed: %s", exc)
        raise HTTPException(status_code=502, detail="Errore nella creazione del pagamento.")

    return {"url": session.url}


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

    return {"status": "ok"}

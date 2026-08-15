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
    phone: Optional[str] = None
    plan: Optional[str] = None
    pagamento: Optional[str] = None
    # Originating market ("it" homepage or "sk" /sk/ page). Sets the Stripe
    # Checkout UI language and the cancel-return page; validated server-side.
    locale: Optional[str] = "it"
    # Set by the terms checkbox on the /sk/ signup modal. Recorded on the
    # session so there is a record of the acceptance; the Italian page has no
    # terms document yet and simply omits it.
    terms_accepted: Optional[bool] = False


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
        "phone": data.phone or "",
        "plan": data.plan or "",
        "pagamento": data.pagamento or "",
        # Carried through to the webhook so the customer confirmation email
        # can be sent in the same language as the checkout page.
        "locale": checkout_locale,
        # Which terms were accepted and when. The timestamp is taken here rather
        # than in the browser so it cannot be spoofed by the client.
        "terms_accepted": (
            f"ApollonIA-VOP-SK.pdf @ {datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}"
            if data.terms_accepted
            else ""
        ),
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
            # Collect company billing details for invoicing: full billing address
            # (incl. company name field) and an optional VAT/tax ID.
            # NOTE: customer_creation is a payment-mode-only param — subscription
            # mode always creates a Customer, so it's omitted here.
            billing_address_collection="required",
            tax_id_collection={"enabled": True},
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
    payload = {
        "from": settings.RESEND_FROM,
        "to": [to],
        "subject": subject,
        "text": body,
    }
    # Route replies to a real inbox (e.g. info@apollon-ia.com) when configured,
    # so customers replying to a payment email reach a human, not RESEND_FROM
    # (which may be an unmonitored sending address).
    if settings.OUTREACH_REPLY_TO:
        payload["reply_to"] = settings.OUTREACH_REPLY_TO
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
            json=payload,
        )
        resp.raise_for_status()


# Column width for the "Label:              value" rows of the notification email.
_LABEL_WIDTH = 20

_INTERVAL_LABELS = {"day": "giorno", "week": "settimana", "month": "mese", "year": "anno"}


def _row(label: str, *values) -> list[str]:
    """One aligned "Label: value" line; empty values collapse to an em dash.

    Passing several values (e.g. the lines of an address) prints one per line,
    the continuation lines indented to sit under the first.
    """
    lines = [str(v) for v in values if v not in (None, "")]
    if not lines:
        lines = ["—"]
    head = f"{label + ':':<{_LABEL_WIDTH}}{lines[0]}"
    return [head] + [f"{'':<{_LABEL_WIDTH}}{line}" for line in lines[1:]]


def _money(amount: Optional[int], currency: Optional[str]) -> Optional[str]:
    """Format a Stripe minor-unit amount (14900 → "149.00 EUR").

    Both markets bill in EUR, so the 100 divisor is safe here; it would be
    wrong for a zero-decimal currency (JPY) if we ever sold in one.
    """
    if amount is None:
        return None
    return f"{amount / 100:,.2f} {(currency or '').upper()}".strip()


def _id_of(value) -> Optional[str]:
    """Stripe returns a related object as an ID string unless it was expanded."""
    if isinstance(value, dict):
        return value.get("id")
    return value


def _address_lines(address: Optional[dict]) -> list[str]:
    """Flatten a Stripe address into printable lines (empty if not collected)."""
    if not address:
        return []
    street = " ".join(p for p in (address.get("line1"), address.get("line2")) if p)
    town = " ".join(
        p for p in (address.get("postal_code"), address.get("city"), address.get("state")) if p
    )
    return [line for line in (street, town, address.get("country")) if line]


def _tax_id_line(details: dict) -> Optional[str]:
    """The VAT/tax IDs collected by tax_id_collection, e.g. "eu_vat IT01234567890"."""
    ids = details.get("tax_ids") or []
    parts = [
        f"{t.get('type') or '?'} {t.get('value')}" for t in ids if t.get("value")
    ]
    return ", ".join(parts) or None


def _line_item_lines(session: dict) -> list[str]:
    """Describe what was bought — only present when the session was expanded."""
    items = ((session.get("line_items") or {}).get("data")) or []
    lines = []
    for item in items:
        price = item.get("price") or {}
        interval = (price.get("recurring") or {}).get("interval")
        text = f"{item.get('description') or '—'} × {item.get('quantity') or 1}"
        amount = _money(item.get("amount_total"), item.get("currency") or session.get("currency"))
        if amount:
            text += f" — {amount}"
        if interval:
            text += f" / {_INTERVAL_LABELS.get(interval, interval)}"
        lines.append(text)
    return lines


def _renewal_date(subscription) -> Optional[str]:
    """Next billing date, when the subscription came back expanded.

    Stripe's 2025-03-31 API version moved current_period_end off the
    subscription onto its items; no api_version is pinned, so read both.
    """
    if not isinstance(subscription, dict):
        return None
    end = subscription.get("current_period_end")
    if not end:
        ends = [
            item.get("current_period_end")
            for item in (subscription.get("items") or {}).get("data") or []
            if item.get("current_period_end")
        ]
        end = min(ends) if ends else None
    if not end:
        return None
    return datetime.datetime.fromtimestamp(end, _ROME).strftime("%d/%m/%Y")


async def _send_signup_notification(session: dict, meta: dict, email: Optional[str]) -> None:
    """Notify the platform owner (LEAD_EMAIL) that a checkout completed.

    Reports everything the Checkout Session carries — billing details and VAT
    ID collected on the hosted page, the amounts actually charged, and the
    Stripe object IDs — so the number can be provisioned and invoiced without
    opening the Dashboard.
    """
    if not settings.LEAD_EMAIL:
        logger.warning("LEAD_EMAIL not configured — payment notification skipped")
        return

    plan = meta.get("plan_label") or meta.get("plan") or "?"
    studio = meta.get("studio_name") or "—"
    details = session.get("customer_details") or {}
    totals = session.get("total_details") or {}
    currency = session.get("currency")

    body_lines = ["Nuovo pagamento ApollonIA", "", "— PIANO —"]
    body_lines += _row("Piano", plan)
    body_lines += _row("Articoli", *_line_item_lines(session))
    body_lines += _row("Subtotale", _money(session.get("amount_subtotal"), currency))
    # Only worth a line when non-zero — a "0.00 EUR" discount row is noise.
    if totals.get("amount_discount"):
        body_lines += _row("Sconto", f"-{_money(totals['amount_discount'], currency)}")
    if totals.get("amount_tax"):
        body_lines += _row("IVA", _money(totals["amount_tax"], currency))
    body_lines += _row("Totale pagato", _money(session.get("amount_total"), currency))
    body_lines += _row("Stato pagamento", session.get("payment_status"))
    body_lines += _row("Prossimo rinnovo", _renewal_date(session.get("subscription")))

    body_lines += ["", "— CLIENTE (da Stripe) —"]
    body_lines += _row("Email", email)
    body_lines += _row("Intestatario", details.get("name"))
    body_lines += _row("Telefono", details.get("phone"))
    body_lines += _row("Partita IVA", _tax_id_line(details))
    # "reverse" means the EU VAT ID shifted the tax liability to the customer.
    if (details.get("tax_exempt") or "none") != "none":
        body_lines += _row("Regime IVA", details.get("tax_exempt"))
    body_lines += _row("Indirizzo", *_address_lines(details.get("address")))

    body_lines += ["", "— DAL FORM —"]
    body_lines += _row("Studio", studio)
    body_lines += _row("Telefono", meta.get("phone"))
    body_lines += _row("Piano scelto", meta.get("plan"))
    body_lines += _row("Pagamento", meta.get("pagamento"))
    body_lines += _row("Lingua", meta.get("locale"))

    body_lines += ["", "— STRIPE —"]
    body_lines += _row("Session", session.get("id"))
    body_lines += _row("Customer", _id_of(session.get("customer")))
    body_lines += _row("Subscription", _id_of(session.get("subscription")))
    body_lines += _row("Invoice", _id_of(session.get("invoice")))

    body_lines += [
        "",
        f"Timestamp: {datetime.datetime.now(_ROME).strftime('%d/%m/%Y %H:%M')} (Rome)",
        "",
        "Contatta lo studio entro 24 ore per configurare il numero Apollonia.",
    ]
    body = "\n".join(body_lines)
    try:
        await _send_email(
            settings.LEAD_EMAIL,
            f"[ApollonIA] Nuovo pagamento {plan} — {studio}",
            body,
        )
        logger.info("Payment notification sent for studio=%s plan=%s", studio, plan)
    except Exception as exc:
        logger.error("Failed to send payment notification: %s", exc)


_CONFIRMATION_COPY = {
    "it": {
        "subject": "ApollonIA — pagamento confermato",
        "greeting_named": "Ciao {studio},",
        "greeting_default": "Ciao,",
        "thanks_with_plan": "grazie! Abbiamo ricevuto il tuo pagamento per il piano {plan}.",
        "thanks_no_plan": "grazie! Abbiamo ricevuto il tuo pagamento.",
        "followup": (
            "Il nostro team ti contatterà entro 24 ore per configurare il "
            "numero Apollonia e attivare l'inoltro delle chiamate."
        ),
        "reply": "Per qualsiasi domanda, rispondi pure a questa email.",
        "signoff": "A presto,",
        "team": "Il team ApollonIA",
    },
    "sk": {
        "subject": "ApollonIA — platba potvrdená",
        "greeting_named": "Dobrý deň, {studio},",
        "greeting_default": "Dobrý deň,",
        "thanks_with_plan": "ďakujeme! Prijali sme vašu platbu za program {plan}.",
        "thanks_no_plan": "ďakujeme! Prijali sme vašu platbu.",
        "followup": (
            "Náš tím vás bude do 24 hodín kontaktovať, aby nastavil vaše "
            "číslo Apollonia a aktivoval presmerovanie hovorov."
        ),
        "reply": "Ak máte akékoľvek otázky, jednoducho odpovedzte na tento e-mail.",
        "signoff": "S pozdravom,",
        "team": "Tím ApollonIA",
    },
}


async def _send_customer_confirmation(meta: dict, email: Optional[str]) -> None:
    """Confirm the payment to the customer who just checked out, in the
    language of the checkout page they used (falls back to Italian for
    older sessions created before locale was stored in metadata)."""
    if not email:
        logger.warning("No customer email on session — confirmation skipped")
        return

    locale = meta.get("locale")
    copy = _CONFIRMATION_COPY.get(locale, _CONFIRMATION_COPY["it"])

    studio = meta.get("studio_name")
    greeting = copy["greeting_named"].format(studio=studio) if studio else copy["greeting_default"]
    # Use the language-neutral plan tier ("Base"/"Pro"/"Max"), not the Italian
    # plan_label, so this email doesn't embed "(Mensile)"/"(Annuale)".
    plan = meta.get("plan") or ""
    thanks = copy["thanks_with_plan"].format(plan=plan) if plan else copy["thanks_no_plan"]
    body = "\n".join([
        greeting,
        "",
        thanks,
        "",
        copy["followup"],
        "",
        copy["reply"],
        "",
        copy["signoff"],
        copy["team"],
    ])
    try:
        await _send_email(email, copy["subject"], body)
        logger.info("Customer confirmation sent to %s (locale=%s)", email, locale)
    except Exception as exc:
        logger.error("Failed to send customer confirmation to %s: %s", email, exc)


def _expand_session(session: dict) -> dict:
    """Re-fetch the session with the parts Stripe leaves out of webhook events.

    Webhook payloads carry no line_items at all and reference the subscription
    by ID, so the purchased items and the renewal date need an explicit
    retrieve. A failure here must not cost us the notification: fall back to
    the event's own copy, which still has the billing details and the totals.
    """
    if not settings.STRIPE_SECRET_KEY:
        return session
    try:
        stripe.api_key = settings.STRIPE_SECRET_KEY
        return stripe.checkout.Session.retrieve(
            session["id"], expand=["line_items", "subscription"]
        )
    except Exception as exc:
        logger.warning("Could not expand checkout session %s: %s", session.get("id"), exc)
        return session


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
        session = _expand_session(event["data"]["object"])
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

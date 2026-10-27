"""Querie trial checkout and Stripe events.

Querie shares the Stripe account — and therefore the one webhook endpoint,
POST /webhook in billing/router.py — with ApollonIA. Every Querie object
carries metadata product=querie; billing/router.py hands those events here
before its own handling, so a Querie trial never triggers ApollonIA's
"payment received, we'll set up your number" emails.

The trial: Stripe Checkout in subscription mode, card required, TRIAL_DAYS
free days, first charge on day 15. The founder coupon is attached only while
offer.current_phase() says "founder" — decided here at checkout time, never
by the page.
"""

import datetime
import hashlib
import hmac
import logging
from typing import Optional

import stripe

from config import settings
from querie import config as qc
from querie import db, notify, offer

logger = logging.getLogger(__name__)

PRODUCT_TAG = "querie"


def _plain(obj) -> dict:
    """StripeObject stopped subclassing dict in newer stripe-python."""
    if obj is None:
        return {}
    return obj if isinstance(obj, dict) else obj.to_dict()


def _id_of(value) -> Optional[str]:
    if isinstance(value, dict):
        return value.get("id")
    return getattr(value, "id", value)


def is_querie(obj) -> bool:
    return (_plain(obj).get("metadata") or {}).get("product") == PRODUCT_TAG


# ── one-click cancel links ───────────────────────────────────────────────────
def _signing_key() -> Optional[bytes]:
    key = settings.SESSION_SECRET or settings.ADMIN_TOKEN
    return key.encode() if key else None


def _sig(subscription_id: str, key: bytes) -> str:
    msg = f"querie-manage:{subscription_id}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]


def manage_token(subscription_id: str) -> Optional[str]:
    key = _signing_key()
    if not key or not subscription_id:
        return None
    return f"{subscription_id}.{_sig(subscription_id, key)}"


def verify_token(token: str) -> Optional[str]:
    """The subscription ID a token was issued for, or None if forged."""
    key = _signing_key()
    if not key or not token or "." not in token:
        return None
    subscription_id, sig = token.rsplit(".", 1)
    if not subscription_id.startswith("sub_"):
        return None
    if not hmac.compare_digest(sig, _sig(subscription_id, key)):
        return None
    return subscription_id


def cancel_url(subscription_id: Optional[str]) -> Optional[str]:
    token = manage_token(subscription_id or "")
    if not token:
        return None
    return f"{qc.SITE_URL}{qc.BASE_PATH}/zrusit?t={token}"


def portal_url(subscription_id: str, cancel: bool) -> str:
    """A fresh Stripe Customer Portal session. With cancel=True it opens
    straight on the cancellation screen for this subscription, so the email
    link really is one click. Needs cancellation enabled in the portal
    settings (Stripe Dashboard → Settings → Billing → Customer portal)."""
    stripe.api_key = settings.STRIPE_SECRET_KEY
    sub = _plain(stripe.Subscription.retrieve(subscription_id))
    params = {
        "customer": _id_of(sub.get("customer")),
        "return_url": f"{qc.SITE_URL}{qc.BASE_PATH}/",
        "locale": "sk",
    }
    if cancel and sub.get("status") in ("trialing", "active", "past_due"):
        try:
            return stripe.billing_portal.Session.create(
                **params,
                flow_data={
                    "type": "subscription_cancel",
                    "subscription_cancel": {"subscription": subscription_id},
                },
            ).url
        except stripe.error.StripeError as exc:
            # e.g. cancellation not enabled in the portal config — the plain
            # portal still lets them cancel, one screen further in.
            logger.warning("Querie cancel flow unavailable for %s: %s", subscription_id, exc)
    return stripe.billing_portal.Session.create(**params).url


# ── checkout ─────────────────────────────────────────────────────────────────
def create_checkout(
    *, email: str, name: str, phone: str, company: Optional[str], annual: bool = False,
) -> tuple[str, str, bool]:
    """Open a trial Checkout Session. Returns (session_id, url, founder)."""
    # The founder coupon is "25% off 3 months" — on a yearly invoice that would
    # be 25% off the whole year, so yearly billing never gets it.
    founder_phase = offer.current_phase() == "founder" and not annual
    founder = founder_phase and bool(settings.QUERIE_FOUNDER_COUPON_ID)
    if founder_phase and not founder:
        logger.error("Querie founder phase but QUERIE_FOUNDER_COUPON_ID unset — full price checkout")

    metadata = {
        "product": PRODUCT_TAG,
        "name": name[:200],
        "phone": phone[:50],
        "company": (company or "")[:200],
        "email": email[:200],
        "founder": "1" if founder else "0",
        "billing": "annual" if annual else "monthly",
        # Which VOP/privacy versions were accepted, and when — server time, not the browser's.
        "terms_accepted": f"{qc.CONSENT_VERSION} @ {offer.now().isoformat(timespec='seconds')}",
    }
    subscription_data = {
        "trial_period_days": qc.TRIAL_DAYS,
        # A trial that somehow ends without a card is cancelled, never billed.
        "trial_settings": {"end_behavior": {"missing_payment_method": "cancel"}},
        "metadata": metadata,
    }
    if settings.QUERIE_STRIPE_TAX_RATE_ID:
        subscription_data["default_tax_rates"] = [settings.QUERIE_STRIPE_TAX_RATE_ID]

    first = offer.date_sk(offer.first_charge_date())
    params = dict(
        mode="subscription",
        line_items=[{
            "price": settings.QUERIE_STRIPE_ANNUAL_PRICE_ID if annual else settings.QUERIE_STRIPE_PRICE_ID,
            "quantity": 1,
            # Price is per user; the seat count is picked on the Stripe page.
            "adjustable_quantity": {"enabled": True, "minimum": 1, "maximum": qc.MAX_USERS},
        }],
        customer_email=email,
        locale="sk",
        payment_method_collection="always",
        subscription_data=subscription_data,
        metadata=metadata,
        success_url=f"{qc.SITE_URL}{qc.BASE_PATH}/vitajte?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{qc.SITE_URL}{qc.BASE_PATH}/#registracia",
        billing_address_collection="required",
        tax_id_collection={"enabled": True},
        # The same promise as next to the form on our page, right on the button.
        custom_text={"submit": {"message": (
            f"Dnes neplatíte nič. Prvá platba {first}, ak predtým nezrušíte. "
            f"Pripomenieme sa {qc.REMINDER_DAYS_BEFORE} dni pred prvou platbou. "
            "Zrušíte kedykoľvek."
        )}},
    )
    if founder:
        params["discounts"] = [{"coupon": settings.QUERIE_FOUNDER_COUPON_ID}]

    stripe.api_key = settings.STRIPE_SECRET_KEY
    session = stripe.checkout.Session.create(**params)
    return session.id, session.url, founder


# ── webhook events ───────────────────────────────────────────────────────────
def _trial_facts(sub: dict) -> tuple[datetime.date, int]:
    """(first charge date, seat count) from a subscription."""
    trial_end = sub.get("trial_end")
    first = (
        datetime.datetime.fromtimestamp(trial_end, qc.TZ).date()
        if trial_end else offer.first_charge_date()
    )
    items = (sub.get("items") or {}).get("data") or []
    quantity = sum(int(i.get("quantity") or 1) for i in items) or 1
    return first, quantity


async def handle_event(event_type: str, obj) -> None:
    obj = _plain(obj)
    if event_type == "checkout.session.completed":
        await _on_checkout_completed(obj)
    elif event_type == "customer.subscription.trial_will_end":
        await _on_trial_will_end(obj)
    else:
        logger.info("Querie Stripe event %s ignored", event_type)


async def _on_checkout_completed(session: dict) -> None:
    meta = session.get("metadata") or {}
    email = session.get("customer_email") or (session.get("customer_details") or {}).get("email") or meta.get("email")
    subscription_id = _id_of(session.get("subscription"))
    customer_id = _id_of(session.get("customer"))
    founder = meta.get("founder") == "1"
    annual = meta.get("billing") == "annual"

    if not db.mark_trial_started(session.get("id"), email, customer_id, subscription_id):
        logger.warning("Querie checkout %s matched no sign-up row (email=%s)", session.get("id"), email)

    sub: dict = {}
    if subscription_id and settings.STRIPE_SECRET_KEY:
        try:
            stripe.api_key = settings.STRIPE_SECRET_KEY
            sub = _plain(stripe.Subscription.retrieve(subscription_id))
        except Exception as exc:
            logger.warning("Could not fetch Querie subscription %s: %s", subscription_id, exc)
    first, quantity = _trial_facts(sub)

    logger.info("Querie trial started — email=%s seats=%s founder=%s annual=%s", email, quantity, founder, annual)
    await notify.notify_owner_trial(
        name=meta.get("name"), email=email, phone=meta.get("phone"), company=meta.get("company"),
        quantity=quantity, founder=founder, annual=annual, first_charge=first, subscription_id=subscription_id,
    )
    await notify.send_trial_welcome(
        to=email, name=meta.get("name"), quantity=quantity, founder=founder, annual=annual,
        first_charge=first, cancel_url=cancel_url(subscription_id),
    )


async def _on_trial_will_end(sub: dict) -> None:
    """Stripe fires this 3 days before the trial ends — our promised reminder."""
    if sub.get("status") != "trialing" or sub.get("cancel_at_period_end"):
        logger.info("Querie trial %s already cancelled — no reminder", sub.get("id"))
        return
    meta = sub.get("metadata") or {}
    email = meta.get("email")
    customer_id = _id_of(sub.get("customer"))
    if customer_id and settings.STRIPE_SECRET_KEY:
        try:
            stripe.api_key = settings.STRIPE_SECRET_KEY
            email = _plain(stripe.Customer.retrieve(customer_id)).get("email") or email
        except Exception as exc:
            logger.warning("Could not fetch Querie customer %s: %s", customer_id, exc)
    first, quantity = _trial_facts(sub)
    await notify.send_trial_reminder(
        to=email, name=meta.get("name"), quantity=quantity, founder=meta.get("founder") == "1",
        annual=meta.get("billing") == "annual", first_charge=first, cancel_url=cancel_url(sub.get("id")),
    )

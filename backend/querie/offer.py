"""Which state the Querie rollout is in, and the prices and dates that follow.

Three phases, by Bratislava time (see querie/config.py for the dates):

  waitlist  before launch — sign up to reserve the founder price, no card
  founder   launch → founder deadline — trial checkout + 25% for 3 months
  trial     after the deadline — trial checkout, founder offer gone for good

The page, the checkout endpoint and the webhook all ask this module, so the
founder coupon can never be applied after the deadline even if a stale page
is still open in someone's browser.
"""

import datetime
import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from config import settings
from querie import config as qc

logger = logging.getLogger(__name__)

PHASES = ("waitlist", "founder", "trial")

_WEEKDAYS = ("pondelok", "utorok", "streda", "štvrtok", "piatok", "sobota", "nedeľa")
# Accusative, for "v pondelok" / "v stredu".
_WEEKDAYS_ACC = ("pondelok", "utorok", "stredu", "štvrtok", "piatok", "sobotu", "nedeľu")


def now() -> datetime.datetime:
    return datetime.datetime.now(qc.TZ)


def checkout_ready() -> bool:
    """Trial checkout needs a Stripe key and the Querie price."""
    return bool(settings.STRIPE_SECRET_KEY and settings.QUERIE_STRIPE_PRICE_ID)


def current_phase(at: Optional[datetime.datetime] = None) -> str:
    at = at or now()
    override = (settings.QUERIE_PHASE or "auto").strip().lower()
    if override in PHASES:
        phase = override
    elif at < qc.LAUNCH_AT:
        phase = "waitlist"
    elif at < qc.FOUNDER_ENDS_AT:
        phase = "founder"
    else:
        phase = "trial"
    # Never show a checkout that cannot work: without Stripe configured the
    # page keeps collecting sign-ups instead.
    if phase != "waitlist" and not checkout_ready():
        logger.warning("Querie phase %s but Stripe price/key not configured — showing waitlist", phase)
        return "waitlist"
    return phase


def founder_open(at: Optional[datetime.datetime] = None) -> bool:
    """Is the founder offer still on the table (waitlist reserves it, founder
    phase grants it)? False forever once the deadline has passed."""
    at = at or now()
    override = (settings.QUERIE_PHASE or "auto").strip().lower()
    if override == "trial":
        return False
    if override == "founder":
        return True
    return at < qc.FOUNDER_ENDS_AT


def founder_price() -> Decimal:
    price = qc.PRICE_EUR * (100 - qc.FOUNDER_DISCOUNT_PERCENT) / 100
    return price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def eur(amount: Decimal) -> str:
    """Slovak money format: 139 → "139 €", 104.25 → "104,25 €" (non-breaking space)."""
    amount = Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount == amount.to_integral_value():
        text = f"{int(amount):,}".replace(",", " ")
    else:
        text = f"{amount:,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} €"


def date_sk(value: datetime.date | datetime.datetime) -> str:
    """23. 11. 2026"""
    return f"{value.day}. {value.month}. {value.year}"


def weekday_sk(value: datetime.date | datetime.datetime) -> str:
    return _WEEKDAYS[value.weekday()]


def weekday_acc_sk(value: datetime.date | datetime.datetime) -> str:
    return _WEEKDAYS_ACC[value.weekday()]


def first_charge_date(start: Optional[datetime.datetime] = None) -> datetime.date:
    """Day 15: the trial runs TRIAL_DAYS days from sign-up."""
    start = start or now()
    return (start + datetime.timedelta(days=qc.TRIAL_DAYS)).date()


def founder_deadline_display() -> datetime.datetime:
    """The last minute of the offer, for copy ("15. 11. 2026 o 23:59")."""
    return qc.FOUNDER_ENDS_AT - datetime.timedelta(minutes=1)

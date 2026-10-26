"""Querie product constants — everything marketing may want to change.

PRODUCT_NAME is the one place the product is named: the landing page, the
emails and the Stripe checkout all read it from here. Environment-specific
values (pixel IDs, Stripe IDs) are in the main config.py as QUERIE_* settings.
"""

import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from config import settings

PRODUCT_NAME = "Querie"

# Where the page lives on the existing ApollonIA domain.
BASE_PATH = "/querie"
SITE_URL = "https://www.apollon-ia.com"

# ── Rollout ──────────────────────────────────────────────────────────────────
TZ = ZoneInfo("Europe/Bratislava")
# Waitlist until this moment, trial checkout from it on.
LAUNCH_AT = datetime.datetime(2026, 11, 9, 0, 0, tzinfo=TZ)
# Founder offer is open while now < FOUNDER_ENDS_AT, i.e. through Sun 15 Nov
# 23:59:59. One-time: once it passes it disappears from the page for good.
FOUNDER_ENDS_AT = datetime.datetime(2026, 11, 16, 0, 0, tzinfo=TZ)

# ── Pricing (EUR, VAT excluded — always shown as "bez DPH") ──────────────────
PRICE_EUR = Decimal("139.00")
FOUNDER_DISCOUNT_PERCENT = 25
FOUNDER_MONTHS = 3
TRIAL_DAYS = 14
# "Forgot to cancel? We refund your first payment within 7 days."
REFUND_WINDOW_DAYS = 7
# Fixed by Stripe: customer.subscription.trial_will_end fires 3 days before the
# trial ends, and that event is what sends our reminder. Descriptive only.
REMINDER_DAYS_BEFORE = 3
# Upper bound for the seat picker on Stripe Checkout.
MAX_USERS = 50

# ── Company ──────────────────────────────────────────────────────────────────
COMPANY_NAME = "Bionexis Lab s. r. o."
COMPANY_ADDRESS = "Horná 37, 974 01 Banská Bystrica"
COMPANY_ID = ""  # TODO: IČO / DIČ / IČ DPH for the footer
# TODO: verify — a dedicated Querie inbox may be preferable.
CONTACT_EMAIL = "info@apollon-ia.com"

# ── Ad pixels ────────────────────────────────────────────────────────────────
# Placeholders until the real IDs are set in Render (QUERIE_META_PIXEL_ID,
# QUERIE_TIKTOK_PIXEL_ID). A placeholder counts as "not configured": that pixel
# is never loaded, and with neither configured there is no cookie banner.
META_PIXEL_PLACEHOLDER = "META_PIXEL_ID"
TIKTOK_PIXEL_PLACEHOLDER = "TIKTOK_PIXEL_ID"


def meta_pixel_id() -> str | None:
    value = settings.QUERIE_META_PIXEL_ID or META_PIXEL_PLACEHOLDER
    return None if value == META_PIXEL_PLACEHOLDER else value


def tiktok_pixel_id() -> str | None:
    value = settings.QUERIE_TIKTOK_PIXEL_ID or TIKTOK_PIXEL_PLACEHOLDER
    return None if value == TIKTOK_PIXEL_PLACEHOLDER else value

# ── Testimonials ─────────────────────────────────────────────────────────────
# Beta-tester quotes. The section is hidden while this list is empty. "photo"
# is optional (a path under /querie/assets/); without it initials are shown.
TESTIMONIALS: list[dict] = [
    # {
    #     "quote": "…",
    #     "name": "Jana Kováčová",
    #     "role": "Realitná maklérka, Bratislava",
    #     "photo": "/querie/assets/testimonials/jana.jpg",
    # },
]

# ── Legal documents ──────────────────────────────────────────────────────────
# Shared with ApollonIA: one VOP and one privacy policy for both products.
# TODO: the VOP PDF still describes only ApollonIA — update it to cover Querie.
TERMS_URL = "/assets/ApollonIA_VOP_SK_2.pdf"
# TODO: point at the shared privacy policy once it is published; until then
# this is Querie's own placeholder page.
PRIVACY_URL = f"{BASE_PATH}/ochrana-osobnych-udajov"

# Recorded with every sign-up (and on the Stripe subscription) so we know which
# documents were accepted. Bump it whenever the VOP or privacy policy changes.
CONSENT_VERSION = "VOP_SK_2+privacy-placeholder"  # TODO: bump when the updated documents are published

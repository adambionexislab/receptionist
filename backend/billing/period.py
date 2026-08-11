"""The billing period every "this month" metric is measured over.

One definition, shared by the call-minutes stats (calls/db.py) and the AI-tool
credit counters (usage/db.py), so two cards on the same dashboard can never
disagree about when the current period started.

That period is the tenant's **subscription month**, not the calendar month: it
begins on the day of the month their subscription renews and runs to the day
before the next renewal. An agency that signed up on the 22nd gets a fresh
allowance on the 22nd — which is the point, since that is when they are
charged. A calendar month would hand them a second allowance nine days later.

Boundaries are Rome-local midnight (same offset as Bratislava, so correct for
both IT and SK tenants). Timestamps are stored as UTC ISO strings, so the
bounds come back as UTC ISO strings too and compare directly against them.
"""

import calendar
import datetime
import logging
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ROME = ZoneInfo("Europe/Rome")

# Used for a tenant with no usable date at all. The 1st makes their period the
# calendar month — the old behaviour, and the least surprising thing to show
# for a row we can't read a subscription date from.
_FALLBACK_ANCHOR_DAY = 1


def _day_in(year: int, month: int, day: int) -> int:
    """The anchor day as it actually falls in one month. A subscription
    anchored on the 31st renews on the 30th in November and the 28th in
    February — the last day of the month, never spilling into the next one."""
    return min(day, calendar.monthrange(year, month)[1])


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = (year * 12 + month - 1) + delta
    return index // 12, index % 12 + 1


def anchor_day(tenant: dict[str, Any]) -> int:
    """The day of the month a tenant's subscription renews on.

    `billing_anchor` when the team has recorded the real Stripe billing date;
    otherwise `created_at`, the day the tenant was provisioned — which is right
    after they paid, so it is the best available proxy. Provisioning is manual,
    so it can lag the payment by a day or two; `billing_anchor` exists to
    correct that without touching created_at, which other things date from.
    """
    for field in ("billing_anchor", "created_at"):
        raw = (tenant.get(field) or "").strip()
        if not raw:
            continue
        try:
            stamp = datetime.datetime.fromisoformat(raw)
        except ValueError:
            logger.warning("Tenant %s has an unparseable %s: %r", tenant.get("id"), field, raw)
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        # The day as it reads in Rome, matching the Rome-midnight boundaries
        # below — a tenant created at 23:30 UTC renews on the next day's date.
        return stamp.astimezone(_ROME).day
    return _FALLBACK_ANCHOR_DAY


def subscription_month_utc(
    day: int, now: Optional[datetime.datetime] = None
) -> tuple[str, str]:
    """(start_utc_iso, next_start_utc_iso) for the subscription month containing
    `now` (defaults to the current instant), for a subscription anchored on
    `day` of the month.

    The end is exclusive — it is the instant the next period begins — so the
    two bounds tile the timeline with no gap and no overlap, and no second is
    ever counted in two periods or in neither.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    now_rome = now.astimezone(_ROME)

    start = now_rome.replace(
        day=_day_in(now_rome.year, now_rome.month, day),
        hour=0, minute=0, second=0, microsecond=0,
    )
    if now_rome < start:
        # This month's renewal hasn't happened yet, so the period running now
        # is the one that started at last month's.
        year, month = _shift_month(now_rome.year, now_rome.month, -1)
        start = start.replace(year=year, month=month, day=_day_in(year, month, day))

    year, month = _shift_month(start.year, start.month, 1)
    end = start.replace(year=year, month=month, day=_day_in(year, month, day))

    utc = datetime.timezone.utc
    return start.astimezone(utc).isoformat(), end.astimezone(utc).isoformat()


def tenant_month_utc(
    tenant: dict[str, Any], now: Optional[datetime.datetime] = None
) -> tuple[str, str]:
    """The subscription month one tenant is currently in. The single call every
    dashboard metric goes through, so they all measure the same window."""
    return subscription_month_utc(anchor_day(tenant), now)

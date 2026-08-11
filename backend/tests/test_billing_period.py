"""Tests for the subscription-month billing period (billing/period.py).

The rules that matter here: a period starts on the day the agency is charged
rather than on the 1st, consecutive periods tile the timeline exactly, a
month-end anchor never spills into the next month, and the anchor is read from
the tenant with a correctable override.
"""

import datetime

from billing import period

UTC = datetime.timezone.utc


def at(year, month, day, hour=12):
    return datetime.datetime(year, month, day, hour, tzinfo=UTC)


def test_period_runs_from_the_subscription_day_to_the_next_one():
    start, end = period.subscription_month_utc(22, now=at(2026, 8, 25))
    # Rome is UTC+2 in August, so Rome midnight on the 22nd is 22:00 UTC on the
    # 21st — the boundary is local midnight, not UTC midnight.
    assert start == "2026-08-21T22:00:00+00:00"
    assert end == "2026-09-21T22:00:00+00:00"


def test_before_this_months_renewal_the_period_started_last_month():
    """On the 5th with a renewal on the 22nd, the agency is 2 weeks into the
    period that began last month — not at the start of a new one."""
    start, end = period.subscription_month_utc(22, now=at(2026, 9, 5))
    assert start == "2026-08-21T22:00:00+00:00"
    assert end == "2026-09-21T22:00:00+00:00"


def test_the_boundary_is_the_stroke_of_rome_midnight():
    """Rome midnight on the 22nd is 22:00 UTC on the 21st in summer. A minute
    either side of it must land in different periods — the allowance renews on
    the agency's clock, not on UTC's."""
    boundary = datetime.datetime(2026, 9, 21, 22, tzinfo=UTC)
    before = period.subscription_month_utc(22, now=boundary - datetime.timedelta(minutes=1))
    after = period.subscription_month_utc(22, now=boundary)

    assert before[1] == after[0] == boundary.isoformat()
    assert before[0] == "2026-08-21T22:00:00+00:00"
    assert after[1] == "2026-10-21T22:00:00+00:00"


def test_winter_boundaries_shift_with_rome_daylight_saving():
    """The boundary is Rome midnight all year, so its UTC offset moves — an
    allowance must not renew an hour early or late across the DST change."""
    start, _ = period.subscription_month_utc(22, now=at(2027, 1, 25))
    assert start == "2027-01-21T23:00:00+00:00"  # UTC+1 in January


def test_a_month_end_anchor_clamps_to_short_months():
    """A subscription anchored on the 31st renews on the last day of months
    that have no 31st, never spilling into the next month."""
    feb = period.subscription_month_utc(31, now=at(2026, 2, 10))
    assert feb[0].startswith("2026-01-30T23:00")  # Jan 31, Rome
    assert feb[1].startswith("2026-02-27T23:00")  # Feb 28, Rome

    nov = period.subscription_month_utc(31, now=at(2026, 11, 15))
    assert nov[0].startswith("2026-10-30T23:00")  # Oct 31, Rome
    assert nov[1].startswith("2026-11-29T23:00")  # Nov 30, Rome

    # 2028 is a leap year: the February period ends on the 29th.
    leap = period.subscription_month_utc(31, now=at(2028, 2, 10))
    assert leap[1].startswith("2028-02-28T23:00")  # Feb 29, Rome


def test_consecutive_periods_tile_without_gap_or_overlap():
    """Each period's exclusive end is the next one's start, for every anchor
    day across a full year — so no second is billed twice or not at all."""
    for day in (1, 15, 28, 29, 30, 31):
        _, end = period.subscription_month_utc(day, now=at(2026, 1, 5))
        for _ in range(14):  # a full year of renewals, DST changes included
            closed_at = datetime.datetime.fromisoformat(end)
            # Step one hour past the boundary and ask again.
            start, end = period.subscription_month_utc(
                day, now=closed_at + datetime.timedelta(hours=1)
            )
            # The new period opens exactly where the previous one closed.
            assert datetime.datetime.fromisoformat(start) == closed_at
            assert datetime.datetime.fromisoformat(end) > closed_at


def test_anchor_comes_from_created_at_by_default():
    tenant = {"id": "t", "created_at": "2026-08-22T09:15:00+00:00"}
    assert period.anchor_day(tenant) == 22


def test_a_recorded_billing_date_overrides_created_at():
    """Provisioning is manual and can lag the payment, so the real Stripe date
    wins once the team records it."""
    tenant = {
        "id": "t",
        "created_at": "2026-08-24T09:15:00+00:00",
        "billing_anchor": "2026-08-22T00:00:00+00:00",
    }
    assert period.anchor_day(tenant) == 22


def test_a_late_night_signup_anchors_on_the_rome_date():
    """23:30 UTC is already the next day in Rome, and the boundaries are cut in
    Rome — the anchor has to agree with them."""
    tenant = {"id": "t", "created_at": "2026-08-21T23:30:00+00:00"}
    assert period.anchor_day(tenant) == 22


def test_an_unreadable_anchor_falls_back_rather_than_failing():
    """A tenant we can't read a date from still gets a period — the calendar
    month — instead of an exception on their dashboard."""
    assert period.anchor_day({"id": "t", "created_at": "not-a-date"}) == 1
    assert period.anchor_day({"id": "t"}) == 1
    assert period.anchor_day({"id": "t", "created_at": None}) == 1
    # A blank override falls through to created_at rather than to the 1st.
    assert period.anchor_day(
        {"id": "t", "billing_anchor": "", "created_at": "2026-08-22T09:00:00+00:00"}
    ) == 22


def test_tenant_month_matches_the_anchor_it_resolves():
    tenant = {"id": "t", "created_at": "2026-08-22T09:15:00+00:00"}
    assert period.tenant_month_utc(tenant, now=at(2026, 9, 5)) == (
        period.subscription_month_utc(22, now=at(2026, 9, 5))
    )

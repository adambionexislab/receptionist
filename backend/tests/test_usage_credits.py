"""Tests for plan allowances and the AI-tool usage ledger (usage/db.py).

The rules that matter here: each tool costs a fixed amount, one meeting is
charged once no matter how many times its transcription session is reopened,
both allowances (call minutes and tool credits) follow the subscription and
reset with the billing period, and running out of either produces overage
instead of blocking anything — with the two kinds of excess landing on one
invoice.
"""

import datetime
import sqlite3

import pytest

from tenants import db as tenants_db
from usage import db as usage_db

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"

# Mid-month in Rome, well inside the same month in UTC — so a test that charges
# "now" and one that charges at this instant land in the same period.
AUGUST = datetime.datetime(2026, 8, 15, 12, 0, tzinfo=datetime.timezone.utc)
SEPTEMBER = datetime.datetime(2026, 9, 2, 12, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    """Point the shared tenants connection at a throwaway in-memory DB, so
    these tests never touch the real data on disk."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(tenants_db, "get_connection", lambda: conn)
    monkeypatch.setattr(usage_db, "_initialized", False)
    usage_db.init()
    yield conn
    conn.close()


def charged_at(when, tenant=TENANT, tool="photo", ref=None):
    """Record one use and backdate it, so a test can place usage in a specific
    billing period without freezing the clock."""
    usage_db.record(tenant, tool, ref)
    conn = tenants_db.get_connection()
    conn.execute(
        "UPDATE tool_usage SET created_at = ? WHERE id = (SELECT MAX(id) FROM tool_usage)",
        (when.isoformat(),),
    )
    conn.commit()


def test_each_tool_costs_its_own_price():
    usage_db.record(TENANT, "photo")
    usage_db.record(TENANT, "photo")
    usage_db.record(TENANT, "meeting", "rec-1")

    usage = usage_db.monthly_usage(TENANT)
    # €0.50 + €0.50 + €1.00
    assert usage["used_cents"] == 200
    assert usage["uses"] == {"photo": 2, "meeting": 1}


def test_a_meeting_is_charged_once_however_often_its_session_reopens():
    """The 60-minute session cap, a dropped connection and resuming a stranded
    meeting all re-mint a token for the same record — one meeting, one credit."""
    assert usage_db.record(TENANT, "meeting", "rec-1") is True
    assert usage_db.record(TENANT, "meeting", "rec-1") is False
    assert usage_db.record(TENANT, "meeting", "rec-1") is False

    assert usage_db.monthly_usage(TENANT)["used_cents"] == 100
    # A different meeting is a different charge.
    assert usage_db.record(TENANT, "meeting", "rec-2") is True
    assert usage_db.monthly_usage(TENANT)["used_cents"] == 200


def test_every_photo_is_charged_even_for_the_same_image():
    """Photos carry no ref: re-running the tool on one image is a second
    (equally paid-for) generation, not a duplicate of the first."""
    assert usage_db.record(TENANT, "photo") is True
    assert usage_db.record(TENANT, "photo") is True
    assert usage_db.monthly_usage(TENANT)["uses"]["photo"] == 2


def test_allowances_follow_the_plan():
    assert usage_db.allowance_cents("Base") == 1500
    assert usage_db.allowance_cents("Pro") == 3000
    assert usage_db.allowance_cents("Max") == 6000
    assert usage_db.minute_allowance("Base") == 500
    assert usage_db.minute_allowance("Pro") == 1000
    assert usage_db.minute_allowance("Max") == 2000
    # One plan word drives both allowances, so they can never disagree.
    assert usage_db.minute_allowance(None) == 500
    assert usage_db.minute_allowance("Max (Annual)") == 2000
    # A plan copied from a Stripe label carries the billing period too, in
    # whatever language that label is written in — the tier word is all that
    # decides the allowance, so relabelling the products can't change anyone's
    # credits.
    assert usage_db.allowance_cents("Pro (Mensile)") == 3000
    assert usage_db.allowance_cents("Pro (Monthly)") == 3000
    assert usage_db.allowance_cents("Max (Annual)") == 6000
    assert usage_db.allowance_cents("base (ročne)") == 1500
    # A tenant with no plan (or an unreadable one) gets the smallest allowance
    # rather than an accidental free ride.
    assert usage_db.allowance_cents(None) == 1500
    assert usage_db.allowance_cents("Enterprise") == 1500


def test_remaining_counts_down_from_the_plan_allowance():
    for _ in range(4):
        usage_db.record(TENANT, "photo")        # 4 × €0.50 = €2.00
    usage_db.record(TENANT, "meeting", "rec-1")  # + €1.00

    credits = usage_db.monthly_credits(TENANT, "Base", minutes=120)
    assert credits["tools"]["allowance_cents"] == 1500
    assert credits["tools"]["used_cents"] == 300
    assert credits["tools"]["remaining_cents"] == 1200
    assert credits["minutes"]["included"] == 500
    assert credits["minutes"]["remaining"] == 380
    assert credits["overage_cents"] == 0


def test_spending_past_the_credit_allowance_becomes_overage():
    # Base is €15: thirty photos spend it exactly, and the tools keep working.
    for _ in range(30):
        usage_db.record(TENANT, "photo")
    exhausted = usage_db.monthly_credits(TENANT, "Base")
    assert exhausted["tools"]["remaining_cents"] == 0
    assert exhausted["overage_cents"] == 0

    usage_db.record(TENANT, "meeting", "rec-1")
    usage_db.record(TENANT, "photo")

    over = usage_db.monthly_credits(TENANT, "Base")
    assert over["tools"]["used_cents"] == 1650
    # Remaining floors at zero rather than going negative — the cards read
    # "nothing left" and "€1.50 over", never "-€1.50 left".
    assert over["tools"]["remaining_cents"] == 0
    assert over["tools"]["overage_cents"] == 150
    assert over["overage_cents"] == 150


def test_seconds_become_minutes_once_for_the_whole_period():
    """All of a period's seconds are summed and converted in one step, so no
    call is rounded up to a minute of its own."""
    # 40 calls of 90s is exactly 60 minutes of talking, and bills as 60 — not
    # the 80 that rounding each call up on its own would produce.
    assert usage_db.billable_minutes(40 * 90) == 60
    assert usage_db.billable_minutes(0) == 0
    assert usage_db.billable_minutes(29) == 0
    assert usage_db.billable_minutes(30) == 1
    assert usage_db.billable_minutes(89) == 1
    assert usage_db.billable_minutes(90) == 2


def test_half_minutes_always_round_the_same_way():
    """Python's round() rounds halves to even — round(1.5) == 2 but
    round(2.5) == 2 — which would make a bill depend on the parity of the
    minute it landed on. Halves go up here, every time."""
    for half_minutes in range(1, 12, 2):  # 0.5, 1.5, 2.5 … minutes
        seconds = half_minutes * 30
        assert usage_db.billable_minutes(seconds) == (half_minutes + 1) // 2


def test_minutes_past_the_plan_are_billed_by_the_minute():
    # Pro includes 1000 minutes; the 1000th is still included.
    at_limit = usage_db.monthly_credits(TENANT, "Pro", minutes=1000)
    assert at_limit["minutes"]["over"] == 0
    assert at_limit["minutes"]["remaining"] == 0
    assert at_limit["overage_cents"] == 0

    over = usage_db.monthly_credits(TENANT, "Pro", minutes=1080)
    assert over["minutes"]["over"] == 80
    # 80 × €0.25
    assert over["minutes"]["overage_cents"] == 2000
    assert over["overage_cents"] == 2000


def test_one_invoice_adds_up_minutes_and_tools():
    """The two allowances run out independently, but the agency is billed
    once — so the overage the dashboard shows is their sum."""
    for _ in range(31):
        usage_db.record(TENANT, "photo")  # €15.50 against Base's €15.00

    credits = usage_db.monthly_credits(TENANT, "Base", minutes=540)

    assert credits["tools"]["overage_cents"] == 50     # €0.50 of tools
    assert credits["minutes"]["overage_cents"] == 1000  # 40 min × €0.25
    assert credits["overage_cents"] == 1050


def test_an_unused_allowance_is_never_a_negative_charge():
    """Minutes to spare must not subsidise tool overage (or vice versa)."""
    for _ in range(31):
        usage_db.record(TENANT, "photo")

    credits = usage_db.monthly_credits(TENANT, "Base", minutes=10)

    assert credits["minutes"]["remaining"] == 490
    assert credits["minutes"]["overage_cents"] == 0
    assert credits["overage_cents"] == 50


def test_credits_reset_with_the_billing_period():
    charged_at(AUGUST, tool="meeting", ref="rec-1")
    charged_at(AUGUST, tool="photo")

    august = usage_db.monthly_credits(TENANT, "Pro", now=AUGUST)
    assert august["tools"]["used_cents"] == 150
    assert august["tools"]["remaining_cents"] == 2850

    # September starts from a full allowance without any reset job having run,
    # and August stays readable for invoicing.
    september = usage_db.monthly_credits(TENANT, "Pro", now=SEPTEMBER)
    assert september["tools"]["used_cents"] == 0
    assert september["tools"]["remaining_cents"] == 3000
    assert september["tools"]["uses"] == {"photo": 0, "meeting": 0}
    assert usage_db.monthly_credits(TENANT, "Pro", now=AUGUST)["tools"]["used_cents"] == 150


def test_usage_is_scoped_to_one_tenant():
    usage_db.record(TENANT, "photo")
    usage_db.record(TENANT, "meeting", "rec-1")
    # Same record id at another agency: scoped separately, so one tenant's
    # meeting can never suppress another's charge.
    assert usage_db.record(OTHER_TENANT, "meeting", "rec-1") is True

    assert usage_db.monthly_usage(TENANT)["used_cents"] == 150
    assert usage_db.monthly_usage(OTHER_TENANT)["used_cents"] == 100
    assert usage_db.monthly_credits(OTHER_TENANT, "Max")["tools"]["uses"] == {
        "photo": 0, "meeting": 1,
    }


def test_an_unknown_tool_is_rejected_rather_than_billed_at_zero():
    with pytest.raises(ValueError):
        usage_db.record(TENANT, "video")
    assert usage_db.monthly_usage(TENANT)["used_cents"] == 0

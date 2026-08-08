"""Tests for the AI-tool credit ledger (usage/db.py).

The rules that matter here: each tool costs a fixed amount, one meeting is
charged once no matter how many times its transcription session is reopened,
the allowance follows the subscription and resets with the billing period, and
running out produces overage instead of blocking the tool.
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


def test_allowance_follows_the_plan():
    assert usage_db.allowance_cents("Base") == 1500
    assert usage_db.allowance_cents("Pro") == 3000
    assert usage_db.allowance_cents("Max") == 6000
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

    credits = usage_db.monthly_credits(TENANT, "Base")
    assert credits["allowance_cents"] == 1500
    assert credits["used_cents"] == 300
    assert credits["remaining_cents"] == 1200
    assert credits["overage_cents"] == 0


def test_spending_past_the_allowance_becomes_overage():
    # Base is €15: thirty photos spend it exactly, and the tools keep working.
    for _ in range(30):
        usage_db.record(TENANT, "photo")
    exhausted = usage_db.monthly_credits(TENANT, "Base")
    assert exhausted["remaining_cents"] == 0
    assert exhausted["overage_cents"] == 0

    usage_db.record(TENANT, "meeting", "rec-1")
    usage_db.record(TENANT, "photo")

    over = usage_db.monthly_credits(TENANT, "Base")
    assert over["used_cents"] == 1650
    # Remaining floors at zero rather than going negative — the two cards read
    # "nothing left" and "€1.50 over", never "-€1.50 left".
    assert over["remaining_cents"] == 0
    assert over["overage_cents"] == 150


def test_credits_reset_with_the_billing_period():
    charged_at(AUGUST, tool="meeting", ref="rec-1")
    charged_at(AUGUST, tool="photo")

    august = usage_db.monthly_credits(TENANT, "Pro", now=AUGUST)
    assert august["used_cents"] == 150
    assert august["remaining_cents"] == 2850

    # September starts from a full allowance without any reset job having run,
    # and August stays readable for invoicing.
    september = usage_db.monthly_credits(TENANT, "Pro", now=SEPTEMBER)
    assert september["used_cents"] == 0
    assert september["remaining_cents"] == 3000
    assert september["uses"] == {"photo": 0, "meeting": 0}
    assert usage_db.monthly_credits(TENANT, "Pro", now=AUGUST)["used_cents"] == 150


def test_usage_is_scoped_to_one_tenant():
    usage_db.record(TENANT, "photo")
    usage_db.record(TENANT, "meeting", "rec-1")
    # Same record id at another agency: scoped separately, so one tenant's
    # meeting can never suppress another's charge.
    assert usage_db.record(OTHER_TENANT, "meeting", "rec-1") is True

    assert usage_db.monthly_usage(TENANT)["used_cents"] == 150
    assert usage_db.monthly_usage(OTHER_TENANT)["used_cents"] == 100
    assert usage_db.monthly_credits(OTHER_TENANT, "Max")["uses"] == {"photo": 0, "meeting": 1}


def test_an_unknown_tool_is_rejected_rather_than_billed_at_zero():
    with pytest.raises(ValueError):
        usage_db.record(TENANT, "video")
    assert usage_db.monthly_usage(TENANT)["used_cents"] == 0

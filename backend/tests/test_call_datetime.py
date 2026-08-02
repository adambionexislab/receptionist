"""Tests for the phone agent knowing what day it is (call/router.py).

Regression origin: asked when he was free for a viewing, the caller said
"tomorrow" and the lead recorded "piatok" — a weekday the model invented,
because nothing in the session ever told it the date. Every relative answer
("tomorrow", "next Monday", "next month") was a guess.

The prompt now ends with the real date in the tenant's timezone, and the
date-bearing tool fields ask for the caller's own words alongside a resolved
date — the agent reads the email hours later, when "tomorrow" has moved.
"""

import datetime
from zoneinfo import ZoneInfo

import pytest

from call import router

LOCALES = ("it", "sk")

# A Saturday, deliberately: the reported bug produced a weekday name, so the
# tests need one that is wrong unless it was actually computed.
SATURDAY = datetime.datetime(2026, 8, 1, 15, 42, tzinfo=ZoneInfo("Europe/Rome"))


@pytest.mark.parametrize("locale", LOCALES)
def test_the_prompt_states_the_current_date(locale):
    content = router._content(locale)

    prompt = router._build_system_prompt(content, "Studio Demo", None, now=SATURDAY)

    assert "2026-08-01" in prompt
    assert "15:42" in prompt


@pytest.mark.parametrize(
    "locale,weekday", [("it", "sabato"), ("sk", "sobota")]
)
def test_the_weekday_is_computed_not_guessed(locale, weekday):
    """The exact failure — a weekday the model made up. It has to come from the
    date, in the tenant's language."""
    line = router._now_line(router._content(locale), SATURDAY)

    assert weekday in line


@pytest.mark.parametrize("locale", LOCALES)
def test_every_weekday_maps_to_a_distinct_name(locale):
    """An off-by-one or duplicated entry would misname the day every time, and
    the model has no way to notice."""
    weekdays = router._content(locale)["weekdays"]

    assert len(weekdays) == 7
    assert len(set(weekdays)) == 7


@pytest.mark.parametrize("locale", LOCALES)
def test_each_weekday_index_matches_pythons(locale):
    """weekday() is Monday=0. Feeding it a known week catches a table written
    Sunday-first, which would shift every date by a day."""
    content = router._content(locale)
    # 2026-08-03 is a Monday.
    monday = datetime.datetime(2026, 8, 3, 9, 0, tzinfo=ZoneInfo(content["timezone"]))

    for offset in range(7):
        day = monday + datetime.timedelta(days=offset)
        assert content["weekdays"][offset] in router._now_line(content, day)


@pytest.mark.parametrize("locale", LOCALES)
def test_the_prompt_forbids_guessing_a_date(locale):
    prompt = router._build_system_prompt(router._content(locale), None, None, SATURDAY)

    forbids = {"it": "non indovinare", "sk": "neodhadujte"}[locale]
    assert forbids in prompt.lower()


@pytest.mark.parametrize("locale", LOCALES)
def test_the_tenant_name_still_leads_the_prompt(locale):
    """The date section is appended, so it must not displace the first line the
    tenant's agency name goes into."""
    content = router._content(locale)

    prompt = router._build_system_prompt(content, "Studio Demo", "Apollonia", SATURDAY)

    assert "Studio Demo" in prompt.splitlines()[1]
    assert "2026-08-01" in prompt


@pytest.mark.parametrize("locale", LOCALES)
def test_each_locale_has_a_real_timezone(locale):
    """A bad name raises at call time, on the request that answers the phone."""
    content = router._content(locale)

    assert ZoneInfo(content["timezone"])


@pytest.mark.parametrize("locale", LOCALES)
def test_the_date_is_local_not_utc(locale):
    """Render runs UTC. A viewing booked at 00:30 CEST would otherwise be
    recorded against the previous day."""
    content = router._content(locale)
    just_after_midnight = datetime.datetime(
        2026, 8, 2, 0, 30, tzinfo=ZoneInfo(content["timezone"])
    )

    assert "2026-08-02" in router._now_line(content, just_after_midnight)


def test_visit_availability_asks_for_words_and_a_resolved_date():
    """Both halves matter: the resolved date because the agent reads the email
    later, the caller's words because a wrong resolution should be visible."""
    field = router._RECORD_CALLER_INFO_TOOL["parameters"]["properties"]
    description = field["visit_availability"]["description"].lower()

    assert "caller's own words" in description
    assert "never guess" in description


def test_move_in_date_carries_the_same_rule():
    """Rentals ask for a date too, and it went unguarded for the same reason."""
    field = router._RECORD_CALLER_INFO_TOOL["parameters"]["properties"]
    description = field["move_in_date"]["description"].lower()

    assert "never guess" in description


def test_the_clock_defaults_to_now_when_no_time_is_passed():
    """Production passes nothing — the default is the whole feature."""
    content = router._content("sk")
    today = datetime.datetime.now(ZoneInfo(content["timezone"])).strftime("%Y-%m-%d")

    assert today in router._now_line(content)

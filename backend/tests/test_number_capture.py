"""Tests for how Apollonia handles numbers the caller speaks (router.py, locales.py).

Regression origin: the budget she searched with kept being a value the caller
never said, and always a plausible-looking rent below the real one:

    said 1500 -> search_listings(max_price=650), then 650 again, then 750
    said 1000 -> search_listings(max_price=600)

The last of those was a clean, unsplit 0.78s turn — the shortest answer in the
call — and the €690 Banská Bystrica flat it should have matched was the only
listing in that city. Errors that all land on a typical rent, and all below what
was said, are a model filling a gap it could not decode, not a mishearing: with
no rule telling her to ask, guessing was the only move the prompt left her.

Worse, she never said the number out loud — she went straight from the question
to the tool to "nothing matches your budget" — so the caller had no way to catch
it. The fix pinned here is one read-back turn before the search: it caught a
real one on the next call, "Osemdesiat eur mesačne, správne?" for a spoken
osemsto, corrected in a single turn.

The read-back is the whole safety net, on purpose. A second net after the search
— reciting the criteria and offering to change them — was tried and removed: it
turned a zero result into a three-question interrogation the caller hung up on.
"""

import re

import pytest

from call import router

LOCALES = ("it", "sk")


def _prompt(locale):
    """Flattened so a reflow of the hard-wrapped source can't fail these."""
    body = router._content(locale)["system_prompt_body"]
    return re.sub(r"\s+", " ", body).lower()


# The section heading, and phrases from each rule inside it.
SECTION = {"it": "# numeri detti dal chiamante", "sk": "# čísla, ktoré povie volajúci"}
DONT_GUESS = {"it": "non tirare a indovinare", "sk": "nehádajte"}
CONFIRM_BUDGET = {"it": "aspetta che confermi", "sk": "počkajte na potvrdenie"}
DROP_OLD_VALUE = {"it": "non riutilizzare mai quello di prima", "sk": "nikdy nepoužite to predchádzajúce"}
# The TYPE B step that routes into the section rather than restating it.
TYPE_B_POINTER = {"it": "conferma il budget come indicato", "sk": "potvrďte rozpočet podľa"}


@pytest.mark.parametrize("locale", LOCALES)
def test_the_section_exists_in_every_locale(locale):
    """The Slovak prompt is a separate document, not a translation layer, so a
    rule added only to the Italian one silently does not exist for sk tenants."""
    assert SECTION[locale] in _prompt(locale)


@pytest.mark.parametrize("locale", LOCALES)
def test_she_is_told_not_to_guess_a_number(locale):
    """The failure was never a wrong transcription she was confident in — it was
    a blank she filled with a plausible value."""
    assert DONT_GUESS[locale] in _prompt(locale)


@pytest.mark.parametrize("locale", LOCALES)
def test_the_budget_is_confirmed_before_searching(locale):
    """One read-back turn converges the value; without it a bad number is only
    discovered by the agent reading the lead, hours later."""
    assert CONFIRM_BUDGET[locale] in _prompt(locale)


@pytest.mark.parametrize("locale", LOCALES)
def test_a_correction_replaces_the_old_value(locale):
    """She re-sent an identical failed search after being corrected — 650, then
    650 again — so being told to drop the previous value is its own rule."""
    assert DROP_OLD_VALUE[locale] in _prompt(locale)


@pytest.mark.parametrize("locale", LOCALES)
def test_type_b_points_at_the_section_instead_of_restating_it(locale):
    """gpt-realtime-2 pays for instruction conflict, so the search flow refers
    to the rule rather than keeping a second copy that can drift from it."""
    assert TYPE_B_POINTER[locale] in _prompt(locale)


@pytest.mark.parametrize("locale", LOCALES)
def test_the_zero_result_step_stays_a_single_open_question(locale):
    """Deliberately NOT expanded. Making her recite the criteria and offer to
    change them turned a dead end into an interrogation — location, budget,
    rooms, three answers, no second search, caller hung up. Callers do not
    change their mind about the city, so the read-back before the search is the
    whole safety net; after a zero result she asks once and stops."""
    step = {
        "it": "3. se nessun risultato: chiedi se vuole provare criteri diversi.",
        "sk": "3. ak žiadny výsledok: spýtajte sa, či chce skúsiť iné kritériá.",
    }[locale]
    assert step in _prompt(locale)


@pytest.mark.parametrize("locale", LOCALES)
def test_the_worked_example_is_in_the_caller_s_language(locale):
    """The read-back example is spoken aloud. An Italian example sitting in the
    Slovak prompt is exactly how English tool schemas leaked pronunciation
    before — see the sk_tools commit."""
    example = {"it": "mille euro al mese", "sk": "tisíc eur mesačne"}[locale]
    assert example in _prompt(locale)

"""Tests for how much Apollonia says about a listing (call/router.py, locales.py).

Regression origin: asked about one house she answered with this, in a single
breath, before the caller had asked anything:

    Dom na adrese Pezinská 19 v Pezinku je dostupný. Je to dvojpodlažný
    rodinný dom so šiestimi izbami, záhradou, terasou, garážou a parkovaním
    pod prístreškom, s rekonštruovanou kuchyňou a kúpeľňami a novšou
    strechou. Aby som mohla...

Eight features, no price, no size — she was reciting the listing's free-text
description. The prompt only asked her to describe it "stručne" / "brevemente",
which is not a limit gpt-realtime-2 can act on; it follows explicit
instructions far better than implied ones. The rules now carry hard numbers,
and these tests keep the numbers there.
"""

import re

import pytest

from call import router

LOCALES = ("it", "sk")


def _prompt(locale):
    """The prompt as one flat string. The source is hard-wrapped at ~72 chars,
    so matching raw text would make these tests fail on a harmless reflow
    rather than on the rule actually being removed."""
    body = router._content(locale)["system_prompt_body"]
    return re.sub(r"\s+", " ", body).lower()


@pytest.mark.parametrize("locale", LOCALES)
def test_a_listing_description_is_capped_at_one_sentence(locale):
    """"Briefly" left the length entirely to the model. A sentence count is
    something it can actually check itself against."""
    marker = {"it": "una sola frase", "sk": "jedna jediná veta"}[locale]

    assert marker in _prompt(locale)


@pytest.mark.parametrize("locale", LOCALES)
def test_the_number_of_facts_is_capped(locale):
    """Three facts, not eight. Stated in both the length rule and the TYPE A
    step, since that step is what she is following at the moment it matters."""
    marker = {"it": "tre dati", "sk": "tromi údajmi"}[locale]

    assert marker in _prompt(locale)


@pytest.mark.parametrize("locale", LOCALES)
def test_listing_features_are_not_to_be_listed(locale):
    """The actual content of the runaway answer was a feature list — garden,
    terrace, garage, carport, renovated kitchen."""
    marker = {"it": "non elencare le dotazioni", "sk": "nevymenúvajte vybavenie"}[
        locale
    ]

    assert marker in _prompt(locale)


@pytest.mark.parametrize("locale", LOCALES)
def test_the_free_text_field_is_answers_only_never_the_opening_description(locale):
    """'text' holds the full listing blurb. Reciting it is what produced the
    wall of speech, so it is now scoped to answering direct questions."""
    prompt = _prompt(locale)

    scoped = {"it": "serve solo a rispondere", "sk": "slúži iba na odpovede"}[locale]
    assert scoped in prompt


@pytest.mark.parametrize("locale", LOCALES)
def test_search_results_are_offered_one_at_a_time(locale):
    """TYPE B had the same failure mode multiplied by the number of matches."""
    marker = {"it": "presentane uno alla volta", "sk": "vždy jednu naraz"}[locale]

    assert marker in _prompt(locale)


@pytest.mark.parametrize("locale", LOCALES)
def test_the_facts_to_lead_with_are_named(locale):
    """Left to choose, she picked carport over price. The decision-relevant
    facts are named so the one sentence carries the useful ones."""
    prompt = _prompt(locale)

    price = {"it": "prezzo", "sk": "cenu"}[locale]
    assert price in prompt

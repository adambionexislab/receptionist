"""Tests for how Apollonia asks the qualifying questions (router.py, locales.py).

Regression origin: she appended suggested answers to almost every question —
"Kedy by ste mali čas na obhliadku? Stačí povedať, ktorý deň alebo čas vám
približne vyhovuje.", "…napríklad budúci týždeň poobede.", "…alebo nie?"

Nothing ever told her to do that. What seeded it was a single item in the
qualifying-question list that carried its own spoken example set:

    - Pracovná situácia (zamestnanec, živnostník, študent?)

That parenthesis was a note about what the field means, but nothing marked it
as not-to-be-spoken, so she read it out and then generalised "question +
examples" to the whole list. The categories now live only in the tool schema,
which is never spoken aloud, and the rule about when an example IS warranted is
explicit.
"""

import re

import pytest

from call import router

LOCALES = ("it", "sk")

# The question each list opens with, and the categories that used to trail it.
EMPLOYMENT_QUESTION = {"it": "situazione lavorativa", "sk": "pracovná situácia"}
SPOKEN_CATEGORIES = {
    "it": ("dipendente, autonomo, studente?",),
    "sk": ("zamestnanec, živnostník, študent?",),
}


def _prompt(locale):
    """Flattened so a reflow of the hard-wrapped source can't fail these."""
    body = router._content(locale)["system_prompt_body"]
    return re.sub(r"\s+", " ", body).lower()


@pytest.mark.parametrize("locale", LOCALES)
def test_the_question_list_still_covers_employment(locale):
    """Removing the examples must not remove the question."""
    assert EMPLOYMENT_QUESTION[locale] in _prompt(locale)


@pytest.mark.parametrize("locale", LOCALES)
def test_no_question_ships_with_a_spoken_example_set(locale):
    """The seed. A parenthetical menu next to a question is read out."""
    prompt = _prompt(locale)

    for categories in SPOKEN_CATEGORIES[locale]:
        assert categories not in prompt


def test_the_employment_categories_survive_where_they_are_never_spoken():
    """They still matter for classifying the answer — they just belong in the
    tool schema, which the caller never hears."""
    field = router._RECORD_CALLER_INFO_TOOL["parameters"]["properties"][
        "employment_status"
    ]["description"]

    assert "dipendente" in field
    assert "not a menu to read out" in field


@pytest.mark.parametrize("locale", LOCALES)
def test_the_default_is_to_ask_and_stop(locale):
    marker = {"it": "fai la domanda e fermati", "sk": "otázku položte a mlčte"}[locale]

    assert marker in _prompt(locale)


@pytest.mark.parametrize("locale", LOCALES)
def test_the_phrases_she_actually_used_are_named(locale):
    """Naming the exact string worked better than an abstract prohibition when
    we did it for the farewell, so these are quoted from the call logs."""
    prompt = _prompt(locale)

    quoted = {
        "it": "per esempio la settimana prossima nel pomeriggio",
        "sk": "napríklad budúci týždeň poobede",
    }[locale]
    assert quoted in prompt


@pytest.mark.parametrize("locale", LOCALES)
def test_the_tacked_on_or_not_is_named_too(locale):
    """"Máte nehnuteľnosť...alebo nie?" — turning a plain question into a
    menu of two."""
    marker = {"it": "oppure no?", "sk": "alebo nie?"}[locale]

    assert marker in _prompt(locale)


@pytest.mark.parametrize("locale", LOCALES)
def test_examples_are_allowed_when_the_caller_did_not_understand(locale):
    """A flat ban would leave her unable to help a caller who is stuck. The
    rule is conditional, and the condition has to be in the prompt."""
    prompt = _prompt(locale)

    trigger = {"it": "solo quando il chiamante mostra di non aver capito",
               "sk": "iba vtedy, keď volajúci dá najavo, že otázke nerozumel"}[locale]
    assert trigger in prompt


@pytest.mark.parametrize("locale", LOCALES)
def test_the_explanation_is_capped_at_one_example(locale):
    """Otherwise a confused caller gets the full menu after all."""
    marker = {"it": "aggiungi un esempio", "sk": "pridajte jeden príklad"}[locale]

    assert marker in _prompt(locale)

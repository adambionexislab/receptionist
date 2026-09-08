"""Tests for the mandatory AI disclosure in the opening sentence.

EU transparency rules on AI systems that interact with people: the caller has
to be told he is not talking to a human, unprompted, at the start of the call.
So the disclosure cannot live in the greeting text alone — it has to be a rule
in the prompt (a caller who opens in another language, or any regenerated first
turn, must still get it) and it has to name the tenant's agency, which is why
the section is formatted per tenant like the first line.
"""

import re

import pytest

from call import router
from demo import router as demo_router

LOCALES = ("it", "sk")

# The words that make the sentence a disclosure. Anything softer ("assistente"
# alone) leaves the caller thinking a human picked up.
VIRTUAL_ASSISTANT = {"it": "assistente virtuale", "sk": "virtuálna asistentka"}


def _flat(text):
    """Flattened so a reflow of the hard-wrapped source can't fail these."""
    return re.sub(r"\s+", " ", text).lower()


@pytest.mark.parametrize("locale", LOCALES)
def test_the_prompt_requires_the_disclosure_in_the_first_sentence(locale):
    prompt = _flat(router._build_system_prompt(router._content(locale), "Studio Demo", None))

    assert VIRTUAL_ASSISTANT[locale] in prompt
    first = {"it": "nella tua prima frase", "sk": "v prvej vete"}[locale]
    assert first in prompt


@pytest.mark.parametrize("locale", LOCALES)
def test_the_disclosure_names_the_tenants_agency(locale):
    """The agency has to reach the example sentence, not just the role line:
    "sono l'assistente virtuale di" with nothing after it is what the caller
    would hear."""
    prompt = router._build_system_prompt(router._content(locale), "Studio Demo", None)

    # once in the role line, once in the opening declaration
    assert prompt.count("Studio Demo") >= 2


@pytest.mark.parametrize("locale", LOCALES)
def test_the_disclosure_uses_the_name_the_tenant_configured(locale):
    """A tenant who renamed the agent must not have her say 'Apollonia' in the
    one sentence that identifies her."""
    prompt = router._build_system_prompt(router._content(locale), "Studio Demo", "Aurora")

    assert prompt.count("Aurora") >= 2


@pytest.mark.parametrize("locale", LOCALES)
def test_the_sentence_still_reads_when_there_is_no_agency_name(locale):
    """The env-var demo fallback has no tenant row: the example must not become
    'sono l'assistente virtuale di {agency}' or trail off into nothing."""
    prompt = router._build_system_prompt(router._content(locale), None, None)

    assert "{agency}" not in prompt
    assert router._content(locale)["agency_fallback"] in prompt


@pytest.mark.parametrize("locale", LOCALES)
def test_the_disclosure_is_never_omitted_in_another_language(locale):
    """She speaks first, but a caller can steer the call into another language;
    the language rule must not read as permission to drop the declaration."""
    prompt = _flat(router._content(locale)["opening_section"])

    marker = {
        "it": "la dichiarazione non si omette mai",
        "sk": "vyhlásenie nikdy nevynechávajte",
    }[locale]
    assert marker in prompt


@pytest.mark.parametrize("locale", LOCALES)
def test_a_cut_off_disclosure_is_repeated(locale):
    """Cut off partway, the caller never heard it, so the opening starts again.

    Deliberately NOT narrowed to "only repeat if you didn't reach the end of the
    sentence" — that was tried and reverted. She reaches the end of the
    generated TEXT every time; whether the caller heard it depends on audio
    playout, which she cannot observe. Conditioning the disclosure on something
    invisible to her risks skipping it. Repeating an opening the caller already
    heard is a wasted sentence; skipping one they didn't is a legal problem."""
    prompt = _flat(router._content(locale)["opening_section"])

    marker = {
        "it": "ripeti la frase di apertura per intero",
        "sk": "zopakujte celú úvodnú vetu od začiatku",
    }[locale]
    assert marker in prompt


@pytest.mark.parametrize("locale", LOCALES)
def test_the_greeting_turn_asks_for_the_introduction(locale):
    """The greeting is generated from this one user message; if it only says
    'greet the caller', that is the turn where the disclosure goes missing."""
    prompt = _flat(router._content(locale)["greeting_prompt"])

    marker = {"it": "presentati", "sk": "predstavte sa"}[locale]
    assert marker in prompt


@pytest.mark.parametrize("locale", LOCALES)
def test_the_website_demo_opening_carries_it_too(locale):
    """The demo note is appended last and overrides the phone opening, so a
    demo note that only says 'introduce yourself as Apollonia' would win."""
    note = _flat(demo_router._DEMO_NOTES[locale])

    assert VIRTUAL_ASSISTANT[locale] in note


@pytest.mark.parametrize(
    "locale,doubled",
    [("it", "assistente virtuale di apollonia"), ("sk", "virtuálna asistentka apollonia")],
)
def test_the_demo_does_not_make_her_say_her_name_twice(locale, doubled):
    """In the demo the "agency" is ApollonIA — which is also her own name, so
    the disclosure came out as "volám sa Apollonia, som virtuálna asistentka
    ApollonIA". There she declares she is virtual without naming anyone."""
    assert doubled not in _flat(demo_router._DEMO_NOTES[locale])

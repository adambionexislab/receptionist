"""Tests for what the lead email — and the summary model reading it — is told.

Regression origin: a caller asked about a house in Pezinok and mentioned he had
a one-bedroom flat in Bratislava to sell first. The summary announced he had
called to buy a one-bedroom flat in Bratislava. The email itself was correct;
what misled the summariser was the label "Nehnuteľnosť na predaj" ("property
for sale"), which reads exactly like a listing on offer. So these tests pin
down the two things that keep the caller's own property distinct from the one
he called about: an unambiguous label, and a summary prompt that maps the
sections for the model.
"""

import pytest

from call import router
from config import Settings, _TEXT_MODEL_DEFAULT

LOCALES = ("it", "sk")


def test_the_text_models_share_one_default_so_a_bump_reaches_both():
    """SUMMARY_MODEL and EXTRACTION_MODEL are separate env vars so one task can
    be pinned elsewhere, but their in-code defaults come from a single constant
    — otherwise a model bump silently leaves one of them behind. Asserted on the
    declared defaults, not the resolved settings, which any .env can override."""
    defaults = Settings.model_fields

    assert defaults["SUMMARY_MODEL"].default == _TEXT_MODEL_DEFAULT
    assert defaults["EXTRACTION_MODEL"].default == _TEXT_MODEL_DEFAULT


def _session(locale, interested, caller_info, shown=None):
    return {
        "locale": locale,
        "interested_listings": interested,
        "listings_shown": shown if shown is not None else list(interested),
        "caller_info": caller_info,
    }


# The call that exposed the bug, as the session held it.
PEZINOK_HOUSE = {
    "address": "Hollého 12, Pezinok",
    "zone": "Pezinok",
    "type": "vendita",
    "rooms": 5,
    "size_sqm": 140,
    "price": 320000,
}
ADAM_INFO = {
    "name": "Adam",
    "has_mortgage_preapproval": "zatiaľ nie",
    "has_property_to_sell": "áno, jednoizbový byt v Bratislave",
    "sale_timeline": "do dvoch mesiacov",
    "visit_availability": "budúci týždeň, ktorýkoľvek deň",
}


@pytest.mark.parametrize("locale", LOCALES)
def test_summary_prompt_quotes_the_real_section_headers(locale):
    """The instruction tells the model which section means what by quoting the
    headers verbatim. A header edited on one side only would silently break
    that mapping and leave the model guessing again."""
    content = router._content(locale)
    instruction = content["summary_instruction"]

    assert content["email_section_interested"] in instruction
    assert content["email_section_collected"] in instruction


@pytest.mark.parametrize("locale", LOCALES)
def test_summary_prompt_warns_about_the_caller_s_own_property(locale):
    content = router._content(locale)

    assert (
        content["caller_info_labels"]["has_property_to_sell"]
        in content["summary_instruction"]
    )


@pytest.mark.parametrize("locale", LOCALES)
def test_the_property_to_sell_label_cannot_be_read_as_a_listing(locale):
    """'Immobile da vendere' / 'Nehnuteľnosť na predaj' name the offer, not the
    caller's own home; the label has to say whose property it is."""
    label = router._content(locale)["caller_info_labels"]["has_property_to_sell"]

    owned = {"it": "proprietà", "sk": "Vlastná"}[locale]
    before_buying = {"it": "prima dell'acquisto", "sk": "pred kúpou"}[locale]
    assert owned in label
    assert before_buying in label


@pytest.mark.parametrize("locale", LOCALES)
def test_own_property_to_sell_never_lands_in_the_interested_section(locale):
    """The end-to-end shape of the Pezinok call: the flat the caller must sell
    belongs to the collected block, and only the house he asked about sits
    under 'interested'."""
    content = router._content(locale)
    body = router._format_lead_body(
        content, _session(locale, [PEZINOK_HOUSE], ADAM_INFO), "+393899376234", [], False
    )

    collected, _, rest = body.partition(content["email_section_collected"])
    caller_block, _, listing_block = rest.partition(content["email_section_interested"])

    assert "jednoizbový byt v Bratislave" in caller_block
    assert "jednoizbový byt v Bratislave" not in listing_block
    assert "Pezinok" in listing_block.partition(content["email_section_others"])[0]
    assert "Pezinok" not in caller_block
    assert collected.startswith(f"{content['email_caller_label']}: +393899376234")


@pytest.mark.parametrize("locale", LOCALES)
def test_no_interest_says_so_rather_than_leaving_the_section_blank(locale):
    """An empty 'interested' section would let the summariser reach into the
    collected data for something that looks like a property."""
    content = router._content(locale)
    body = router._format_lead_body(
        content, _session(locale, [], ADAM_INFO), "+393899376234", [], False
    )

    interested = body.partition(content["email_section_interested"])[2]
    assert content["email_none_specified"] in interested
    assert "jednoizbový byt v Bratislave" not in interested


@pytest.mark.parametrize("locale", LOCALES)
def test_body_formatting_error_still_yields_a_sendable_lead(locale):
    """A lead is worth more than its formatting: a malformed session must not
    raise out of the email path."""
    content = router._content(locale)
    body = router._format_lead_body(content, {"locale": locale}, "+39123", [], False)

    assert "+39123" in body
    assert content["email_format_error"] in body

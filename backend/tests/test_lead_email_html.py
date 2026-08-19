"""Tests for the HTML part of the post-call lead email (call/router.py).

The mail is sent twice over: a plain-text part, which is the record the summary
model read and which any client can display, and an HTML part that adds nothing
but emphasis — section headings and field names in bold — so an agent can skim
a lead instead of reading it. Two things therefore have to hold: the HTML must
be derived from that same text (never assembled separately, or the two drift),
and a slip in it must cost the emphasis, not the lead.
"""

from html import escape

import pytest

from call import router

LOCALES = ("it", "sk")

HOUSE = {
    "address": "Hollého 12, Pezinok",
    "zone": "Pezinok",
    "type": "vendita",
    "rooms": 5,
    "size_sqm": 140,
    "price": 320000,
}
CALLER_INFO = {"name": "Adam", "visit_availability": "budúci týždeň"}


def _session(locale, **overrides):
    session = {
        "locale": locale,
        "interested_listings": [HOUSE],
        "listings_shown": [HOUSE],
        "caller_info": CALLER_INFO,
    }
    session.update(overrides)
    return session


def _html(locale, session, summary="Adam ha chiamato."):
    content = router._content(locale)
    body = router._format_lead_body(content, session, "+393899376234", [], False)
    return content, router._format_lead_html(content, summary, body)


@pytest.mark.parametrize("locale", LOCALES)
def test_section_headings_are_bold_and_keep_their_markers(locale):
    """The '===' around a heading belong to both parts of the mail: the bold is
    added on top of them, not in place of them, so the HTML reads as the same
    document as the text."""
    content, html = _html(locale, _session(locale))

    for key in ("email_section_collected", "email_section_interested",
                "email_section_others"):
        assert f"<strong>{content[key]}</strong>" in html


@pytest.mark.parametrize("locale", LOCALES)
def test_field_names_are_bold_and_their_values_are_not(locale):
    content, html = _html(locale, _session(locale))

    assert f"<strong>{content['email_caller_label']}:</strong> +393899376234" in html
    assert f"<strong>{content['caller_info_labels']['name']}:</strong> Adam" in html


@pytest.mark.parametrize("locale", LOCALES)
def test_a_colon_inside_a_value_does_not_invent_a_field_name(locale):
    """Only the labels the body actually writes count as field names — the mail
    is full of caller speech, and a message reading 'richiamo: dopo le 18' must
    not come out with half of itself in bold."""
    content, html = _html(
        locale,
        _session(
            locale,
            left_message={
                "caller_name": "Adam",
                "urgency": "urgente",
                "message": "richiamo: dopo le 18",
            },
        ),
    )

    assert f"<strong>{content['email_message_label']}:</strong> richiamo: dopo le 18" in html
    assert "<strong>richiamo:</strong>" not in html


@pytest.mark.parametrize("locale", LOCALES)
def test_caller_speech_cannot_smuggle_markup_into_the_mail(locale):
    """Everything below the subject line is dictated by whoever called."""
    _, html = _html(
        locale,
        _session(locale, caller_info={"name": "<script>alert(1)</script> & Co"}),
    )

    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; Co" in html


@pytest.mark.parametrize("locale", LOCALES)
def test_every_text_line_survives_into_the_html(locale):
    """The HTML is a rendering of the text body, not a second summary of the
    call: nothing the agent would have read in the text may go missing."""
    content = router._content(locale)
    body = router._format_lead_body(content, _session(locale), "+393899376234", [], False)
    html = router._format_lead_html(content, "Adam ha chiamato.", body)

    for line in (l.strip() for l in body.split("\n")):
        if not line or line.startswith("="):
            continue
        # Labels arrive wrapped in <strong>, so check the two halves of the
        # line rather than the line itself, and against the escaped text —
        # an apostrophe reaches the mail as &#x27;.
        label, sep, value = line.partition(":")
        if sep:
            assert escape(label) in html and escape(value.strip()) in html
        else:
            assert escape(line) in html


def test_a_formatting_slip_costs_the_bold_not_the_lead():
    """The HTML part is optional by construction: when it cannot be built the
    caller drops it and sends the text-only mail, which is complete on its own."""
    assert router._format_lead_html({}, "Adam ha chiamato.", "Chiamante: +39123") == ""

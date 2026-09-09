"""Tests for _extract_sip_number (call/router.py) — how a phone number is read
out of a SIP From/To/Diversion header.

Two things this guards, both found on the first real DIDWW call:

1. The To header of every call reaching OpenAI's SIP connector carries the
   PROJECT ID, not the dialed DID (the DID arrives in Diversion instead). The
   parser must not manufacture a number out of it: "proj_34LHXs57ZcnMu1aSmvk9RQYO"
   contains nine digits, which is exactly the width _same_number compares on,
   so a fabricated number here could route a call to the wrong tenant.

2. Carriers disagree about the leading +. Twilio sends it, DIDWW does not. The
   caller's number goes into the agency's lead email verbatim, so it has to
   come out dialable either way.
"""

from call.router import _extract_sip_number, _sip_header

# The real headers from the first DIDWW call, trimmed to the two that matter.
DIDWW_HEADERS = [
    {
        "name": "From",
        "value": '"+421948155064" <sip:421948155064@46.19.210.14>;tag=11-650F3232',
    },
    {
        "name": "To",
        "value": "<sip:proj_34LHXs57ZcnMu1aSmvk9RQYO@sip.api.openai.com:5061>;tag=b9fd592c",
    },
]


def test_project_id_is_not_a_number():
    """The To header on a SIP-connector call must yield nothing at all."""
    assert _extract_sip_number(_sip_header(DIDWW_HEADERS, "To")) == ""


def test_didww_caller_gets_its_plus_back():
    assert _extract_sip_number(_sip_header(DIDWW_HEADERS, "From")) == "+421948155064"


def test_twilio_caller_keeps_its_plus():
    assert _extract_sip_number('"Mario" <sip:+390212345678@host;user=phone>;tag=x') == (
        "+390212345678"
    )


def test_tel_uri():
    assert _extract_sip_number("<tel:+421948155064>") == "+421948155064"


def test_diversion_header_carries_the_dialed_did():
    """What DIDWW injects once Diversion Inject Mode is on — this is the value
    the tenant lookup routes on."""
    assert _extract_sip_number(
        "<sip:+421948155064@sip.didww.com>;reason=unconditional"
    ) == "+421948155064"


def test_international_access_prefix_becomes_plus():
    assert _extract_sip_number("<sip:00421948155064@host>") == "+421948155064"


def test_national_format_is_left_alone():
    """A single leading 0 is national format and we cannot know its country, so
    guessing a + would invent a number in some other country."""
    assert _extract_sip_number("<sip:0948155064@host>") == "0948155064"


def test_anonymous_caller():
    assert _extract_sip_number('"Anonymous" <sip:anonymous@anonymous.invalid>') == ""


def test_empty_header():
    assert _extract_sip_number("") == ""


def test_alphanumeric_sip_user_is_rejected():
    """A SIP address that is a name, not a number, yields nothing rather than
    the digits that happen to be in it."""
    assert _extract_sip_number("<sip:reception2@pbx.example.com>") == ""


def test_separators_are_stripped():
    """Punctuation a carrier may leave in the user part. Not spaces: those are
    not legal there unescaped, and the URI regex stops at whitespace."""
    assert _extract_sip_number("<sip:+39.02.1234-5678@host>") == "+390212345678"


def test_short_extension_is_not_promoted_to_e164():
    """Too short to be an international number — left as dialed rather than
    turned into a bogus country code."""
    assert _extract_sip_number("<sip:4321@pbx>") == "4321"

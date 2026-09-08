"""Nothing in another language may reach the model on a tenant's call.

Regression origin: Slovak calls came out with English phonology. The cause was
not the Slovak prompt — it was ~600 words of English `description` fields in the
shared tool schemas, re-serialised into every single turn and competing with it.
Phonetic respellings were tried as a fix first and made things worse (see
locales.py); localising the schemas is what worked.

Everything the model reads is in scope, not just the system prompt: tool
descriptions are prompt text too, and so is the greeting turn, the farewell
instruction, the date section and the demo note.

What is deliberately NOT translated is identifiers — tool names, parameter
names, enum tokens and the status strings the prompt quotes back. Handlers
dispatch on those, and the tool descriptions tell her in Slovak that they are
internal values never to be spoken. IDENTIFIERS below is the full permitted
list; anything else foreign is a bug.
"""

import re

import pytest

from call import router
from demo import router as demo_router

LOCALES = ("it", "sk")

# Tool names, parameter names, enum tokens and result strings the prompt quotes.
# Stripped before scanning, because they are machine tokens by design.
IDENTIFIERS = (
    "search_listings",
    "get_listing_by_address",
    "mark_listing_interest",
    "record_caller_info",
    "leave_message",
    "end_call",
    "address_query",
    "area",
    "saved",
    "vendita",
    "affitto",
    "normale",
    "urgente",
    # Product name. Not a session tool — see test_the_prompt_promises_no_tool_it_lacks.
    "ApollonIA Meeting",
)

# Letters that exist in Slovak and never in Italian. Precise enough to have no
# false positives, which a wordlist could not manage in the other direction.
SLOVAK_ONLY = re.compile(r"[ľňŕšťžčďĺäôý]")

# High-signal Italian and English words that would never appear in Slovak prose.
FOREIGN_IN_SLOVAK = (
    "chiamante", "immobile", "immobili", "richiesta", "agente", "della",
    "nella", "chiedi", "rispondi", "oppure", "soltanto", "sempre",
    "caller", "listing", "property", "please", "whether", "the ",
)


def _strip_identifiers(text):
    for token in IDENTIFIERS:
        text = text.replace(token, " ")
    return text


def _model_facing_text(locale):
    """Every string that reaches the model on a call in this locale."""
    content = router._content(locale)
    parts = [
        router._build_system_prompt(content, "Štúdio Demo", "Apollonia"),
        content["greeting_prompt"],
        content["farewell_instruction"],
        content["ask_for_number"],
        demo_router._DEMO_NOTES[locale],
    ]
    for tool in content["tools"]:
        parts.append(tool["description"])
        for field in tool["parameters"]["properties"].values():
            parts.append(field.get("description", ""))
    return "\n".join(parts)


@pytest.mark.parametrize("locale", LOCALES)
def test_every_tool_description_is_translated(locale):
    """The schemas were the actual cause. They are per-locale content now, not
    constants, so this checks each locale really has its own rather than falling
    back to the Italian ones."""
    tools = router._content(locale)["tools"]

    assert tools, locale
    for tool in tools:
        text = tool["description"]
        if locale == "sk":
            assert SLOVAK_ONLY.search(text), f"{tool['name']} not in Slovak"
        else:
            assert not SLOVAK_ONLY.search(text), f"{tool['name']} not in Italian"


def test_no_italian_or_english_reaches_a_slovak_call():
    text = _strip_identifiers(_model_facing_text("sk")).lower()

    for word in FOREIGN_IN_SLOVAK:
        assert word not in text, f"{word!r} reaches the Slovak model"


def test_no_slovak_reaches_an_italian_call():
    """The reverse leak. Cheaper to catch than to hear."""
    found = SLOVAK_ONLY.findall(_strip_identifiers(_model_facing_text("it")))

    assert not found, f"Slovak letters in the Italian prompt: {set(found)}"


@pytest.mark.parametrize("locale", LOCALES)
def test_the_prompt_promises_no_tool_it_lacks(locale):
    """Realtime models are eager: name a tool that isn't in the tools list and
    they will invent it or pretend the action succeeded. So every snake_case
    identifier the prompt mentions has to be a tool that actually exists."""
    prompt = router._build_system_prompt(
        router._content(locale), "Štúdio Demo", "Apollonia"
    )
    available = {t["name"] for t in router._content(locale)["tools"]}
    params = {
        name
        for t in router._content(locale)["tools"]
        for name in t["parameters"]["properties"]
    }

    mentioned = set(re.findall(r"\b[a-z]+_[a-z_]+\b", prompt))

    assert mentioned <= available | params, mentioned - available - params

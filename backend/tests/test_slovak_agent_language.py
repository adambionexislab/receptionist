"""Everything the Slovak realtime session carries has to be Slovak.

The model reads the tool schemas on every turn, just like the instructions, so
English prose there is not inert: it keeps English orthography live in her
context and comes back out as English pronunciation of shared words mid-call.
The tool results are the same story one step later — they are the last thing she
reads before describing a property out loud.

So these tests guard the whole payload of a Slovak call, not just the prompt:
accept config instructions + tools, and the projected tool results. Identifiers
are the deliberate exception — tool names, field names and the internal
"vendita"/"affitto" / "normale"/"urgente" tokens are what the handlers dispatch
on, and she never says them aloud.
"""

import json
import re

import pytest

from call import router

# Identifiers that legitimately appear in the Slovak payload: tool names, JSON
# field names, schema keywords and the internal enum tokens.
ALLOWED = {
    # tool + field names
    "search_listings", "get_listing_by_address", "mark_listing_interest",
    "record_caller_info", "leave_message", "end_call",
    "address_query", "address", "caller_name", "phone", "message", "urgency",
    "name", "zone", "rooms_min", "rooms_max", "max_price", "employment_status",
    "monthly_income", "household_size", "has_pets", "move_in_date",
    "has_mortgage_preapproval", "has_property_to_sell", "sale_timeline",
    "visit_availability",
    # JSON-schema vocabulary
    "type", "description", "parameters", "properties", "required", "enum",
    "function", "object", "string", "integer", "boolean",
    # internal tokens, explained in the schema as never-spoken
    "vendita", "affitto", "normale", "urgente",
}

# Words that would mean English or Italian prose leaked back in. Deliberately
# short and common: this is a smoke alarm, not a language detector.
FOREIGN_PROSE = re.compile(
    r"\b("
    r"the|and|or|for|with|when|this|that|caller|listing|listings|property|"
    r"call|rent|buy|number|available|price|rooms|date|month|record|search|"
    r"chiamante|immobile|immobili|richiesta|agente|dipendente|autonomo|"
    r"studente|rogito|affitti|acquisto"
    r")\b",
    re.IGNORECASE,
)


def _slovak_session():
    """The accept config an inbound call to a Slovak tenant actually sends."""
    content = router._content("sk")
    instructions = router._build_system_prompt(content, "Realitná kancelária", None)
    return router._build_accept_config(instructions, content)


def _prose(payload):
    """The payload with every identifier stripped, leaving only prose."""
    words = re.findall(r"[A-Za-z_][A-Za-z_]*", json.dumps(payload, ensure_ascii=False))
    return " ".join(w for w in words if w not in ALLOWED)


def test_the_slovak_session_carries_slovak_tools():
    """The locale picks the tool schemas, not just the instructions."""
    session = _slovak_session()

    assert session["tools"] is not router._IT_TOOLS
    assert [t["name"] for t in session["tools"]] == [
        t["name"] for t in router._IT_TOOLS
    ]


def test_the_slovak_tool_schemas_have_no_foreign_prose():
    leaked = FOREIGN_PROSE.findall(_prose(_slovak_session()["tools"]))

    assert not leaked, f"foreign words in the Slovak tool schemas: {sorted(set(leaked))}"


def test_the_slovak_instructions_have_no_foreign_prose():
    leaked = FOREIGN_PROSE.findall(_prose(_slovak_session()["instructions"]))

    assert not leaked, f"foreign words in the Slovak prompt: {sorted(set(leaked))}"


@pytest.mark.parametrize("locale", ("it", "sk"))
def test_every_locale_carries_its_own_tools_and_field_map(locale):
    """Both are read straight out of the content dict at call time; a missing
    key would raise on accept, before the call is even answered."""
    content = router._content(locale)

    assert len(content["tools"]) == len(router._IT_TOOLS)
    assert set(content["model_listing_fields"]) == set(router._MODEL_LISTING_FIELDS)


def test_the_slovak_tool_schemas_match_the_italian_ones_structurally():
    """Translating the descriptions must not drift the contract the handlers in
    call/router.py dispatch on."""
    def shape(tool):
        params = tool["parameters"]
        return (
            tool["name"],
            sorted(params["properties"]),
            params["required"],
            {k: (v["type"], tuple(v.get("enum") or ()))
             for k, v in params["properties"].items()},
        )

    sk_tools = router._content("sk")["tools"]
    assert [shape(t) for t in sk_tools] == [shape(t) for t in router._IT_TOOLS]

"""Tests for how Apollonia closes a call (call/router.py, call/locales.py).

Regression origin: she kept announcing the hang-up instead of saying goodbye —
"Ďakujem, ukončím hovor po rozlúčke." The prompt forbade exactly that, in five
separate places, and she still did it sometimes: the same prompt also tells her
to announce an action before every slow tool call, and that habit won often
enough to matter.

The fix is structural rather than another prohibition. She now calls end_call
silently, and the goodbye is a separate response whose `instructions` replace
the session prompt for that one turn — so the announcing habit is not in
context when the farewell is generated. These tests guard both halves: that the
farewell turn is built the way that mechanism requires, and that the main
prompt never goes back to asking her to speak the goodbye herself.
"""

import pytest

from call import router

LOCALES = ("it", "sk")

# Phrases that announce the hang-up instead of performing it — what she actually
# said on the calls that prompted this, plus the near variants.
ANNOUNCEMENTS = {
    "it": ("ora riaggancio", "chiudo la chiamata", "la saluto"),
    "sk": ("ukončím hovor", "teraz zložím", "rozlúčim sa"),
}


@pytest.mark.parametrize("locale", LOCALES)
def test_farewell_turn_replaces_the_session_prompt(locale):
    """The whole mechanism: per-response instructions override the session
    prompt, so the preamble habit that leaked the announcement is not in
    context for the goodbye."""
    content = router._content(locale)
    event = router._farewell_response_event(content)

    assert event["type"] == "response.create"
    assert event["response"]["instructions"] == content["farewell_instruction"]
    assert event["response"]["instructions"]


@pytest.mark.parametrize("locale", LOCALES)
def test_farewell_turn_cannot_answer_with_another_tool_call(locale):
    """Without this she can respond to the request for a goodbye by calling
    end_call again, and the caller hears silence before the line drops."""
    event = router._farewell_response_event(router._content(locale))

    assert event["response"]["tool_choice"] == "none"


@pytest.mark.parametrize("locale", LOCALES)
def test_farewell_instruction_forbids_announcing_the_hangup(locale):
    """It is a short, single-purpose instruction, so it can afford to name the
    exact phrases that went wrong."""
    instruction = router._content(locale)["farewell_instruction"].lower()

    assert any(phrase in instruction for phrase in ANNOUNCEMENTS[locale])


@pytest.mark.parametrize("locale", LOCALES)
def test_farewell_instruction_keeps_the_callers_language(locale):
    """A caller who switched language mid-call must not be said goodbye to in
    the tenant's language — the session prompt's language rule does not apply
    to this turn, so the farewell instruction has to carry it itself."""
    instruction = router._content(locale)["farewell_instruction"].lower()

    marker = {"it": "lingua", "sk": "jazyk"}[locale]
    assert marker in instruction


@pytest.mark.parametrize("locale", LOCALES)
def test_the_main_prompt_no_longer_asks_her_to_speak_the_goodbye(locale):
    """The competing instruction. If a future edit reinstates "say the farewell
    words, then call end_call", the model is back to composing the goodbye
    under the full prompt and this bug returns."""
    content = router._content(locale)
    prompt = content["system_prompt_body"].lower()

    reinstated = {
        "it": ("pronuncia le vere parole di saluto", "pronuncia le parole di saluto"),
        "sk": ("vyslovte slová rozlúčky", "vyslovte skutočné slová rozlúčky"),
    }[locale]
    assert not any(phrase in prompt for phrase in reinstated)


@pytest.mark.parametrize("locale", LOCALES)
def test_the_main_prompt_tells_her_to_call_end_call_silently(locale):
    prompt = router._content(locale)["system_prompt_body"].lower()

    marker = {"it": "in silenzio", "sk": "mlčky"}[locale]
    assert marker in prompt


def test_end_call_tool_description_does_not_ask_for_a_spoken_goodbye():
    """The tool description is in context at exactly the moment she decides to
    end the call, so a stale 'say goodbye first' here would outweigh the prompt."""
    description = router._END_CALL_TOOL["description"].lower()

    assert "do not say goodbye" in description
    assert "say the actual goodbye words" not in description


def test_farewell_has_a_timeout_shorter_than_the_silence_watchdog():
    """If the farewell response never arrives, the caller sits on an open line.
    The 100s silence watchdog is far too long a backstop for a call that has
    already been ended."""
    assert 0 < router._FAREWELL_TIMEOUT_SECONDS < 100

"""Tests for checking the caller is still there (call/router.py).

Regression origin: she asked a question, the caller said nothing, and she waited
in silence until they hung up:

    23:45:45  Apollonia: Ako vám môžem pomôcť?
              ...22 seconds of nothing...
    23:46:07  Call ended (control WebSocket closed)

The dead-air nudge could not cover this. It fires on `awaiting_reply_since`,
which is only set when a caller turn COMPLETES — so it handles "the caller spoke
and got nothing back" and is structurally unable to handle the mirror case,
"she spoke and got nothing back". Nothing was owed, so nothing fired, and the
line just ran toward the 100s hang-up.
"""

import pytest

from call import router
from call.router import _CALLER_SILENCE_SECONDS as QUIET
from call.router import _MAX_SILENCE_PROMPTS, _should_check_caller_is_there

LOCALES = ("it", "sk")


def _session(**overrides):
    session = {
        "last_speech_at": 100.0,
        "awaiting_reply_since": None,
        "ending_at": None,
        "silence_prompts": 0,
    }
    session.update(overrides)
    return session


def test_asks_once_the_line_has_been_quiet():
    assert _should_check_caller_is_there(_session(), False, 100.0 + QUIET + 0.1)


def test_waits_out_a_normal_pause():
    """A caller thinking about their budget is not a caller who has gone. This
    is the whole risk of the feature: ask too early and she talks over someone
    who was about to answer."""
    assert not _should_check_caller_is_there(_session(), False, 100.0 + QUIET - 0.1)


def test_stays_quiet_while_she_is_still_speaking():
    """The clock starts from her transcript, which lands before her audio has
    finished playing out — so an open response means she is very probably still
    audible on the line."""
    assert not _should_check_caller_is_there(_session(), True, 100.0 + QUIET + 10.0)


def test_stays_quiet_when_a_reply_is_already_owed():
    """The caller HAS spoken and is waiting on her: that is the nudge's case.
    Both firing would have her speak twice, the second time asking whether
    someone who just talked is still there."""
    session = _session(awaiting_reply_since=100.0)

    assert not _should_check_caller_is_there(session, False, 100.0 + QUIET + 10.0)


def test_stays_quiet_once_the_call_is_ending():
    """After end_call the farewell owns the next turn."""
    session = _session(ending_at=101.0)

    assert not _should_check_caller_is_there(session, False, 100.0 + QUIET + 10.0)


def test_asks_only_once():
    """A caller who does not answer "are you still there?" is gone, on hold, or
    does not want to talk. Asking again is what makes an agent feel like a
    machine, so the line is left to run out to the hang-up instead."""
    session = _session(silence_prompts=_MAX_SILENCE_PROMPTS)

    assert not _should_check_caller_is_there(session, False, 100.0 + QUIET + 60.0)


def test_the_pause_allows_for_audio_still_draining():
    """The threshold is measured from her transcript event, not from when she
    stops being audible, and over SIP those differ by seconds. A value tuned as
    though the clock started at end-of-audio would cut callers off."""
    assert QUIET >= 10.0


@pytest.mark.parametrize("locale", LOCALES)
def test_the_instruction_exists_in_every_locale(locale):
    """Spoken aloud, so it has to be the tenant's language — and it replaces the
    system prompt for that turn, which is what stops her treating the silence as
    a cue to re-ask the question the caller just failed to answer."""
    event = router._silence_check_response_event(router._content(locale))

    assert event["response"]["instructions"].strip()
    assert event["response"]["tool_choice"] == "none"


def test_the_slovak_instruction_is_slovak():
    text = router._content("sk")["silence_check_instruction"]

    assert any(ch in text for ch in "ľščťžý")

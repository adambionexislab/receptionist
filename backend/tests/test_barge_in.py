"""Tests for holding barge-in off during the opening (call/router.py).

Regression origin: something at pickup — line noise, the connect tone, or her
own voice echoing back — scored above the VAD threshold and cancelled the
greeting, over and over:

    21:37:07,658  Apollonia: Dobrý deň, volám sa Apollonia, ... Ako vám môžem pomô
    21:37:07,739  Caller interrupted — response cancelled
    21:37:08,269  Caller finished speaking
    21:37:10,177  Apollonia: Ospravedlňujem sa, nič zrozumiteľné som nepočula.

She reported hearing nothing intelligible, so that was not a caller talking.
The restart itself is the prompt working as intended — an interrupted AI
disclosure has not legally been made, so she starts the sentence over — which
is why the fix is upstream: the opening now starts with interrupt_response
FALSE and the control socket hands barge-in back a few seconds later.

The two things these tests pin are the ones that would fail silently in
production: shipping the "off" to a client that can never turn it back on, and
letting the re-arm event quietly reset the rest of the VAD tuning.
"""

import pytest

from call.router import _ARM_BARGE_IN, _BARGE_IN_ARM_SECONDS as ARM
from call.router import _IT_CONTENT, _SESSION_UPDATE, _build_accept_config
from call.router import _should_arm_barge_in
from demo.router import _DEMO_TURN_DETECTION

_PHONE_VAD = _SESSION_UPDATE["session"]["audio"]["input"]["turn_detection"]
_ARM_VAD = _ARM_BARGE_IN["session"]["audio"]["input"]["turn_detection"]


def _session(**overrides):
    session = {"greeting_at": None, "barge_in_armed": False}
    session.update(overrides)
    return session


# ── The session the phone call is accepted with ──────────────────────────────


def test_the_opening_cannot_be_interrupted():
    assert _PHONE_VAD["interrupt_response"] is False


def test_nothing_else_may_speak_during_the_opening():
    """interrupt_response alone was not enough. It stopped the greeting response
    being cancelled — so the "Caller interrupted" log line vanished and it read
    as fixed — but create_response still fired at the end of the caller's turn,
    and that second response's audio displaced the greeting still playing out
    over SIP. The caller heard the same truncation, and the log hid it, because
    the transcript event reports generated text, not what was played."""
    assert _PHONE_VAD["create_response"] is False


def test_the_accepted_sip_session_carries_it():
    """_build_accept_config rebuilds the session for SIP and strips the PCM
    format fields; the VAD tuning has to survive that trip, or the greeting
    ships unprotected."""
    accepted = _build_accept_config("prompt", _IT_CONTENT)

    assert accepted["audio"]["input"]["turn_detection"]["interrupt_response"] is False


# ── Handing barge-in back ────────────────────────────────────────────────────


def test_the_arm_event_turns_interruption_back_on():
    assert _ARM_VAD["interrupt_response"] is True


def test_the_arm_event_turns_auto_response_back_on():
    """Both flags go off together for the opening and have to come back
    together: leaving create_response off would mean every later caller turn
    sits unanswered until the nudge watchdog notices, four seconds of dead air
    per turn for the whole call."""
    assert _ARM_VAD["create_response"] is True


def test_the_arm_event_preserves_the_rest_of_the_vad_tuning():
    """session.update REPLACES a nested object rather than merging into it, so
    an event carrying only the changed flag would reset threshold and
    silence_duration_ms to the API defaults a few seconds into every call —
    retuning endpointing mid-conversation, and doing it invisibly."""
    for field in ("type", "threshold", "prefix_padding_ms", "silence_duration_ms"):
        assert _ARM_VAD[field] == _PHONE_VAD[field], field


def test_the_arm_event_is_a_copy_not_the_live_session_dict():
    """Built from the template with **, so mutating one must not reach the
    other — otherwise arming a single call would flip the flag for every call
    accepted afterwards."""
    assert _ARM_VAD is not _PHONE_VAD


# ── When the re-arm fires ────────────────────────────────────────────────────


def test_arms_once_the_greeting_has_had_time_to_land():
    assert _should_arm_barge_in(_session(greeting_at=100.0), 100.0 + ARM + 0.1)


def test_stays_off_while_the_greeting_is_still_playing():
    """response.done only means generation finished — the audio drains down the
    SIP leg for seconds after it. Arming inside that window puts the greeting
    back in reach of whatever was cutting it."""
    assert not _should_arm_barge_in(_session(greeting_at=100.0), 100.0 + ARM - 0.1)


def test_stays_off_before_the_greeting_is_triggered():
    assert not _should_arm_barge_in(_session(), 10_000.0)


def test_arms_only_once():
    """The watchdog re-checks every second for the life of the call; without
    this the same session.update would go out on every tick."""
    session = _session(greeting_at=100.0)
    late = 100.0 + ARM + 10.0

    assert _should_arm_barge_in(session, late)
    session["barge_in_armed"] = True
    assert not _should_arm_barge_in(session, late)


def test_loop_clock_zero_still_arms():
    """`greeting_at` is an event-loop timestamp, not a duration, and a falsy
    test on it would leave that call uninterruptible from end to end."""
    assert _should_arm_barge_in(_session(greeting_at=0.0), ARM + 0.1)


def test_the_window_outlasts_a_spoken_greeting():
    """The opening is a full sentence plus the AI disclosure — several seconds
    of audio. A window shorter than that re-arms while she is still talking."""
    assert ARM >= 6.0


# ── The browser demo, which has no control socket ────────────────────────────


@pytest.mark.parametrize("flag", ["interrupt_response", "create_response"])
def test_the_demo_does_not_inherit_the_off_switches(flag):
    """The demo browser talks straight to OpenAI, so nothing on our side can
    send it the re-arm. Inheriting interrupt_response=False would leave the demo
    uninterruptible; inheriting create_response=False would leave it silent —
    no turn would ever produce a reply. Parametrised rather than asserted once
    because the second flag was added later and the demo kept stripping only
    the first, which is exactly the bug this guards."""
    assert flag not in _DEMO_TURN_DETECTION


def test_the_demo_still_inherits_the_tuned_vad():
    for field in ("type", "threshold", "prefix_padding_ms", "silence_duration_ms"):
        assert _DEMO_TURN_DETECTION[field] == _PHONE_VAD[field], field


@pytest.mark.parametrize("mutated", ["threshold", "silence_duration_ms"])
def test_the_demo_config_is_a_copy(mutated):
    """A dict comprehension, not a reference to the live session config."""
    assert _DEMO_TURN_DETECTION is not _PHONE_VAD
    assert mutated in _DEMO_TURN_DETECTION

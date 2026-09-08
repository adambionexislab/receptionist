"""Tests for the dead-air recovery in call/router.py.

Regression origin: the caller answers a question and Apollonia says nothing, so
the caller has to speak again to get her going:

    15:59:45  Apollonia: ...Môžem vám ešte s niečím pomôcť?
    15:59:54  Caller speaking          <- answered; no reply came
    16:00:02  Caller speaking          <- had to prod her, 8.6s later
    16:00:04  Apollonia: Dobre, ...

A dropped turn can happen several ways — a commit that produced no response, a
response that ended without audio, an interruption that cancelled the reply —
and on a phone call all of them sound identical: silence. So rather than
guessing which, the watchdog asks for the reply itself once the caller has been
left hanging. These tests pin the conditions under which it stays quiet, since
speaking at the wrong moment would talk over her.
"""

import pytest

from call.router import _REPLY_NUDGE_SECONDS as NUDGE
from call.router import _should_nudge_reply


def _session(**overrides):
    """A normal mid-call session. barge_in_armed is True because every turn
    except the opening is one: the greeting window is the exception, and it has
    its own test below."""
    session = {
        "awaiting_reply_since": None,
        "ending_at": None,
        "barge_in_armed": True,
    }
    session.update(overrides)
    return session


def test_nudges_when_the_caller_has_been_left_hanging():
    session = _session(awaiting_reply_since=100.0)

    assert _should_nudge_reply(session, False, 100.0 + NUDGE + 0.1)


def test_stays_quiet_while_nothing_is_owed():
    """No completed caller turn means no reply is outstanding."""
    assert not _should_nudge_reply(_session(), False, 10_000.0)


def test_stays_quiet_while_she_is_still_generating():
    """An open response is just her thinking. Nudging here would queue a second
    turn behind the one already coming and make her answer twice."""
    session = _session(awaiting_reply_since=100.0)

    assert not _should_nudge_reply(session, True, 100.0 + NUDGE + 10.0)


def test_stays_quiet_before_the_grace_period_is_up():
    session = _session(awaiting_reply_since=100.0)

    assert not _should_nudge_reply(session, False, 100.0 + NUDGE - 0.1)


def test_stays_quiet_while_the_opening_is_still_playing():
    """The greeting runs with create_response=False, so anything that scored as
    speech during it is committed and deliberately unanswered. Nudging would
    answer it — talking over the tail of the AI disclosure to reply to what was
    most likely line noise. The greeting's own "how can I help you?" is the
    prompt a real caller needs."""
    session = _session(awaiting_reply_since=100.0, barge_in_armed=False)

    assert not _should_nudge_reply(session, False, 100.0 + NUDGE + 10.0)


def test_the_nudge_resumes_once_barge_in_is_armed():
    """Only the opening is excluded, not the rest of the call — the dead-air
    recovery this whole module exists for has to still work afterwards."""
    session = _session(awaiting_reply_since=100.0, barge_in_armed=True)

    assert _should_nudge_reply(session, False, 100.0 + NUDGE + 0.1)


def test_stays_quiet_once_the_call_is_ending():
    """After end_call the farewell owns the next turn; a nudge would race it."""
    session = _session(awaiting_reply_since=100.0, ending_at=101.0)

    assert not _should_nudge_reply(session, False, 100.0 + NUDGE + 10.0)


def test_a_response_that_arrives_late_cancels_the_nudge():
    """The handler clears the flag when she finally speaks, so a slow reply must
    not be followed by a redundant nudge."""
    session = _session(awaiting_reply_since=100.0)
    session["awaiting_reply_since"] = None  # response.output_audio_transcript.done

    assert not _should_nudge_reply(session, False, 100.0 + NUDGE + 10.0)


def test_the_caller_speaking_again_cancels_the_nudge():
    """They gave up waiting and spoke first — their new turn is what matters,
    and nudging now would collide with it."""
    session = _session(awaiting_reply_since=100.0)
    session["awaiting_reply_since"] = None  # input_audio_buffer.speech_started

    assert not _should_nudge_reply(session, False, 100.0 + NUDGE + 10.0)


def test_the_grace_period_leaves_room_for_a_normal_reply():
    """Her observed latency after a caller turn is a second or two. Nudging
    inside that window would cut across ordinary thinking time."""
    assert NUDGE >= 3.0


@pytest.mark.parametrize("awaiting", [0, None, False])
def test_falsy_timestamps_are_treated_as_nothing_owed(awaiting):
    """Loop-clock zero is not a pending turn — it is an uninitialised session."""
    assert not _should_nudge_reply(_session(awaiting_reply_since=awaiting), False, 1e6)

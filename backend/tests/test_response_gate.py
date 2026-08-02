"""Tests for _ResponseGate — serialising response.create against the Realtime
response lifecycle (call/router.py).

Regression origin: two symptoms, one cause. After a tool call Apollonia would
sometimes just stop and wait, and only resume once the caller spoke; and after
end_call the farewell never came, so the 12s watchdog cut the line:

    15:20:58 Apollonia: Zapíšem si vaše údaje, moment.
    15:20:58 Recorded caller info: {...}
    15:21:09 Caller speaking              <- 11s of dead air, caller prompting her
    15:21:12 Apollonia: Vašu požiadavku odovzdám...
    ...
    15:21:21 Apollonia ending call
    15:21:33 WARNING No farewell within 12s of end_call

Both were the code answering a tool result with response.create while the
response that tool call came from was still open. The API rejects that and
generates nothing, so the turn is simply lost — the caller hears silence until
VAD opens a new one. The gate holds such requests until response.done.
"""

import pytest

from call import router


class FakeSocket:
    """Records the events the gate decides to send, in order."""

    def __init__(self):
        self.sent = []

    async def send(self, event):
        self.sent.append(event)


@pytest.fixture
def socket():
    return FakeSocket()


@pytest.fixture
def gate(socket):
    return router._ResponseGate(socket.send)


async def test_request_goes_straight_out_when_no_response_is_open(gate, socket):
    await gate.request()

    assert socket.sent == [{"type": "response.create"}]


async def test_request_made_during_a_response_is_held_back(gate, socket):
    """The exact failure: a tool call arrives mid-response, and replying to it
    immediately is what the API throws away."""
    gate.opened()

    await gate.request()

    assert socket.sent == []


async def test_held_request_is_sent_when_the_response_finishes(gate, socket):
    gate.opened()
    await gate.request()

    await gate.closed()

    assert socket.sent == [{"type": "response.create"}]


async def test_nothing_is_sent_twice(gate, socket):
    """A second response.done must not replay the turn — that would talk over
    her next utterance."""
    gate.opened()
    await gate.request()
    await gate.closed()

    await gate.closed()

    assert socket.sent == [{"type": "response.create"}]


async def test_response_done_with_nothing_queued_sends_nothing(gate, socket):
    gate.opened()

    await gate.closed()

    assert socket.sent == []


async def test_several_tool_results_in_one_response_collapse_to_one_turn(gate, socket):
    """Two tool calls inside a single response still warrant one reply, so the
    newest request supersedes the older rather than queueing both."""
    gate.opened()
    await gate.request()
    await gate.request({"type": "response.create", "response": {"tool_choice": "none"}})

    await gate.closed()

    assert socket.sent == [
        {"type": "response.create", "response": {"tool_choice": "none"}}
    ]


async def test_the_farewell_is_marked_only_once_it_actually_goes_out(gate, socket):
    """end_call fires mid-response, so the goodbye waits. Marking the call as
    ending at request time would let a trailing phase of that same response be
    taken for the farewell and cut her off mid-sentence."""
    marked = []
    gate.opened()

    await gate.request({"type": "response.create"}, on_sent=lambda: marked.append(True))
    assert marked == []  # still queued behind the end_call response

    await gate.closed()
    assert marked == [True]


async def test_an_immediate_farewell_is_marked_right_away(gate, socket):
    """When no response is open the goodbye goes out at once, and the flag has
    to keep pace or the hang-up never triggers."""
    marked = []

    await gate.request({"type": "response.create"}, on_sent=lambda: marked.append(True))

    assert marked == [True]
    assert len(socket.sent) == 1


async def test_the_full_end_call_sequence_produces_exactly_one_farewell(gate, socket):
    """End to end, as the events actually arrive on the wire."""
    content = router._content("sk")
    marked = []

    gate.opened()  # response.created — the turn containing the end_call
    # response.function_call_arguments.done → end_call handler
    await gate.request(
        router._farewell_response_event(content), on_sent=lambda: marked.append(True)
    )
    assert socket.sent == []  # nothing sent into the open response

    await gate.closed()  # response.done of that turn
    gate.opened()  # response.created — the farewell itself

    assert marked == [True]
    assert socket.sent == [router._farewell_response_event(content)]
    assert socket.sent[0]["response"]["tool_choice"] == "none"

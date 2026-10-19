"""Turn-taking rules, copied from the OpenAI SIP call loop.

COPIED FROM backend/call/router.py at commit 9a70526 (_ResponseGate,
_should_nudge_reply, _REPLY_NUDGE_SECONDS, _FAREWELL_TIMEOUT_SECONDS), unchanged
apart from the names, so both engines take turns the same way. A change to
those in router.py gets a "port to the agent?" check (roadmap §6). The agent
can't import them: it runs on the VM, without the backend.
"""

from collections.abc import Awaitable, Callable
from typing import Any

# How long to wait for the farewell response after end_call before hanging up
# regardless: generous for a one-sentence goodbye, short enough that a failed
# response never leaves the caller listening to nothing.
FAREWELL_TIMEOUT_SECONDS = 12.0

# How long after the caller stops talking we let silence run before asking for
# a reply ourselves. Only reached when NO response is in flight, so this cannot
# talk over her; it catches a turn dropped outright.
REPLY_NUDGE_SECONDS = 4.0

# The line has been quiet this long (no caller speech, nothing from her): hang up.
SILENCE_HANGUP_SECONDS = 100.0


def should_nudge_reply(state: dict[str, Any], response_active: bool, now: float) -> bool:
    """Whether the caller has been left hanging long enough that we should ask
    for a reply on their behalf. Every condition is a reason NOT to speak:
    nothing is owed, she is already generating an answer, or the call is
    ending and the farewell owns the next turn."""
    awaiting = state.get("awaiting_reply_since")
    if not awaiting or response_active or state.get("ending_at"):
        return False
    return now - awaiting > REPLY_NUDGE_SECONDS


class ResponseGate:
    """Serialises `response.create` against the Realtime response lifecycle.

    Asking for a response while one is open generates nothing, and tool calls
    arrive mid-response, so a reply to a tool result is held until
    `response.done`. Only the most recent request is held."""

    def __init__(self, send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._send = send
        self.active = False
        self._pending: tuple[dict[str, Any], Callable[[], None] | None] | None = None

    def opened(self) -> None:
        """A `response.created` arrived."""
        self.active = True

    async def closed(self) -> None:
        """A `response.done` arrived: release whatever queued behind it."""
        self.active = False
        pending, self._pending = self._pending, None
        if pending is not None:
            await self._dispatch(*pending)

    async def request(
        self,
        event: dict[str, Any] | None = None,
        *,
        on_sent: Callable[[], None] | None = None,
    ) -> None:
        """Ask for a model turn now, or as soon as the open one finishes.
        `on_sent` fires when the request actually goes out."""
        event = event or {"type": "response.create"}
        if self.active:
            self._pending = (event, on_sent)
            return
        await self._dispatch(event, on_sent)

    async def _dispatch(
        self, event: dict[str, Any], on_sent: Callable[[], None] | None
    ) -> None:
        await self._send(event)
        if on_sent is not None:
            on_sent()

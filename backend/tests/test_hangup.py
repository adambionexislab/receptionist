"""Tests for _hangup_call / _reject_call (call/router.py).

Regression origin: end_call ran, the log said "Hung up call rtc_u1_…", and the
phone stayed connected — every time, until the caller hung up themselves.

The log was lying. httpx does not raise on 4xx/5xx, so:

    await client.post(...)                     # Response(4xx) — no exception
    logger.info("Hung up call %s", call_id)    # logged regardless

meant a rejected hangup was indistinguishable from a successful one. Sibling
_accept_call had always checked resp.status_code; hangup and reject never did.
"""

import httpx
import pytest

from call import router


class FakeClient:
    """Stands in for httpx.AsyncClient, replaying queued responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, text=f"body {outcome}")


@pytest.fixture
def fake_http(monkeypatch):
    def install(*responses):
        client = FakeClient(responses)
        monkeypatch.setattr(router.httpx, "AsyncClient", client)
        return client

    return install


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """Keep the retry backoff out of the test runtime without patching
    asyncio.sleep itself, which the event loop and pytest-asyncio also use."""
    monkeypatch.setattr(router, "_HANGUP_RETRY_DELAY", 0)


async def test_a_successful_hangup_reports_success(fake_http):
    client = fake_http(200)

    assert await router._hangup_call("rtc_1") is True
    assert len(client.calls) == 1
    assert client.calls[0][0].endswith("/calls/rtc_1/hangup")


async def test_a_rejected_hangup_is_not_reported_as_done(fake_http):
    """The whole bug: a 4xx used to log 'Hung up call' and return."""
    fake_http(400, 400, 400)

    assert await router._hangup_call("rtc_1") is False


async def test_a_failing_hangup_is_retried(fake_http):
    """A live call is worth more than one attempt — the caller is stuck on the
    line until someone tears it down."""
    client = fake_http(500, 500, 200)

    assert await router._hangup_call("rtc_1") is True
    assert len(client.calls) == router._HANGUP_ATTEMPTS


async def test_retries_stop_as_soon_as_one_succeeds(fake_http):
    client = fake_http(500, 200)

    assert await router._hangup_call("rtc_1") is True
    assert len(client.calls) == 2


async def test_a_transport_error_is_retried_too(fake_http):
    client = fake_http(httpx.ConnectError("boom"), 200)

    assert await router._hangup_call("rtc_1") is True
    assert len(client.calls) == 2


async def test_an_already_ended_call_counts_as_down_and_stops_retrying(fake_http):
    """404 means the caller hung up first. Retrying that would just log noise
    on every normal end of call."""
    client = fake_http(404)

    assert await router._hangup_call("rtc_1") is True
    assert len(client.calls) == 1


async def test_no_call_id_is_not_reported_as_hung_up(fake_http):
    client = fake_http()

    assert await router._hangup_call("") is False
    assert client.calls == []


async def test_hangup_sends_the_json_content_type(fake_http):
    """Matching accept/reject, which both send it."""
    client = fake_http(200)
    await router._hangup_call("rtc_1")

    headers = client.calls[0][1]["headers"]
    assert headers["Content-Type"] == "application/json"


async def test_a_failed_reject_is_not_logged_as_rejected(fake_http, caplog):
    fake_http(400)

    await router._reject_call("rtc_1")

    assert "Reject failed" in caplog.text
    assert "Rejected SIP call" not in caplog.text

"""Registration before answer, the loopback HTTP endpoint, and end reporting."""

import asyncio
import json

import pytest

from voiceagent import config, reporting
from voiceagent.render_api import RenderError
from voiceagent.reporting import EndReporter
from voiceagent.server import Agent

CFG = config.load({
    "OPENAI_API_KEY": "o", "ELEVENLABS_API_KEY": "e",
    "RENDER_API_BASE": "https://example.test", "VOICE_AGENT_TOKEN": "t",
    "ELEVENLABS_VOICE_ID_SK": "v",
})


class FakeRender:
    def __init__(self, answer=None, error=None, delay=0.0):
        self.answer, self.error, self.delay = answer, error, delay
        self.started = []
        self.ended = []
        self.end_failures = 0

    async def start_call(self, call_uuid, caller, dialed, diversion, timeout=1.5):
        self.started.append((call_uuid, caller, dialed, diversion))
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.answer

    async def end(self, call_uuid, payload, timeout=10.0):
        if self.end_failures:
            self.end_failures -= 1
            raise RenderError("down")
        self.ended.append((call_uuid, payload))


def _agent(render):
    return Agent(CFG, render, EndReporter(render, "/nonexistent"))


async def test_ours_registers_a_pending_call():
    render = FakeRender({"decision": "eleven", "session": {"instructions": "p"}})
    agent = _agent(render)
    result = await agent.register("421903123456", "421482304191", "")
    assert result.startswith("ok:")
    call_uuid = result[3:]
    assert render.started[0][0] == call_uuid  # Render sees the same UUID
    assert agent.pending[call_uuid].dialed == "421482304191"


@pytest.mark.parametrize("decision", ["decline", "realtime"])
async def test_other_decisions_pass_through_without_a_pending_call(decision):
    agent = _agent(FakeRender({"decision": decision}))
    assert await agent.register("a", "b", "") == decision
    assert agent.pending == {}


async def test_render_down_means_fallback():
    agent = _agent(FakeRender(error=RenderError("timeout")))
    assert await agent.register("a", "b", "") == "realtime"


async def test_slow_render_means_fallback_within_the_deadline(monkeypatch):
    from voiceagent import server
    monkeypatch.setattr(server, "_RENDER_DEADLINE_SECONDS", 0.05)
    agent = _agent(FakeRender({"decision": "eleven", "session": {}}, delay=1))
    assert await agent.register("a", "b", "") == "realtime"


async def test_http_endpoint_on_loopback():
    agent = _agent(FakeRender({"decision": "decline"}))
    srv = await asyncio.start_server(agent.handle_http, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]

    async def request(raw: bytes) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(raw)
        await writer.drain()
        data = await reader.read()
        writer.close()
        return data

    body = b"from=421903123456&to=421482304191&diversion="
    resp = await request(
        b"POST /calls HTTP/1.1\r\nHost: x\r\nContent-Type: application/x-www-form-urlencoded\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )
    assert resp.startswith(b"HTTP/1.1 200") and resp.endswith(b"\r\n\r\ndecline")

    health = await request(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
    assert json.loads(health.split(b"\r\n\r\n", 1)[1]) == {"ok": True, "active_calls": 0, "pending": 0}
    srv.close()


async def test_end_report_retries_then_succeeds(monkeypatch, tmp_path):
    monkeypatch.setattr(reporting, "_BACKOFF", (0, 0, 0))
    render = FakeRender()
    render.end_failures = 2
    rep = EndReporter(render, str(tmp_path))
    rep.submit("c1", {"ended_by": "caller"})
    await rep.drain(5)
    assert render.ended == [("c1", {"ended_by": "caller"})]
    assert list(tmp_path.iterdir()) == []


async def test_end_report_is_spooled_and_replayed(monkeypatch, tmp_path):
    monkeypatch.setattr(reporting, "_BACKOFF", (0, 0))
    render = FakeRender()
    render.end_failures = 5
    rep = EndReporter(render, str(tmp_path))
    rep.submit("c1", {"ended_by": "caller"})
    await rep.drain(5)
    assert (tmp_path / "c1.json").exists() and render.ended == []

    render.end_failures = 0
    assert await rep.replay_spool() == 1
    assert render.ended == [("c1", {"ended_by": "caller"})]
    assert not (tmp_path / "c1.json").exists()

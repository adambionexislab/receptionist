"""Copied turn rules, the session conversion, and config loading."""

import pytest

from voiceagent import config
from voiceagent.callflow import realtime_session
from voiceagent.turns import REPLY_NUDGE_SECONDS, ResponseGate, should_nudge_reply


async def test_gate_holds_requests_while_a_response_is_open():
    sent = []

    async def send(event):
        sent.append(event)

    gate = ResponseGate(send)
    gate.opened()
    marks = []
    await gate.request({"type": "response.create", "x": 1})
    await gate.request({"type": "response.create", "x": 2}, on_sent=lambda: marks.append("sent"))
    assert sent == [] and marks == []
    await gate.closed()
    assert sent == [{"type": "response.create", "x": 2}]  # only the latest
    assert marks == ["sent"]
    await gate.request()
    assert sent[-1] == {"type": "response.create"}


def test_nudge_rules():
    owed = {"awaiting_reply_since": 10.0}
    assert should_nudge_reply(owed, False, 10.0 + REPLY_NUDGE_SECONDS + 0.1)
    assert not should_nudge_reply(owed, True, 100.0)  # she is answering
    assert not should_nudge_reply({**owed, "ending_at": 5.0}, False, 100.0)
    assert not should_nudge_reply({}, False, 100.0)


def test_session_is_render_s_config_with_text_out_and_pcmu_in():
    render_session = {
        "type": "realtime",
        "model": "gpt-realtime-2.1",
        "instructions": "prompt",
        "tools": [{"name": "search_listings"}],
        "tool_choice": "auto",
        "reasoning": {"effort": "low"},
        "audio": {
            "input": {"turn_detection": {"type": "server_vad", "silence_duration_ms": 600}},
            "output": {"voice": "marin"},
        },
    }
    s = realtime_session(render_session)
    assert s["output_modalities"] == ["text"]
    assert s["audio"] == {
        "input": {
            "turn_detection": {"type": "server_vad", "silence_duration_ms": 600},
            "format": {"type": "audio/pcmu"},
        }
    }
    assert s["instructions"] == "prompt" and s["tools"] == [{"name": "search_listings"}]
    assert render_session["audio"]["output"] == {"voice": "marin"}  # input not mutated


ENV = {
    "OPENAI_API_KEY": "o", "ELEVENLABS_API_KEY": "e",
    "RENDER_API_BASE": "https://www.apollon-ia.com/", "VOICE_AGENT_TOKEN": "t",
    "ELEVENLABS_VOICE_ID_SK": "WtA85syCrJwasGeHGH2p", "ELEVENLABS_LANGUAGE_CODE": "sk",
}


def test_config_loads_voices_per_locale():
    cfg = config.load(ENV)
    assert cfg.render_api_base == "https://www.apollon-ia.com"
    assert cfg.voice_for("sk") == "WtA85syCrJwasGeHGH2p"
    assert cfg.voice_for("it") == "WtA85syCrJwasGeHGH2p"  # falls back to sk
    assert cfg.audiosocket_bind == ("127.0.0.1", 9092)


def test_config_refuses_a_base_without_scheme():
    with pytest.raises(SystemExit):
        config.load({**ENV, "RENDER_API_BASE": "www.apollon-ia.com"})


def test_config_names_missing_settings():
    with pytest.raises(SystemExit, match="ELEVENLABS_API_KEY"):
        config.load({k: v for k, v in ENV.items() if k != "ELEVENLABS_API_KEY"})

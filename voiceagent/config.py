"""Settings from the environment (systemd EnvironmentFile=/etc/apollonia-agent.env).

The names match the roadmap's §3 table and the runbook's env template.
"""

import os
from dataclasses import dataclass, field


def _host_port(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host or "127.0.0.1", int(port)


def _float(value: str | None) -> float | None:
    return float(value) if value not in (None, "") else None


@dataclass(frozen=True)
class Config:
    openai_api_key: str
    elevenlabs_api_key: str
    render_api_base: str
    voice_agent_token: str
    openai_realtime_model: str = "gpt-realtime-2.1"
    elevenlabs_model: str = "eleven_v4_turbo"
    elevenlabs_ws_url: str = "wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input"
    elevenlabs_output_format: str = "pcm_8000"
    # Default language when Render's answer carries no locale.
    elevenlabs_language_code: str = "sk"
    # voice per tenant locale: {"sk": "...", "it": "..."}
    voices: dict[str, str] = field(default_factory=dict)
    pronunciation_dict_id: str = ""
    pronunciation_dict_version: str = ""
    stability: float | None = None
    speed: float | None = None
    similarity_boost: float | None = None
    audiosocket_bind: tuple[str, int] = ("127.0.0.1", 9092)
    agent_http_bind: tuple[str, int] = ("127.0.0.1", 9093)
    spool_dir: str = "/var/lib/apollonia-agent/spool"
    # Hard stop for one call; Render keeps a session for 3 h at most.
    max_call_seconds: float = 900.0

    def voice_for(self, locale: str) -> str:
        return self.voices.get(locale) or self.voices.get("sk") or ""


def load(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    missing = [
        k for k in ("OPENAI_API_KEY", "ELEVENLABS_API_KEY", "RENDER_API_BASE", "VOICE_AGENT_TOKEN")
        if not env.get(k)
    ]
    if missing:
        raise SystemExit(f"Missing settings: {', '.join(missing)}")
    base = env["RENDER_API_BASE"].rstrip("/")
    if not base.startswith(("https://", "http://")):
        # Without a scheme every request is plain HTTP and Render answers 307
        # with an empty body (seen in Phase 1).
        raise SystemExit("RENDER_API_BASE needs https:// (e.g. https://www.apollon-ia.com)")
    voices = {
        key.removeprefix("ELEVENLABS_VOICE_ID_").lower(): value
        for key, value in env.items()
        if key.startswith("ELEVENLABS_VOICE_ID_") and value
    }
    return Config(
        openai_api_key=env["OPENAI_API_KEY"],
        elevenlabs_api_key=env["ELEVENLABS_API_KEY"],
        render_api_base=base,
        voice_agent_token=env["VOICE_AGENT_TOKEN"],
        openai_realtime_model=env.get("OPENAI_REALTIME_MODEL") or "gpt-realtime-2.1",
        elevenlabs_model=env.get("ELEVENLABS_MODEL") or "eleven_v4_turbo",
        elevenlabs_ws_url=env.get("ELEVENLABS_WS_URL")
        or "wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input",
        elevenlabs_output_format=env.get("ELEVENLABS_OUTPUT_FORMAT") or "pcm_8000",
        elevenlabs_language_code=env.get("ELEVENLABS_LANGUAGE_CODE") or "sk",
        voices=voices,
        pronunciation_dict_id=env.get("ELEVENLABS_PRONUNCIATION_DICT_ID") or "",
        pronunciation_dict_version=env.get("ELEVENLABS_PRONUNCIATION_DICT_VERSION") or "",
        stability=_float(env.get("ELEVENLABS_STABILITY")),
        speed=_float(env.get("ELEVENLABS_SPEED")),
        similarity_boost=_float(env.get("ELEVENLABS_SIMILARITY_BOOST")),
        audiosocket_bind=_host_port(env.get("AUDIOSOCKET_BIND") or "127.0.0.1:9092"),
        agent_http_bind=_host_port(env.get("AGENT_HTTP_BIND") or "127.0.0.1:9093"),
        spool_dir=env.get("AGENT_SPOOL_DIR") or "/var/lib/apollonia-agent/spool",
        max_call_seconds=float(env.get("MAX_CALL_SECONDS") or 900),
    )

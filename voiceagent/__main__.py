"""python -m voiceagent: the apollonia-agent service.

Restarting the service drops calls in progress on this engine only; deploy at
quiet hours (roadmap §2.7). Calls that arrive while it is down take the
dialplan's fallback to OpenAI SIP.
"""

import asyncio
import logging
import signal

from voiceagent import config, eleven
from voiceagent.render_api import RenderAPI
from voiceagent.reporting import EndReporter
from voiceagent.server import Agent

logger = logging.getLogger("voiceagent")

_WARM_EVERY_SECONDS = 240  # keep the ElevenLabs path warm between calls
_SPOOL_EVERY_SECONDS = 300


async def _every(seconds: float, fn) -> None:
    while True:
        await asyncio.sleep(seconds)
        try:
            await fn()
        except Exception:
            logger.exception("Periodic task failed")


async def main() -> None:
    cfg = config.load()
    logging.getLogger().setLevel(cfg.log_level)
    # Libraries stay at INFO even when the agent debugs: websockets at DEBUG
    # would log every frame, including the audio.
    for noisy in ("websockets", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.INFO)
    if cfg.log_level == "DEBUG":
        logger.info(
            "Debug logging on%s",
            f"; caller transcripts via {cfg.debug_transcribe_model}" if cfg.debug_transcribe_model else "",
        )
    render = RenderAPI(cfg.render_api_base, cfg.voice_agent_token)
    reporter = EndReporter(render, cfg.spool_dir)
    agent = Agent(cfg, render, reporter)

    http = await asyncio.start_server(agent.handle_http, *cfg.agent_http_bind)
    sock = await asyncio.start_server(agent.handle_audiosocket, *cfg.audiosocket_bind)
    logger.info(
        "Listening: registry http://%s:%d, AudioSocket %s:%d",
        *cfg.agent_http_bind, *cfg.audiosocket_bind,
    )

    async def warm() -> None:
        voice = cfg.voice_for(cfg.elevenlabs_language_code)
        if voice:
            ms = await eleven.warm(cfg, voice)
            logger.debug("ElevenLabs warm: %.0f ms", ms)

    await warm()
    await reporter.replay_spool()
    background = [
        asyncio.create_task(_every(_WARM_EVERY_SECONDS, warm)),
        asyncio.create_task(_every(_SPOOL_EVERY_SECONDS, reporter.replay_spool)),
    ]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # not on Windows (tests, local runs)
            pass
    await stop.wait()

    logger.info("Stopping: %d call(s) in progress will be dropped", len(agent.active))
    http.close()
    sock.close()
    for task in background:
        task.cancel()
    for call in list(agent.active.values()):
        call.hang_up("agent_stopping")
    await asyncio.sleep(0.5)  # let the calls report their end
    await reporter.drain(timeout=10)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())

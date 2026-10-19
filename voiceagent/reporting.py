"""Telling Render a call ended, without ever losing it.

POST /internal/voice/calls/{uuid}/end is what sends the lead email, WhatsApp
alert and call row. Render answers 202 at once and treats a repeat as a no-op,
so retrying is always safe. If Render stays unreachable, the payload is written
to the spool directory and retried later (at startup and every few minutes).
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from voiceagent.render_api import RenderAPI, RenderError

logger = logging.getLogger(__name__)

# Seconds between attempts; about 2 minutes in all before spooling.
_BACKOFF = (0, 1, 2, 4, 8, 15, 30, 60)


class EndReporter:
    def __init__(self, render: RenderAPI, spool_dir: str) -> None:
        self.render = render
        self.spool = Path(spool_dir)
        self._tasks: set[asyncio.Task] = set()

    def submit(self, call_uuid: str, payload: dict[str, Any]) -> None:
        task = asyncio.create_task(self._deliver(call_uuid, payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver(self, call_uuid: str, payload: dict[str, Any]) -> None:
        for delay in _BACKOFF:
            if delay:
                await asyncio.sleep(delay)
            try:
                await self.render.end(call_uuid, payload)
                logger.info("Call %s: reported to Render", call_uuid)
                return
            except RenderError as exc:
                logger.warning("Call %s: /end failed (%s), retrying", call_uuid, exc)
        self._write_spool(call_uuid, payload)

    def _write_spool(self, call_uuid: str, payload: dict[str, Any]) -> None:
        try:
            self.spool.mkdir(parents=True, exist_ok=True)
            path = self.spool / f"{call_uuid}.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False))
            os.replace(tmp, path)
            logger.error("Call %s: Render unreachable — spooled to %s", call_uuid, path)
        except OSError:
            logger.exception("Call %s: could not spool the end report: %s", call_uuid, payload)

    async def replay_spool(self) -> int:
        """Retry every spooled report once; delete the ones Render accepted."""
        if not self.spool.is_dir():
            return 0
        sent = 0
        for path in sorted(self.spool.glob("*.json")):
            try:
                payload = json.loads(path.read_text())
                await self.render.end(path.stem, payload)
            except (OSError, ValueError) as exc:
                logger.error("Spooled report %s unreadable: %s", path.name, exc)
                continue
            except RenderError as exc:
                logger.warning("Spooled report %s still not accepted: %s", path.name, exc)
                break  # Render is still down: try the rest next round
            path.unlink(missing_ok=True)
            sent += 1
        if sent:
            logger.info("Delivered %d spooled end report(s)", sent)
        return sent

    async def drain(self, timeout: float) -> None:
        """Wait for reports in flight (service stopping)."""
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)

"""Client for Render's /internal/voice/* (backend/call/agent_api.py).

Plain urllib in a worker thread: three small JSON calls per call, no need for
an HTTP library on the VM.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)


class RenderError(Exception):
    pass


class RenderAPI:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token

    def _post(self, path: str, body: dict[str, Any], timeout: float) -> tuple[int, Any]:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body, ensure_ascii=False).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raise RenderError(f"{path}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RenderError(f"{path}: {exc}") from exc

    async def post(self, path: str, body: dict[str, Any], timeout: float) -> tuple[int, Any]:
        return await asyncio.to_thread(self._post, path, body, timeout)

    async def start_call(
        self, call_uuid: str, caller: str, dialed: str, diversion: str, timeout: float = 1.5
    ) -> dict[str, Any]:
        _, data = await self.post(
            "/internal/voice/calls",
            {"call_uuid": call_uuid, "from": caller, "to": dialed, "diversion": diversion},
            timeout,
        )
        return data or {}

    async def tool(
        self, call_uuid: str, name: str, arguments: str | None, timeout: float = 10.0
    ) -> str | None:
        _, data = await self.post(
            f"/internal/voice/calls/{call_uuid}/tool",
            {"name": name, "arguments": arguments},
            timeout,
        )
        return (data or {}).get("output")

    async def end(self, call_uuid: str, payload: dict[str, Any], timeout: float = 10.0) -> None:
        status, _ = await self.post(f"/internal/voice/calls/{call_uuid}/end", payload, timeout)
        if status != 202:
            raise RenderError(f"/end answered {status}")

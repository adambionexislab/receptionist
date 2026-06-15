"""Browser live-demo session tokens.

POST /session-token mints a short-lived ephemeral client secret for OpenAI's
Realtime API so the landing-page "Parla con Apollonia" widget can open a WebRTC
connection straight to OpenAI. The backend is never in the audio path: it only
hands the browser a token that expires in ~1 minute.

The OpenAI API key stays server-side — only the ephemeral secret reaches the
browser. The demo session reuses the same model and voice as the phone agent
(see call/router.py) and the same system prompt, with the public knowledge base
appended so Apollonia can answer questions about herself, the product and
pricing. The phone agent's tools are intentionally NOT included: in the WebRTC
demo there is no server to answer function calls, so leaving them out keeps the
conversation from stalling on an unanswerable tool call.
"""

import logging
from functools import lru_cache
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException

from call.router import _SESSION_UPDATE, _SYSTEM_PROMPT
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

# Project root (sibling of backend/). The knowledge base is a top-level file.
_KB_PATH = Path(__file__).resolve().parent.parent.parent / "apollonia_knowledge_base.md"

_OPENAI_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"

_DEMO_MODEL = _SESSION_UPDATE["session"]["model"]
_DEMO_VOICE = _SESSION_UPDATE["session"]["audio"]["output"]["voice"]

# Demo override: there are no listing tools in the WebRTC demo, so Apollonia must
# decline property searches instead of pretending to run them. Placed last so it
# takes priority over the phone prompt's tool-driven call flows.
_DEMO_NOTE = (
    "# Demo dal sito — nessuna ricerca immobili\n"
    "Questa è una demo dal vivo sul sito web, non una vera chiamata. In questa\n"
    "demo NON hai accesso agli annunci immobiliari e gli strumenti di ricerca\n"
    "non sono disponibili. Se il visitatore chiede di cercare immobili, chiede\n"
    "di un immobile specifico, di prezzi o disponibilità di immobili, o vuole\n"
    "essere ricontattato per un immobile, spiega in modo cordiale che questa è\n"
    "solo una demo e che potrai farlo davvero una volta che ApollonIA sarà\n"
    "attiva per la sua agenzia. Poi invitalo a chiederti chi sei, cosa sai\n"
    "fare, come funzioni o quanto costi.\n"
    "Non usare, citare né simulare strumenti (search_listings,\n"
    "get_listing_by_address, record_caller_info, leave_message, end_call,\n"
    "ecc.): in questa demo non esistono."
)


@lru_cache(maxsize=1)
def _demo_instructions() -> str:
    """Phone system prompt + the public knowledge base + the demo override, so
    the demo agent can answer product questions but declines property searches.
    Cached: the file is read once."""
    try:
        kb = _KB_PATH.read_text(encoding="utf-8")
        base = f"{_SYSTEM_PROMPT}\n\n# Base di conoscenza (demo dal sito)\n{kb}"
    except FileNotFoundError:
        logger.warning(
            "Knowledge base not found at %s — demo prompt without it", _KB_PATH
        )
        base = _SYSTEM_PROMPT
    return f"{base}\n\n{_DEMO_NOTE}"


@router.post("/session-token")
async def session_token():
    """Create an ephemeral Realtime session and return the client secret."""
    if not settings.OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY not configured — cannot mint demo session token")
        raise HTTPException(status_code=503, detail="Demo non disponibile al momento.")

    session_config = {
        "type": "realtime",
        "model": _DEMO_MODEL,
        "instructions": _demo_instructions(),
        # WebRTC negotiates Opus audio itself, so only the voice is pinned here
        # (no PCM format fields, which apply to the raw WebSocket phone path).
        "audio": {"output": {"voice": _DEMO_VOICE}},
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                _OPENAI_CLIENT_SECRETS_URL,
                headers={
                    "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"session": session_config},
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "OpenAI client_secrets failed: %s — %s",
            exc,
            exc.response.text if exc.response is not None else "",
        )
        raise HTTPException(status_code=502, detail="Impossibile avviare la demo.")
    except Exception as exc:
        logger.error("OpenAI client_secrets error: %s", exc)
        raise HTTPException(status_code=502, detail="Impossibile avviare la demo.")

    # GA shape: {"value": "ek_...", "expires_at": ..., "session": {...}}.
    # Returned as-is; the browser uses `value` for the WebRTC SDP handshake.
    return resp.json()

"""Acquisizione — seller-meeting capture.

An agent runs a live-transcribed listing-intake meeting with a property
seller, entirely inside the agency dashboard (dashboard/index.html): GDPR
consent logging, a live WebRTC transcription session, one-shot extraction
into structured listing fields/tasks, an editable review + confirm step, and
optional photo enhancement.

Ships dark behind config.ACQUISIZIONE_ENABLED — main.py only mounts this
router when the flag is set.

No audio is ever stored: only the resulting transcript text (see
acquisizione/db.py). The browser never sees the OpenAI API key — only the
ephemeral client secret minted by /session-token, exactly like the site's live
demo widget (see demo/router.py's session_token, same client_secrets pattern).
"""

import asyncio
import logging
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from acquisizione import db, extraction, photos
from config import settings
from dashboard.router import current_tenant

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/acquisizione")

_OPENAI_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"


class ConsentRequest(BaseModel):
    method: Literal["checkbox", "spoken"]


@router.post("/consent")
async def give_consent(data: ConsentRequest, tenant: dict = Depends(current_tenant)):
    """Log consent and open a new intake record, market copied from the
    tenant's current locale. Must happen before any audio is captured — the
    frontend only enables the start button after this call succeeds."""
    market = tenant.get("locale") or "it"
    record = await asyncio.to_thread(db.create, tenant["id"], market, data.method)
    return {"id": record["id"], "market": record["market"]}


class TranscriptUpdate(BaseModel):
    transcript: str


@router.patch("/{record_id}/transcript")
async def autosave_transcript(
    record_id: str, data: TranscriptUpdate, tenant: dict = Depends(current_tenant)
):
    """Periodic defensive autosave of the accumulated transcript, so a crash
    mid-meeting doesn't lose everything. A 404 also covers the record having
    already moved past 'transcribing' (finished/reviewed) — the frontend stops
    calling this after Termina riunione, so that's not expected in practice."""
    ok = await asyncio.to_thread(db.update_transcript, record_id, tenant["id"], data.transcript)
    if not ok:
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}


@router.post("/{record_id}/session-token")
async def session_token(record_id: str, tenant: dict = Depends(current_tenant)):
    """Mint an ephemeral Realtime client secret for a transcription-only WebRTC
    session, scoped to this record's market. Called once to start the meeting
    and again transparently whenever a session approaches its 60-minute cap or
    drops, so the frontend can open a fresh session without losing the
    transcript buffer it has accumulated client-side."""
    record = await asyncio.to_thread(db.get, record_id, tenant["id"])
    if not record:
        raise HTTPException(status_code=404, detail="Not found")
    if record["status"] != "transcribing":
        raise HTTPException(status_code=409, detail="Meeting is no longer accepting audio")

    if not settings.OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY not configured — cannot mint acquisizione session token")
        raise HTTPException(status_code=503, detail="Not configured")

    session_config = {
        "type": "transcription",
        "audio": {
            "input": {
                # gpt-realtime-whisper doesn't support server_vad turn
                # detection: the client commits the input buffer itself on a
                # timer (see the Acquisizione tab's JS) to segment the running
                # transcript into chunks. Leaving this null is required, not
                # just optional, for this model.
                "turn_detection": None,
                "transcription": {
                    "model": settings.REALTIME_TRANSCRIBE_MODEL,
                    "language": record["market"],
                    "delay": "low",
                },
            }
        },
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
            "OpenAI client_secrets (transcription) failed: %s — %s",
            exc, exc.response.text if exc.response is not None else "",
        )
        raise HTTPException(status_code=502, detail="Failed to start transcription session")
    except Exception as exc:
        logger.error("OpenAI client_secrets (transcription) error: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to start transcription session")

    # GA shape: {"value": "ek_...", "expires_at": ..., "session": {...}}.
    # Returned as-is; the browser uses `value` for the WebRTC SDP handshake.
    # Unlike a "realtime" session, the /calls handshake must NOT get a
    # `model=` query param for a transcription session — see the frontend's
    # openSession(), which deliberately omits it.
    return resp.json()


@router.post("/{record_id}/finish")
async def finish_meeting(record_id: str, tenant: dict = Depends(current_tenant)):
    """Close out the live meeting and run the one-shot extraction over the
    transcript accumulated so far (already up to date via periodic autosave —
    the frontend flushes one last time before calling this). On failure the
    record reverts to 'transcribing' so the transcript is never lost and the
    agent can just retry, per the 'fail loud, don't discard' instruction."""
    record = await asyncio.to_thread(db.get, record_id, tenant["id"])
    if not record:
        raise HTTPException(status_code=404, detail="Not found")
    if not record["transcript"].strip():
        raise HTTPException(status_code=400, detail="Transcript is empty")

    moved = await asyncio.to_thread(db.set_processing, record_id, tenant["id"])
    if not moved:
        raise HTTPException(status_code=409, detail="Meeting is not awaiting extraction")

    try:
        result = await extraction.extract(record["transcript"], record["market"])
    except extraction.ExtractionError as exc:
        logger.error("Extraction failed for record %s: %s", record_id, exc)
        await asyncio.to_thread(db.revert_to_transcribing, record_id, tenant["id"])
        raise HTTPException(status_code=502, detail="Extraction failed, please retry")

    await asyncio.to_thread(
        db.set_review_result,
        record_id, tenant["id"],
        result["listing_fields"], result["missing_required"],
        result["listing_text"], result["tasks"],
    )
    return await asyncio.to_thread(db.get, record_id, tenant["id"])


class ConfirmRequest(BaseModel):
    listing_fields: dict[str, Any]
    listing_text: str
    tasks: list[dict[str, Any]]


@router.patch("/{record_id}/confirm")
async def confirm_record(
    record_id: str, data: ConfirmRequest, tenant: dict = Depends(current_tenant)
):
    """Save the agent's edited fields/text/tasks and mark the record final.
    Nothing about the listing is persisted as confirmed before this call."""
    ok = await asyncio.to_thread(
        db.confirm, record_id, tenant["id"],
        data.listing_fields, data.listing_text, data.tasks,
    )
    if not ok:
        raise HTTPException(status_code=409, detail="Record is not awaiting confirmation")
    return await asyncio.to_thread(db.get, record_id, tenant["id"])


_MAX_PHOTO_BYTES = 50 * 1024 * 1024  # OpenAI's own per-image limit


@router.post("/photos/enhance")
async def enhance_photo(
    image: UploadFile = File(...),
    mode: str = Form("enhance"),
    tenant: dict = Depends(current_tenant),
):
    """Enhance one uploaded photo and stream the result straight back. In the
    default 'enhance' mode, the model decides what the photo needs (lighting,
    perspective, clutter) rather than the agent picking a preset. In
    'modernize' mode, it virtually stages the room instead — for an old,
    damaged, or under-construction property. Not tied to any intake record
    and nothing is persisted (see acquisizione/photos.py) — the agent
    downloads whatever they want to keep. Isolated failure: never touches a
    listing record."""
    if mode not in photos.MODES:
        raise HTTPException(status_code=400, detail="Unknown mode")
    raw = await image.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(raw) > _MAX_PHOTO_BYTES:
        raise HTTPException(status_code=413, detail="Image too large")

    try:
        edited = await photos.enhance(
            raw, image.filename or "photo.png", image.content_type or "image/png", mode,
        )
    except photos.PhotoEnhanceError as exc:
        logger.error("Photo enhancement failed: %s", exc)
        raise HTTPException(status_code=502, detail="Photo enhancement failed, please retry")

    return Response(content=edited, media_type="image/png")


@router.get("")
async def list_records(tenant: dict = Depends(current_tenant)):
    """This tenant's intake records, most recent first. Strictly scoped to the
    logged-in tenant's id."""
    records = await asyncio.to_thread(db.list_for_tenant, tenant["id"])
    return {"records": records}


@router.get("/{record_id}")
async def get_record(record_id: str, tenant: dict = Depends(current_tenant)):
    record = await asyncio.to_thread(db.get, record_id, tenant["id"])
    if not record:
        raise HTTPException(status_code=404, detail="Not found")
    return record

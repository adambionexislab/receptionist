"""Sales-meeting notes — the rep's spoken debrief, transcribed and structured.

The general-purpose sibling of the acquisizione meeting tool: same live WebRTC
transcription and one-shot extraction, but nothing property-specific. The rep
turns it on after a meeting, says how it went, and gets back an editable note
(what went well, what went wrong, the customer's objections, next steps) that
the team can read across meetings.

Lives on the internal lead-gen dashboard (leadgen/index.html) next to campaigns
and leads, behind the same staff login, and is global like they are — no tenant
scoping, and no agency ever sees it.

No audio is ever stored: only the transcript text (see salesnotes/db.py). The
browser never sees the OpenAI API key — only the ephemeral client secret
minted by /session-token, exactly like the acquisizione tool and the site's
live demo widget.
"""

import asyncio
import logging
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from config import settings
from leadgen.router import require_staff
from salesnotes import content, db, extraction, schema

logger = logging.getLogger(__name__)

# The dependency is on the router, not the routes: every endpoint here is
# behind the staff login by default, including any added later. /session-token
# in particular mints OpenAI credentials, so it must never be reachable
# unauthenticated.
router = APIRouter(prefix="/notes", dependencies=[Depends(require_staff)])

_OPENAI_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"


def _require(note_id: str) -> dict[str, Any]:
    note = db.get(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Not found")
    return note


class NoteCreate(BaseModel):
    # The language the rep will speak in — drives both the transcription
    # session and the language the finished note is written in.
    language: str = content.DEFAULT_LANGUAGE


@router.post("")
async def create_note(data: NoteCreate):
    """Open a new note. Called on the first click, before any audio is
    captured — the recording can't start without an id to autosave against."""
    language = data.language if data.language in content.LANGUAGES else content.DEFAULT_LANGUAGE
    return await asyncio.to_thread(db.create, language)


class TranscriptUpdate(BaseModel):
    transcript: str


@router.api_route("/{note_id}/transcript", methods=["PATCH", "POST"])
async def autosave_transcript(note_id: str, data: TranscriptUpdate):
    """Periodic defensive autosave of the accumulated transcript, so a crash
    mid-debrief doesn't lose it. A 404 also covers the note having already
    moved past 'recording'.

    POST is the same call: the browser's last-chance flush as the tab goes away
    uses navigator.sendBeacon — the only request that reliably survives a page
    being closed — and a beacon can only POST."""
    ok = await asyncio.to_thread(db.update_transcript, note_id, data.transcript)
    if not ok:
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}


@router.post("/{note_id}/session-token")
async def session_token(note_id: str):
    """Mint an ephemeral Realtime client secret for a transcription-only WebRTC
    session in this note's language. Called once to start recording and again
    transparently whenever a session approaches its 60-minute cap or drops, so
    the browser can open a fresh one without losing its transcript buffer."""
    note = _require(note_id)
    if note["status"] != "recording":
        raise HTTPException(status_code=409, detail="Note is no longer accepting audio")

    if not settings.OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY not configured — cannot mint note session token")
        raise HTTPException(status_code=503, detail="Not configured")

    session_config = {
        "type": "transcription",
        "audio": {
            "input": {
                # gpt-realtime-whisper doesn't support server_vad turn
                # detection: the client commits the input buffer itself (see
                # the Note tab's JS) to segment the running transcript. Leaving
                # this null is required, not optional, for this model.
                "turn_detection": None,
                "transcription": {
                    "model": settings.REALTIME_TRANSCRIBE_MODEL,
                    "language": note["language"],
                    # Accuracy over immediacy — nobody reads this transcript as
                    # it streams; it's extracted once the rep stops talking.
                    "delay": settings.REALTIME_TRANSCRIBE_DELAY,
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
            "OpenAI client_secrets (note transcription) failed: %s — %s",
            exc, exc.response.text if exc.response is not None else "",
        )
        raise HTTPException(status_code=502, detail="Failed to start transcription session")
    except Exception as exc:
        logger.error("OpenAI client_secrets (note transcription) error: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to start transcription session")

    # GA shape: {"value": "ek_...", "expires_at": ..., "session": {...}}.
    # Returned as-is; the browser uses `value` for the WebRTC SDP handshake.
    # Unlike a "realtime" session, the /calls handshake must NOT get a `model=`
    # query param for a transcription session — see the frontend's openSession().
    return resp.json()


@router.post("/{note_id}/abandon")
async def abandon_note(note_id: str):
    """The rep deliberately walked away from this recording (the go-back
    button, or dismissing the offer to resume it). Keeps the note and its
    transcript, and stops /resumable offering it back.

    Never raises: this fires on the way out of a screen, and a note that has
    already moved on isn't a problem the rep can act on — it reports ok=False."""
    ok = await asyncio.to_thread(db.abandon, note_id)
    return {"ok": ok}


@router.post("/{note_id}/finish")
async def finish_note(note_id: str):
    """Stop recording and run the one-shot extraction over the transcript
    (already up to date via autosave — the browser flushes once more before
    calling this). On failure the note reverts to 'recording' so the transcript
    is never lost and the rep can retry."""
    note = _require(note_id)
    if not note["transcript"].strip():
        raise HTTPException(status_code=400, detail="Transcript is empty")

    moved = await asyncio.to_thread(db.set_processing, note_id)
    if not moved:
        raise HTTPException(status_code=409, detail="Note is not awaiting extraction")

    try:
        fields = await extraction.extract(note["transcript"], note["language"])
    except extraction.ExtractionError as exc:
        logger.error("Note extraction failed for %s: %s", note_id, exc)
        await asyncio.to_thread(db.revert_to_recording, note_id)
        raise HTTPException(status_code=502, detail="Extraction failed, please retry")

    await asyncio.to_thread(db.set_review_result, note_id, fields)
    return await asyncio.to_thread(db.get, note_id)


class NoteSave(BaseModel):
    """The note as the rep edited it. Everything is optional and defaulted:
    clearing a field the extraction filled in is a legitimate edit."""

    title: Optional[str] = None
    customer: Optional[str] = None
    outcome: Optional[str] = None
    summary: Optional[str] = None
    went_well: list[str] = Field(default_factory=list)
    went_wrong: list[str] = Field(default_factory=list)
    objections: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


@router.patch("/{note_id}")
async def save_note(note_id: str, data: NoteSave):
    """Store the rep's edited note. Also the way an already-saved note is
    corrected later — see db.save."""
    _require(note_id)
    fields = schema.normalize(data.model_dump())
    ok = await asyncio.to_thread(db.save, note_id, fields)
    if not ok:
        raise HTTPException(status_code=409, detail="Note is not ready to be saved")
    return await asyncio.to_thread(db.get, note_id)


@router.delete("/{note_id}")
async def delete_note(note_id: str):
    """Delete a note outright — the recording that was started by mistake."""
    ok = await asyncio.to_thread(db.delete, note_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}


@router.get("")
async def list_notes(limit: int = 100):
    """Every note worth showing, newest first."""
    notes = await asyncio.to_thread(db.list_notes, limit)
    return {"notes": notes, "languages": content.LANGUAGES, "outcomes": list(schema.OUTCOMES)}


# Declared before /{note_id}: routes match in declaration order, so the literal
# path has to come first or it gets swallowed as a note id.
@router.get("/resumable")
async def resumable_note():
    """A recording left mid-debrief and never come back to — the browser was
    closed, the phone killed the tab. Returns the whole note (transcript
    included) so the dashboard can restore the buffer it lost, or
    {"note": null} when there's nothing to offer."""
    note = await asyncio.to_thread(db.get_resumable)
    return {"note": note}


@router.get("/{note_id}")
async def get_note(note_id: str):
    return _require(note_id)

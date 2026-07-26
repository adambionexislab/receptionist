"""Property photo enhancement (Phase 5).

Enhance-and-return only, per the operator's decision: no photo is ever
written to disk or the database — the edited image is streamed straight back
to the browser, and the agent downloads whatever they want to keep. A failure
here must never affect the already-saved listing; this module has no
interaction with intake_records at all.

Uses the Images API's edit endpoint (POST /v1/images/edits) directly via
httpx, matching every other OpenAI integration in this codebase (call/
router.py, demo/router.py, acquisizione/extraction.py) rather than adding the
openai SDK as a new dependency.
"""

import base64
import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)

_IMAGES_EDIT_URL = "https://api.openai.com/v1/images/edits"

# Complex edits can take up to ~2 minutes per OpenAI's docs (images_openai.md).
_TIMEOUT = 150

_PRESET_PROMPTS: dict[str, str] = {
    "declutter": (
        "Remove clutter, personal items, and loose objects from this real "
        "estate listing photo. Keep the room's architecture, furniture "
        "layout, walls, flooring, and lighting exactly as they are — only "
        "remove clutter."
    ),
    "relight": (
        "Improve the lighting of this real estate listing photo: brighten "
        "and balance exposure, correct color cast, make it look naturally "
        "well-lit. Do not change the room's layout, furniture, or any "
        "objects in it."
    ),
    "straighten": (
        "Correct the perspective and straighten the vertical and horizontal "
        "lines in this real estate listing photo (walls, door frames, "
        "furniture edges), as if shot with a level camera. Do not change "
        "the room's layout, furniture, or any objects in it."
    ),
}

PRESETS = tuple(_PRESET_PROMPTS)


class PhotoEnhanceError(Exception):
    """Raised when the enhancement call fails. Callers should surface a
    clear failure to the agent — this never touches the listing record, so
    there is nothing to roll back."""


async def enhance(image_bytes: bytes, filename: str, content_type: str, preset: str) -> bytes:
    """Send one photo through the configured image-edit model with a preset
    prompt. Returns the edited image's raw bytes (PNG)."""
    if preset not in _PRESET_PROMPTS:
        raise PhotoEnhanceError(f"Unknown preset: {preset}")
    if not settings.OPENAI_API_KEY:
        raise PhotoEnhanceError("OPENAI_API_KEY not configured")

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                _IMAGES_EDIT_URL,
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                data={"model": settings.IMAGE_EDIT_MODEL, "prompt": _PRESET_PROMPTS[preset]},
                files={"image": (filename, image_bytes, content_type)},
            )
            if resp.status_code >= 400:
                logger.error(
                    "Photo enhance request failed: %s — %s", resp.status_code, resp.text,
                )
                raise PhotoEnhanceError(
                    f"OpenAI image edit rejected (status {resp.status_code})"
                )
            data = resp.json()
    except httpx.HTTPError as exc:
        logger.error("Photo enhance request error: %s", exc)
        raise PhotoEnhanceError(f"Photo enhance request failed: {exc}") from exc

    try:
        b64 = data["data"][0]["b64_json"]
    except (KeyError, IndexError, TypeError) as exc:
        logger.error("Photo enhance response missing image data: %s", data)
        raise PhotoEnhanceError("Photo enhance returned no image") from exc

    return base64.b64decode(b64)

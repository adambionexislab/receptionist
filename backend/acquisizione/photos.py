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

_ENHANCE_PROMPT = (
    "This is a photo of a property for a real estate listing. Enhance it so "
    "it looks like it was taken by a professional real estate photographer: "
    "look at what the photo actually needs and fix that — correct and "
    "balance the lighting (brighten, fix exposure and color cast, make it "
    "look naturally and evenly lit), and correct the camera perspective "
    "(straighten vertical and horizontal lines — walls, door frames, window "
    "frames — as if shot level, on a tripod, with a wide-angle real-estate "
    "lens). If the room looks cluttered or messy, clean it up so it looks "
    "tidy and presentable.\n"
    "\n"
    "Strict constraints: do not change the room's dimensions, layout, or "
    "vantage point beyond leveling the perspective. Do not remove, add, "
    "move, resize, or otherwise alter any furniture, windows, doors, or "
    "other fixtures — every important object that is in the photo must "
    "still be in the photo afterward, unchanged apart from being clean and "
    "well lit. Only tidy loose clutter and mess; never redesign the room."
)


class PhotoEnhanceError(Exception):
    """Raised when the enhancement call fails. Callers should surface a
    clear failure to the agent — this never touches the listing record, so
    there is nothing to roll back."""


async def enhance(image_bytes: bytes, filename: str, content_type: str) -> bytes:
    """Send one photo through the configured image-edit model. The model
    decides what the photo actually needs (lighting, perspective, clutter)
    rather than the agent picking a preset. Returns the edited image's raw
    bytes (PNG)."""
    if not settings.OPENAI_API_KEY:
        raise PhotoEnhanceError("OPENAI_API_KEY not configured")

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                _IMAGES_EDIT_URL,
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                data={
                    "model": settings.IMAGE_EDIT_MODEL,
                    "prompt": _ENHANCE_PROMPT,
                    # This is a same-composition touch-up, not from-scratch
                    # generation — "auto" quality tends to resolve to "high"
                    # for a photographic edit like this, which is the main
                    # driver of the ~1-2 minute latency. "medium" is a big
                    # latency win for a modest quality trade-off; leave size
                    # on auto so a landscape-oriented room photo isn't
                    # forced/cropped into a square.
                    "quality": "medium",
                },
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

"""Tile capture → photorealistic aerial, via the OpenAI Images edit endpoint.

Same shape as acquisizione/photos.py: POST /v1/images/edits over httpx, no
OpenAI SDK, model taken from settings.IMAGE_EDIT_MODEL so a bump lands here and
on photo enhancement together.

Why this step exists at all: the Cesium capture is geometry, and Photorealistic
3D Tiles coverage in the smaller towns these agencies work is often flat and
low-detail (see videotour/tiles.py). Handing that render straight to Seedance
would establish the zone with something that reads as a video game. This pass
is what makes it look like a drone photograph — while keeping the actual layout
untouched, so the neighbourhood the drone flies over is still the real one.
"""

import base64
import logging

import httpx

from config import settings
from videotour.content import AERIAL_PROMPT

logger = logging.getLogger(__name__)

_IMAGES_EDIT_URL = "https://api.openai.com/v1/images/edits"

# Matches acquisizione/photos.py: a complex edit can run to ~2 minutes.
_TIMEOUT = 150


class PhotorealError(Exception):
    """Raised when the aerial conversion fails."""


async def to_aerial(tile_png: bytes) -> bytes:
    """Convert a 3D tile capture into a photorealistic aerial photograph.

    Returns PNG bytes. Quality is left on "high" rather than the "medium" the
    photo enhancer uses: this image is the establishing reference for the whole
    drone shot, it is generated once per tour rather than once per photo, and
    the latency is hidden behind the agent still uploading photos in step 2.
    """
    if not settings.OPENAI_API_KEY:
        raise PhotorealError("OPENAI_API_KEY not configured")

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                _IMAGES_EDIT_URL,
                headers={"Authorization": "Bearer " + settings.OPENAI_API_KEY},
                data={
                    "model": settings.IMAGE_EDIT_MODEL,
                    "prompt": AERIAL_PROMPT,
                    "quality": "high",
                    # Leave size on auto so the 16:9 capture is not squared off
                    # — the aspect ratio has to survive into the generation.
                    "size": "auto",
                },
                files={"image": ("tile.png", tile_png, "image/png")},
            )
            if resp.status_code >= 400:
                logger.error(
                    "Aerial conversion failed: %s — %s", resp.status_code, resp.text,
                )
                raise PhotorealError(
                    "OpenAI image edit rejected (status %s)" % resp.status_code
                )
            data = resp.json()
    except httpx.HTTPError as exc:
        logger.error("Aerial conversion request error: %s", exc)
        raise PhotorealError("Aerial conversion request failed: %s" % exc) from exc

    try:
        b64 = data["data"][0]["b64_json"]
    except (KeyError, IndexError, TypeError) as exc:
        logger.error("Aerial conversion returned no image data: %s", data)
        raise PhotorealError("Aerial conversion returned no image") from exc

    return base64.b64decode(b64)

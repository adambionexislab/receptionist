"""Runway image-to-video client.

Direct httpx against the REST API, matching every other third-party
integration in this codebase (acquisizione/photos.py, call/router.py) rather
than pulling in a vendor SDK.

MODEL AND MODE
--------------
Both generations use Seedance 2.5 in REFERENCE mode — `promptImage` as an
array of {uri} objects with no `position` key. Two things follow from that,
and both are load-bearing:

  * No frame is pinned. Runway's api.md is explicit that "the two modes cannot
    be mixed", so passing several room photos as references rules out also
    nominating a first or last frame. The drone clip therefore does not end on
    the exact exterior photo, which is why the stitch uses a crossfade rather
    than a hard cut (see videotour/ffmpeg.py).
  * Seedance is the model precisely because of this mode. gen4.5 and gen4_turbo
    are documented as position "first" only with no reference mode at all, so
    they cannot take N rooms in one generation.

References are addressed in the prompt as @Image1…@ImageN, by their position
in the array. That syntax is documented for Seedance multi-reference but is
not shown as a field on promptImage in the API reference, so the prompts in
videotour/content.py also describe the rooms in plain order — if the markers
are ignored, the prompt still reads correctly.

COST
----
Billed per second of OUTPUT. Reference images are free. At 720p that is 30
credits ($0.30) a second, so duration is the only real cost lever and the
30-second ceiling is what caps a tour. Nothing here retries on its own: a
failed generation surfaces to the pipeline, which decides whether the agency
gets billed for another attempt.
"""

import asyncio
import base64
import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.dev.runwayml.com/v1"
_TIMEOUT = 60

# Runway asks for no more than one poll per task per five seconds.
_POLL_INTERVAL = 6
# A 30-second Seedance generation is minutes of wall time, not seconds. This is
# the ceiling before we give up and let the job fail retryably.
_POLL_TIMEOUT = 15 * 60

_TERMINAL_OK = {"SUCCEEDED"}
_TERMINAL_BAD = {"FAILED", "CANCELLED", "CANCELED"}

# Per Runway's input docs a data URI may not exceed 5MB. Images are downscaled
# before they get here (see videotour/ffmpeg.py); this is the backstop that
# turns an oversized image into a clear error instead of a 400 from the API.
MAX_DATA_URI_BYTES = 5 * 1024 * 1024

# Seedance accepts 30 reference images. We cap interiors well below this, so
# hitting the limit means a caller built the list wrong.
MAX_REFERENCES = 30


class RunwayError(Exception):
    """Raised when a generation cannot be started or does not complete."""


def data_uri(image_bytes: bytes, content_type: str = "image/jpeg") -> str:
    """Encode an image as a data URI for promptImage."""
    if len(image_bytes) > MAX_DATA_URI_BYTES:
        raise RunwayError(
            "Image is %.1f MB; the Runway data-URI limit is 5 MB "
            "(it should have been downscaled first)" % (len(image_bytes) / 1_048_576)
        )
    encoded = base64.b64encode(image_bytes).decode()
    return "data:%s;base64,%s" % (content_type, encoded)


def _headers() -> dict[str, str]:
    if not settings.RUNWAY_API_KEY:
        raise RunwayError("RUNWAY_API_KEY not configured")
    return {
        "Authorization": "Bearer " + settings.RUNWAY_API_KEY,
        "X-Runway-Version": settings.RUNWAY_API_VERSION,
        "Content-Type": "application/json",
    }


async def start_generation(references: list[str], prompt: str, duration: int) -> str:
    """Start one image-to-video generation in reference mode. Returns the task id.

    `references` are data URIs in the order the prompt addresses them as
    @Image1…@ImageN, so the caller controls that ordering deliberately.
    """
    if not references:
        raise RunwayError("At least one reference image is required")
    if len(references) > MAX_REFERENCES:
        raise RunwayError("Too many reference images: %d" % len(references))

    body = {
        "model": settings.RUNWAY_VIDEO_MODEL,
        # Reference mode: no "position" key on any entry. Adding one here would
        # silently switch the call to keyframe mode and invalidate the rest.
        "promptImage": [{"uri": uri} for uri in references],
        "promptText": prompt,
        "ratio": settings.RUNWAY_VIDEO_RATIO,
        "duration": int(duration),
        # Defaults to true. Generated audio across two separately generated
        # clips would not match at the junction, and the stitch would have to
        # reconcile one silent and one audible track — the tour is silent.
        "audio": False,
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                _BASE_URL + "/image_to_video", headers=_headers(), json=body,
            )
            if resp.status_code >= 400:
                logger.error(
                    "Runway generation rejected: %s — %s", resp.status_code, resp.text,
                )
                raise RunwayError(
                    "Runway rejected the generation (status %s)" % resp.status_code
                )
            data = resp.json()
    except httpx.HTTPError as exc:
        logger.error("Runway generation request error: %s", exc)
        raise RunwayError("Runway request failed: %s" % exc) from exc

    task_id = data.get("id")
    if not task_id:
        raise RunwayError("Runway returned no task id")
    logger.info(
        "Runway task %s started (model=%s duration=%ss refs=%d)",
        task_id, settings.RUNWAY_VIDEO_MODEL, duration, len(references),
    )
    return task_id


async def poll(task_id: str, on_progress=None) -> str:
    """Poll one task to completion and return the output video URL.

    `on_progress` is an optional async callback taking a 0-100 int, used to
    feed step 3's progress bar. Output URLs are ephemeral (Runway expires them
    within 24-48h), so the caller must download immediately — never store one.
    """
    waited = 0
    while waited < _POLL_TIMEOUT:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    _BASE_URL + "/tasks/" + task_id, headers=_headers(),
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            # A transient poll failure must not lose a generation we have
            # already paid for — log it and try again on the next tick.
            logger.warning("Runway poll for %s failed, retrying: %s", task_id, exc)
            await asyncio.sleep(_POLL_INTERVAL)
            waited += _POLL_INTERVAL
            continue

        status = (data.get("status") or "").upper()
        if status in _TERMINAL_OK:
            output = data.get("output") or []
            if not output:
                raise RunwayError("Runway succeeded but returned no output")
            logger.info("Runway task %s succeeded", task_id)
            return output[0]
        if status in _TERMINAL_BAD:
            detail = data.get("failure") or data.get("failureCode") or status
            raise RunwayError("Runway task failed: %s" % detail)

        if on_progress is not None:
            # Runway reports 0-1 while running; some statuses omit it entirely.
            raw = data.get("progress")
            if isinstance(raw, (int, float)):
                await on_progress(int(max(0.0, min(1.0, float(raw))) * 100))

        await asyncio.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL

    raise RunwayError("Runway task %s did not finish in time" % task_id)


async def download(url: str) -> bytes:
    """Fetch a finished generation. Runway output URLs expire within 24-48
    hours, so this runs immediately after poll() and the bytes go straight to
    the job directory."""
    try:
        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
    except httpx.HTTPError as exc:
        logger.error("Runway output download failed: %s", exc)
        raise RunwayError("Could not download the generated video: %s" % exc) from exc

"""AI video tour — the dashboard's third AI tool.

A three-step wizard in the agency dashboard: confirm the property's location on
a map, upload an exterior photo and the rooms, watch the video assemble.

Ships dark behind config.VIDEO_TOUR_ENABLED — main.py only mounts this router
when the flag is set, exactly like the acquisizione router.

Every route depends on `current_tenant` (dashboard/router.py) and every query
is scoped by tenant_id, so one agency can never read or touch another's tour.
That includes the video itself: finished tours are streamed through an
authenticated route rather than mounted as static files, because a StaticFiles
mount over the media directory would make every tenant's video public to anyone
who could guess a UUID.
"""

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import settings
from dashboard.router import current_branch, current_tenant
from videotour import db, ffmpeg, geocode, pipeline, storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/video-tour")

# Matches acquisizione's photo limit — the ceiling is what a phone camera
# produces, not what Runway accepts (images are downscaled before they are sent).
_MAX_PHOTO_BYTES = 50 * 1024 * 1024

_ALLOWED_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic"}


# ── step 1: address and location ────────────────────────────────────────────
class GeocodeRequest(BaseModel):
    address: str


@router.post("/geocode")
async def geocode_address(data: GeocodeRequest, tenant: dict = Depends(current_tenant)):
    """Resolve a typed address to coordinates so the map can drop its marker.

    Server-side deliberately: the geocoding key never reaches the browser, which
    only ever gets the separate referrer-restricted key used to draw the map.
    """
    try:
        return await geocode.lookup(data.address, tenant.get("locale") or "it")
    except geocode.GeocodeError as exc:
        logger.info("Geocode failed for tenant %s: %s", tenant["id"], exc)
        raise HTTPException(status_code=422, detail=str(exc))


class CreateRequest(BaseModel):
    """The agent has dragged the marker where they want it and pressed confirm.
    lat/lng are the final coordinates — the geocoded ones are only a suggestion,
    and a dragged pin is usually the more accurate of the two."""
    address: str
    lat: float
    lng: float
    listing_id: Optional[str] = None


@router.post("", status_code=201)
async def create_job(
    data: CreateRequest,
    tenant: dict = Depends(current_tenant),
    branch: Optional[dict] = Depends(current_branch),
):
    """Open a job and start the tile capture immediately.

    This is what makes step 2 feel instant later: by the time the agent has
    picked their photos, the capture and the gpt-image-2 aerial are already
    running or done, so the only thing left to wait for is Runway.
    """
    if not (data.address or "").strip():
        raise HTTPException(status_code=422, detail="Address is required")
    if not (-90 <= data.lat <= 90) or not (-180 <= data.lng <= 180):
        raise HTTPException(status_code=422, detail="Invalid coordinates")

    job = await asyncio.to_thread(
        db.create,
        tenant["id"], tenant.get("locale") or "it",
        data.address.strip(), data.lat, data.lng, data.listing_id,
        # Stored now, spent later: the credit is charged from a background task
        # once the tour is deliverable, long after this request is gone.
        branch["id"] if branch else None,
    )
    await pipeline.start(job)
    return job


# ── step 2: photos ──────────────────────────────────────────────────────────
async def _store_photo(tenant_id: str, job_id: str, upload: UploadFile, name: str) -> str:
    """Validate, downscale and store one uploaded photo.

    Downscaling happens here rather than at generation time for two reasons:
    Runway caps a data URI at 5MB and phone photos routinely exceed it, and
    keeping only the downscaled copy roughly halves what a job costs on a disk
    that is shared with the database. ffmpeg does the resize — it is already a
    hard requirement for the stitch, so no image library was added for this.
    """
    if upload.content_type and upload.content_type.lower() not in _ALLOWED_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported image type")
    raw = await upload.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(raw) > _MAX_PHOTO_BYTES:
        raise HTTPException(status_code=413, detail="Image too large")

    target_dir = await asyncio.to_thread(storage.ensure_job_dir, tenant_id, job_id)
    suffix = Path(upload.filename or "photo.jpg").suffix or ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(raw)
        source = Path(handle.name)
    try:
        await ffmpeg.downscale_still(source, target_dir / name)
    except ffmpeg.FfmpegError as exc:
        logger.error("Could not process upload for job %s: %s", job_id, exc)
        raise HTTPException(status_code=422, detail="Could not process that image")
    finally:
        source.unlink(missing_ok=True)

    return storage.rel(tenant_id, job_id, name)


@router.post("/{job_id}/exterior-photo")
async def upload_exterior(
    job_id: str,
    image: UploadFile = File(...),
    tenant: dict = Depends(current_tenant),
):
    """Store the exterior photo and, if the aerial is already done, start the
    drone generation.

    This is one half of the step-2 gate. It never waits on the other half: the
    photo is stored the moment it arrives, and whether this call or the aerial
    completion fires Runway is decided by a compare-and-swap in videotour/db.py.
    """
    job = await asyncio.to_thread(db.get, job_id, tenant["id"])
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")

    stored = await _store_photo(tenant["id"], job_id, image, "exterior.jpg")
    gate = await asyncio.to_thread(db.set_exterior_photo, job_id, tenant["id"], stored)
    if gate is None:
        raise HTTPException(status_code=409, detail="This tour is no longer accepting photos")

    await pipeline.on_exterior_photo_stored(job_id, tenant["id"], gate)
    return {"ok": True, "gate": gate}


@router.post("/{job_id}/interior-photos")
async def upload_interiors(
    job_id: str,
    images: list[UploadFile] = File(...),
    tenant: dict = Depends(current_tenant),
):
    """Store the room photos. Order is preserved — it is the order the rooms
    appear in the finished tour, and the order the prompt addresses them in.

    Capped at VIDEO_TOUR_MAX_INTERIOR_PHOTOS. Enforced here and not only in the
    wizard: every room is generated seconds, and generated seconds are billed.
    """
    job = await asyncio.to_thread(db.get, job_id, tenant["id"])
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")

    limit = settings.VIDEO_TOUR_MAX_INTERIOR_PHOTOS
    if not images:
        raise HTTPException(status_code=400, detail="No images uploaded")
    if len(images) > limit:
        raise HTTPException(
            status_code=413, detail="At most %d interior photos" % limit,
        )

    paths = []
    for index, image in enumerate(images):
        paths.append(
            await _store_photo(tenant["id"], job_id, image, "interior-%d.jpg" % index)
        )

    if not await asyncio.to_thread(db.set_interior_photos, job_id, tenant["id"], paths):
        raise HTTPException(status_code=409, detail="This tour is no longer accepting photos")
    return {"ok": True, "count": len(paths)}


# ── step 3: preview ─────────────────────────────────────────────────────────
# Declared before /{job_id}: routes match in declaration order, so a literal
# path has to come first or it gets swallowed as a job id.
@router.get("/config")
async def wizard_config(tenant: dict = Depends(current_tenant)):
    """What the wizard needs before it can draw anything: the browser-safe Maps
    key and the server's photo cap, so the UI enforces the same limit the API
    does rather than a hard-coded copy that can drift."""
    return {
        "maps_key": settings.GOOGLE_MAPS_BROWSER_KEY or "",
        "map_id": settings.GOOGLE_MAPS_MAP_ID or "",
        "max_interior_photos": settings.VIDEO_TOUR_MAX_INTERIOR_PHOTOS,
    }


@router.get("")
async def list_jobs(tenant: dict = Depends(current_tenant)):
    """This tenant's tours, most recent first. Strictly scoped by tenant_id."""
    return {"jobs": await asyncio.to_thread(db.list_for_tenant, tenant["id"])}


@router.get("/{job_id}")
async def get_job(job_id: str, tenant: dict = Depends(current_tenant)):
    """Polled by step 3's player. `player_state` collapses the backend statuses
    into the four states the UI actually renders."""
    job = await asyncio.to_thread(db.get, job_id, tenant["id"])
    if job is None:
        raise HTTPException(status_code=404, detail="Not found")
    return job


@router.get("/{job_id}/video")
async def get_video(job_id: str, tenant: dict = Depends(current_tenant)):
    """Stream a finished tour. Authenticated and tenant-scoped rather than
    served from a static mount, so a guessed job id gets a 404 and not a video."""
    job = await asyncio.to_thread(db.get, job_id, tenant["id"])
    if job is None or not job.get("video_path"):
        raise HTTPException(status_code=404, detail="Not found")
    path = storage.resolve(job["video_path"])
    if path is None or not path.is_file():
        # Swept after its retention window, or lost with the disk.
        raise HTTPException(status_code=410, detail="This video is no longer available")
    return FileResponse(str(path), media_type="video/mp4", filename="tour.mp4")


@router.post("/{job_id}/retry")
async def retry_job(job_id: str, tenant: dict = Depends(current_tenant)):
    """Re-run a failed tour from whatever is still missing — never from the tile
    capture. A stitch that failed after two successful generations re-runs only
    ffmpeg, so the agency is not billed twice for work already on disk."""
    stage = await asyncio.to_thread(
        db.begin_retry, job_id, tenant["id"], settings.VIDEO_TOUR_MAX_ATTEMPTS,
    )
    if stage is None:
        raise HTTPException(
            status_code=409, detail="This tour cannot be retried",
        )
    await pipeline.resume(job_id, tenant["id"], stage)
    return {"ok": True, "stage": stage}


@router.post("/{job_id}/abandon")
async def abandon_job(job_id: str, tenant: dict = Depends(current_tenant)):
    """The agent closed the wizard mid-flow. Frees the job's media for early
    collection instead of holding it for the full retention window. Never
    raises: this fires on the way out of a screen."""
    ok = await asyncio.to_thread(db.abandon, job_id, tenant["id"])
    return {"ok": ok}

"""Video-tour pipeline orchestration.

Stages, and what triggers each:

  1. step 1 confirm      → tile capture → gpt-image-2 aerial          [start]
  2. step 2 photo upload → stored immediately, never gated            [router]
  3. whichever of (1) and (2) finishes SECOND closes the gate and fires the
     drone generation. Neither knows which one it is; videotour/db.py decides
     with a compare-and-swap and tells exactly one caller to proceed.
  4. drone clip → interior tour → stitch → ready                      [_run_from]

Everything runs as a detached asyncio task on the app's own loop. There is no
worker process: a Render disk attaches to exactly one service, and the media
has to live on the same disk as the database, so the pipeline stays in-process.
That means a tile capture and an ffmpeg encode share a CPU with live phone
calls — the accepted trade for keeping the media on local disk.

Failures never raise into the caller. A job that breaks is marked failed with
the stage recorded, and step 3's retry button re-enters at whatever is still
missing (db.resume_stage), so a stitch failure never re-pays for two
generations that already succeeded.
"""

import asyncio
import logging
from typing import Any, Optional

from config import settings
from usage import db as usage_db
from videotour import content, db, ffmpeg, photoreal, runway, storage, tiles

logger = logging.getLogger(__name__)

# Detached tasks are kept referenced until they finish; without this the event
# loop is free to garbage-collect a running pipeline mid-generation.
_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def _meter(tenant_id: str, job_id: str) -> None:
    """Charge one video tour against the tenant's monthly credits.

    Called only once a tour is actually deliverable, and keyed on the job id so
    a retried job is billed once rather than once per attempt. Never raises:
    the agency has their video by this point, and failing the request now would
    report a broken tool that is not broken (same reasoning as
    acquisizione/router.py's _meter).
    """
    try:
        await asyncio.to_thread(usage_db.record, tenant_id, "video_tour", job_id)
    except Exception:
        logger.exception("Failed to meter video tour %s for tenant %s", job_id, tenant_id)


def _interior_seconds(room_count: int) -> int:
    """How long the single interior generation should run.

    Clamped to Seedance's 4-30 second range. The upper bound is doing real work
    here: it is what stops an agent who uploads eight rooms from costing twice
    what four rooms costs. Past the ceiling, rooms get less screen time rather
    than the tour getting more expensive.
    """
    wanted = 2 + settings.VIDEO_TOUR_SECONDS_PER_ROOM * max(1, room_count)
    return max(4, min(30, wanted))


async def _reference_uri(relative: str) -> str:
    """Load a stored still and encode it for Runway.

    Images are already downscaled at upload (see router._store_photo), so this is
    normally a straight read; the size check in runway.data_uri is the backstop
    for anything that slipped through.
    """
    path = storage.resolve(relative)
    if path is None or not path.is_file():
        raise runway.RunwayError("Reference image is missing: %s" % relative)
    data = await asyncio.to_thread(path.read_bytes)
    suffix = path.suffix.lower()
    content_type = "image/png" if suffix == ".png" else "image/jpeg"
    return runway.data_uri(data, content_type)


# ── stage 1: tile capture + photoreal aerial ────────────────────────────────
async def start(job: dict[str, Any]) -> None:
    """Fired on step 1 confirm. Runs the capture and the aerial conversion, then
    closes the gate if the agent has already uploaded their exterior photo."""
    _spawn(_run_intro(job["id"], job["tenant_id"], job["lat"], job["lng"]))


async def _run_intro(job_id: str, tenant_id: str, lat: float, lng: float) -> None:
    try:
        png = await tiles.capture(lat, lng)
        tile_path = await asyncio.to_thread(
            storage.write, tenant_id, job_id, "tile.png", png,
        )
        if not await asyncio.to_thread(db.set_tile_image, job_id, tenant_id, tile_path):
            # The job was abandoned while the capture ran. Stop quietly rather
            # than paying OpenAI for an aerial nobody will see.
            logger.info("Video tour %s: job moved on during tile capture", job_id)
            return
    except Exception as exc:
        await asyncio.to_thread(db.fail, job_id, tenant_id, "tile", str(exc))
        return

    try:
        aerial = await photoreal.to_aerial(png)
        aerial_path = await asyncio.to_thread(
            storage.write, tenant_id, job_id, "aerial.png", aerial,
        )
    except Exception as exc:
        await asyncio.to_thread(db.fail, job_id, tenant_id, "photoreal", str(exc))
        return

    result = await asyncio.to_thread(db.set_photoreal, job_id, tenant_id, aerial_path)
    if result == db.GENERATING_VIDEO:
        # We were second: the photo is already in. Fire the drone generation.
        await resume(job_id, tenant_id, db.STAGE_DRONE)


async def on_exterior_photo_stored(job_id: str, tenant_id: str, gate: str) -> None:
    """Called by the router right after the exterior photo lands. `gate` is what
    db.set_exterior_photo returned — only GENERATING_VIDEO means this upload was
    the second input and the drone generation is ours to start."""
    if gate == db.GENERATING_VIDEO:
        await resume(job_id, tenant_id, db.STAGE_DRONE)


# ── stages 4-7: generation, interior tour, stitch ───────────────────────────
async def resume(job_id: str, tenant_id: str, stage: str) -> None:
    """Run the pipeline from `stage` onward, detached."""
    _spawn(_run_from(job_id, tenant_id, stage))


async def _run_from(job_id: str, tenant_id: str, stage: str) -> None:
    job = await asyncio.to_thread(db.get, job_id, tenant_id)
    if job is None:
        return

    # A stage only proceeds while the job is still live. Without this an agent
    # who abandons the wizard mid-generation still gets billed for every
    # remaining Runway call: the previous stage's status transition quietly
    # fails, and the next one would run anyway on the stale job dict.
    def still_live(current: Optional[dict[str, Any]]) -> bool:
        return bool(current) and current["status"] in db.LIVE_STATUSES

    try:
        if stage == db.STAGE_DRONE:
            await _generate_drone(job)
            job = await asyncio.to_thread(db.get, job_id, tenant_id)
            if not still_live(job):
                logger.info("Video tour %s: abandoned before the interior tour", job_id)
                return
            stage = db.STAGE_INTERIOR
        if stage == db.STAGE_INTERIOR:
            await _generate_interior(job)
            job = await asyncio.to_thread(db.get, job_id, tenant_id)
            if not still_live(job):
                logger.info("Video tour %s: abandoned before the stitch", job_id)
                return
            stage = db.STAGE_STITCH
        if stage == db.STAGE_STITCH:
            await _stitch(job)
    except Exception as exc:
        await asyncio.to_thread(db.fail, job_id, tenant_id, stage, str(exc))


async def _generate_drone(job: dict[str, Any]) -> None:
    """The fly-in: photoreal aerial for the zone, real exterior photo for the
    building. Reference mode, so neither is a pinned frame."""
    job_id, tenant_id = job["id"], job["tenant_id"]
    references = [
        await _reference_uri(job["photoreal_image_path"]),
        await _reference_uri(job["exterior_photo_path"]),
    ]

    async def progress(pct: int) -> None:
        # The drone clip occupies 40-60% of the overall bar.
        await asyncio.to_thread(
            db.set_progress, job_id, tenant_id, 40 + int(pct * 0.2),
        )

    task_id = await runway.start_generation(
        references,
        content.drone_prompt(),
        settings.VIDEO_TOUR_DRONE_SECONDS,
    )
    await asyncio.to_thread(db.set_runway_task, job_id, tenant_id, task_id)
    url = await runway.poll(task_id, progress)
    video = await runway.download(url)
    path = await asyncio.to_thread(
        storage.write, tenant_id, job_id, "drone.mp4", video,
    )
    await asyncio.to_thread(db.set_drone_clip, job_id, tenant_id, path)


async def _generate_interior(job: dict[str, Any]) -> None:
    """One generation covering every room, with the exterior photo first so the
    tour opens on the building the drone shot just arrived at."""
    job_id, tenant_id = job["id"], job["tenant_id"]
    rooms = job.get("interior_photo_paths") or []
    if not rooms:
        raise runway.RunwayError("No interior photos were uploaded")

    # Order matters: the prompt addresses these as @Image1 (exterior) then
    # @Image2…@ImageN (rooms, in upload order).
    references = [await _reference_uri(job["exterior_photo_path"])]
    for room in rooms:
        references.append(await _reference_uri(room))

    seconds = _interior_seconds(len(rooms))

    async def progress(pct: int) -> None:
        # The interior tour occupies 60-85% of the overall bar.
        await asyncio.to_thread(
            db.set_progress, job_id, tenant_id, 60 + int(pct * 0.25),
        )

    task_id = await runway.start_generation(
        references,
        content.interior_prompt(len(rooms), settings.VIDEO_TOUR_SECONDS_PER_ROOM),
        seconds,
    )
    await asyncio.to_thread(db.set_runway_task, job_id, tenant_id, task_id)
    url = await runway.poll(task_id, progress)
    video = await runway.download(url)
    path = await asyncio.to_thread(
        storage.write, tenant_id, job_id, "interior.mp4", video,
    )
    await asyncio.to_thread(db.set_interior_clip, job_id, tenant_id, path)


async def _stitch(job: dict[str, Any]) -> None:
    """Join the two clips with a short crossfade, then finish the job."""
    job_id, tenant_id = job["id"], job["tenant_id"]
    drone = storage.resolve(job["drone_clip_path"])
    interior = storage.resolve(job["interior_clip_path"])
    if drone is None or interior is None:
        raise ffmpeg.FfmpegError("A generated clip is missing from disk")

    target = storage.job_dir(tenant_id, job_id) / "tour.mp4"
    await ffmpeg.stitch(drone, interior, target)

    stored = storage.rel(tenant_id, job_id, "tour.mp4")
    if await asyncio.to_thread(db.set_ready, job_id, tenant_id, stored):
        await _meter(tenant_id, job_id)
        # The working files are ~40% of the job's footprint and nothing reads
        # them again. Dropped here rather than on the sweeper's schedule so the
        # disk is reclaimed the moment the tour is deliverable.
        await asyncio.to_thread(storage.drop_intermediates, tenant_id, job_id)


# ── retention sweeper ───────────────────────────────────────────────────────
async def sweep_once() -> None:
    """Delete the media of jobs past their retention window.

    Runs across all tenants. The job ROWS survive — they are the record that a
    tour was generated and billed — but their files go, and the paths are
    nulled so the dashboard stops offering a video that is no longer there.
    """
    jobs = await asyncio.to_thread(
        db.sweepable,
        settings.VIDEO_TOUR_KEEP_READY_DAYS,
        settings.VIDEO_TOUR_KEEP_DEAD_DAYS,
    )
    for job in jobs:
        await asyncio.to_thread(storage.delete_job, job["tenant_id"], job["id"])
        await asyncio.to_thread(db.forget_media, job["id"])
    used = await asyncio.to_thread(storage.disk_usage_bytes)
    if jobs or used:
        logger.info(
            "Video tour sweep: reclaimed %d job(s), %.1f MB still in use",
            len(jobs), used / 1_048_576,
        )


async def sweep_loop() -> None:
    """Periodic retention sweep, started from main.py's lifespan."""
    while True:
        try:
            await sweep_once()
        except Exception:
            logger.exception("Video tour sweep failed")
        await asyncio.sleep(settings.VIDEO_TOUR_SWEEP_INTERVAL_SECONDS)

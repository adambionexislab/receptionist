"""Media on disk for video-tour jobs.

This is the first feature in the codebase to persist media at all — the photo
enhancer deliberately streams its output straight back and stores nothing (see
acquisizione/photos.py), and until now /data held only receptionist.db. That
makes disk budget a real constraint rather than an afterthought:

  uploads (1 exterior + up to 8 interiors)   ~30 MB
  tile capture + gpt-image-2 aerial          ~5 MB
  drone clip + interior clip                 ~55 MB
  final stitched video                       ~55 MB
                                             ─────────
  peak, mid-pipeline                        ~145 MB

The disk is shared with the SQLite file that every phone call writes to, so a
full disk is not a degraded video feature — it is an outage. Two things keep
that from happening: intermediates are deleted the moment a tour reaches
'ready' (drop_intermediates), and the sweeper reclaims whole job directories on
a schedule (sweep). Neither is optional.

Layout, one directory per job so a delete is a single rmtree:

  $DATA_DIR/video_tours/{tenant_id}/{job_id}/
      exterior.jpg  interior-0.jpg …  tile.png  aerial.png
      drone.mp4  interior.mp4  tour.mp4

Paths are stored in the DB relative to the video_tours root, never absolute:
the mount point is configuration, and an absolute path in a row would rot the
moment DATA_DIR changed.
"""

import logging
import shutil
from pathlib import Path
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

_ROOT_NAME = "video_tours"

# Files that exist only to produce the final video. Deleted on success — they
# are ~40% of a finished job's footprint and nothing reads them again.
_INTERMEDIATES = ("tile.png", "aerial.png", "drone.mp4", "interior.mp4")


def root() -> Path:
    return Path(settings.DATA_DIR) / _ROOT_NAME


def job_dir(tenant_id: str, job_id: str) -> Path:
    """The one directory holding everything for a job. Tenant-segmented so a
    path traversal bug can't reach another agency's media, and so a tenant can
    be purged wholesale if they ever leave."""
    return root() / tenant_id / job_id


def ensure_job_dir(tenant_id: str, job_id: str) -> Path:
    path = job_dir(tenant_id, job_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def rel(tenant_id: str, job_id: str, filename: str) -> str:
    """The DB-storable path for a file: relative to the video_tours root."""
    return f"{tenant_id}/{job_id}/{filename}"


def resolve(relative: Optional[str]) -> Optional[Path]:
    """Absolute path for a stored relative path, or None.

    Refuses anything that escapes the video_tours root. Stored paths are
    server-generated so this should never trigger, but the check is cheap and
    the failure mode — serving an arbitrary file off the disk — is not.
    """
    if not relative:
        return None
    base = root().resolve()
    candidate = (base / relative).resolve()
    if base not in candidate.parents and candidate != base:
        logger.error("Refusing path outside the video_tours root: %s", relative)
        return None
    return candidate


def write(tenant_id: str, job_id: str, filename: str, data: bytes) -> str:
    """Write one file into the job directory and return its stored path."""
    target = ensure_job_dir(tenant_id, job_id) / filename
    target.write_bytes(data)
    logger.info(
        "Video tour %s: wrote %s (%.1f MB)", job_id, filename, len(data) / 1_048_576,
    )
    return rel(tenant_id, job_id, filename)


def drop_intermediates(tenant_id: str, job_id: str) -> None:
    """Delete the working files of a finished tour, keeping the final video and
    the agent's own uploads. Best-effort: a tour that is already 'ready' must
    never be failed by a janitor problem."""
    directory = job_dir(tenant_id, job_id)
    for name in _INTERMEDIATES:
        try:
            (directory / name).unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not drop intermediate %s for job %s", name, job_id)


def delete_job(tenant_id: str, job_id: str) -> None:
    """Remove a job's entire directory. Best-effort, for the same reason."""
    try:
        shutil.rmtree(job_dir(tenant_id, job_id), ignore_errors=True)
    except OSError:
        logger.warning("Could not delete job directory for %s", job_id)


def disk_usage_bytes() -> int:
    """Total bytes under the video_tours root. Logged by the sweeper so a disk
    filling up is visible before it takes the database down with it."""
    total = 0
    base = root()
    if not base.is_dir():
        return 0
    for path in base.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total

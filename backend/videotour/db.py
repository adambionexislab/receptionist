"""SQLite persistence for AI video-tour jobs.

One table — video_tour_jobs — lives on the SAME connection as the tenants
registry (see tenants/db.py), reusing its process-wide connection and write
lock, exactly like calls/db.py, acquisizione/db.py and usage/db.py. Every row
carries tenant_id: it is the scoping column for the agency dashboard, and
every read here filters on it so one tenant can never see another's tour.

Only paths live here, never bytes. The media itself (uploaded photos, the tile
capture, the generated clips, the final video) is written to the Render disk by
videotour/storage.py — a video is far too big for a SQLite row, and the disk is
already mounted for the database anyway.

THE STEP-2 GATE
---------------
The pipeline has two producers that finish in an unpredictable order:

  A. the gpt-image-2 photoreal aerial (started when the address is confirmed)
  B. the exterior photo the agent uploads in step 2

Runway can only be called once BOTH exist. Rather than model that as extra
statuses, the two inputs are plain nullable columns and the transition is a
compare-and-swap: each writer stores its own column, then attempts the SAME
conditional UPDATE. SQLite serialises them on the shared write lock, so exactly
one of them sees both columns populated and gets rowcount == 1. That caller —
and only that caller — fires the drone generation. Neither producer needs to
know whether it was first or second, and there is no scheduler to miss a wakeup.

RETRY
-----
A failed job resumes from what is MISSING, not from a recorded stage name:
drone_clip_path IS NULL means redo the drone clip, interior_clip_path IS NULL
means redo the interior tour, otherwise redo the stitch. Deriving it from
artifacts rather than bookkeeping means retry can never re-run — and re-bill —
a Runway call whose output is already sitting on disk. `failed_stage` and
`error` are kept for the dashboard and the logs, and are never read back as
control flow.
"""

import datetime
import json
import logging
import uuid
from typing import Any, Optional

from tenants import db as _tenants_db

logger = logging.getLogger(__name__)

# ── statuses ────────────────────────────────────────────────────────────────
# The single user-facing lifecycle column. The step-2 gate inputs are data
# columns (photoreal_image_path / exterior_photo_path), NOT statuses, so the
# two producers can complete in either order without doubling this list.
RENDERING_TILE = "rendering_tile"            # headless Cesium capture running
GENERATING_PHOTOREAL = "generating_photoreal"  # gpt-image-2 running
AWAITING_INPUTS = "awaiting_inputs"          # photoreal done, waiting on the photo
GENERATING_VIDEO = "generating_video"        # Runway drone clip
GENERATING_INTERIOR = "generating_interior"  # Runway interior tour
STITCHING = "stitching"                      # ffmpeg
READY = "ready"
FAILED = "failed"
ABANDONED = "abandoned"                      # user walked away; swept early

# Statuses a job can still be worked on from. Used to decide whether a late
# photo upload is still worth storing.
LIVE_STATUSES = (
    RENDERING_TILE, GENERATING_PHOTOREAL, AWAITING_INPUTS,
    GENERATING_VIDEO, GENERATING_INTERIOR, STITCHING,
)

# What step 3's player renders. The wizard only knows these four; the backend
# statuses above are an implementation detail it never has to track.
PLAYER_STATE = {
    RENDERING_TILE: "preparing",
    GENERATING_PHOTOREAL: "preparing",
    AWAITING_INPUTS: "preparing",
    GENERATING_VIDEO: "generating",
    GENERATING_INTERIOR: "generating",
    STITCHING: "generating",
    READY: "ready",
    FAILED: "error",
    ABANDONED: "error",
}

# Retry stages, derived from missing artifacts (see the module docstring).
STAGE_DRONE = "drone"
STAGE_INTERIOR = "interior"
STAGE_STITCH = "stitch"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS video_tour_jobs (
  id                    TEXT PRIMARY KEY,
  tenant_id             TEXT NOT NULL,
  listing_id            TEXT,
  locale                TEXT NOT NULL DEFAULT 'it',
  status                TEXT NOT NULL DEFAULT 'rendering_tile',

  address               TEXT NOT NULL,
  lat                   REAL NOT NULL,
  lng                   REAL NOT NULL,

  tile_image_path       TEXT,
  photoreal_image_path  TEXT,
  exterior_photo_path   TEXT,
  interior_photo_paths  TEXT,
  drone_clip_path       TEXT,
  interior_clip_path    TEXT,
  video_path            TEXT,

  progress_pct          INTEGER NOT NULL DEFAULT 0,
  runway_task_id        TEXT,
  failed_stage          TEXT,
  error                 TEXT,
  attempts              INTEGER NOT NULL DEFAULT 0,

  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  completed_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_video_tour_tenant
  ON video_tour_jobs(tenant_id, created_at);

-- The retention sweeper scans by age across all tenants (see storage.py).
CREATE INDEX IF NOT EXISTS idx_video_tour_sweep
  ON video_tour_jobs(status, updated_at);
"""

# Columns added after the table first shipped. CREATE TABLE IF NOT EXISTS won't
# alter an existing table on a deployed disk, so each is added with an
# idempotent ALTER on startup — same pattern as tenants/db.py, listings/db.py
# and acquisizione/db.py.
_ADDED_COLUMNS: dict[str, str] = {}

_JSON_FIELDS = ("interior_photo_paths",)

_initialized = False


def init() -> None:
    """Create the video_tour_jobs table on the shared connection (idempotent)."""
    global _initialized
    conn = _tenants_db.get_connection()
    if _initialized:
        return
    with _tenants_db.write_lock:
        if not _initialized:
            conn.executescript(_SCHEMA)
            _migrate(conn)
            conn.commit()
            _initialized = True
            logger.info("Video tour table ready (video_tour_jobs)")


def _migrate(conn) -> None:
    """Add columns introduced after the table first shipped. Idempotent: each
    column is added only if a pre-existing table is missing it."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(video_tour_jobs)")}
    for column, ddl in _ADDED_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE video_tour_jobs ADD COLUMN {column} {ddl}")
            logger.info("Migrated video_tour_jobs table: added column %s", column)


def _conn():
    if not _initialized:
        init()
    return _tenants_db.get_connection()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _row_to_dict(row) -> dict[str, Any]:
    job = dict(row)
    for field in _JSON_FIELDS:
        raw = job.get(field)
        job[field] = json.loads(raw) if raw else []
    job["player_state"] = PLAYER_STATE.get(job["status"], "error")
    return job


# ── create / read ───────────────────────────────────────────────────────────
def create(
    tenant_id: str,
    locale: str,
    address: str,
    lat: float,
    lng: float,
    listing_id: Optional[str] = None,
) -> dict[str, Any]:
    """Open a job at step 1 confirm. Starts in 'rendering_tile' — the tile
    capture is fired by the caller immediately after this returns, so the job
    is already working before the agent has uploaded a single photo."""
    now = _now()
    job = {
        "id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "listing_id": listing_id,
        "locale": locale or "it",
        "status": RENDERING_TILE,
        "address": address,
        "lat": float(lat),
        "lng": float(lng),
        "created_at": now,
        "updated_at": now,
    }
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            "INSERT INTO video_tour_jobs "
            "(id, tenant_id, listing_id, locale, status, address, lat, lng, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job["id"], job["tenant_id"], job["listing_id"], job["locale"],
                job["status"], job["address"], job["lat"], job["lng"],
                job["created_at"], job["updated_at"],
            ),
        )
        conn.commit()
    logger.info(
        "Video tour job created: %s (tenant=%s lat=%s lng=%s)",
        job["id"], tenant_id, lat, lng,
    )
    return get(job["id"], tenant_id) or job


def get(job_id: str, tenant_id: str) -> Optional[dict[str, Any]]:
    """Fetch one job, strictly scoped to tenant_id. None if missing or owned by
    a different tenant."""
    row = _conn().execute(
        "SELECT * FROM video_tour_jobs WHERE id = ? AND tenant_id = ?",
        (job_id, tenant_id),
    ).fetchone()
    return _row_to_dict(row) if row else None


def list_for_tenant(tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
    """Most-recent jobs for ONE tenant. Strictly scoped by tenant_id."""
    limit = max(1, min(limit, 500))
    rows = _conn().execute(
        "SELECT * FROM video_tour_jobs WHERE tenant_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (tenant_id, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ── stage 1: tile capture ───────────────────────────────────────────────────
def set_tile_image(job_id: str, tenant_id: str, path: str) -> bool:
    """Tile capture done — move to the gpt-image-2 pass. Only from
    'rendering_tile', so a duplicate capture can't rewind a job."""
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "UPDATE video_tour_jobs SET tile_image_path = ?, status = ?, "
            "progress_pct = 15, updated_at = ? "
            "WHERE id = ? AND tenant_id = ? AND status = ?",
            (path, GENERATING_PHOTOREAL, _now(), job_id, tenant_id, RENDERING_TILE),
        )
        conn.commit()
    return cur.rowcount > 0


# ── the step-2 gate ─────────────────────────────────────────────────────────
# Both writers below store their own column and then attempt the same
# compare-and-swap. Exactly one gets rowcount == 1 and returns GENERATING_VIDEO;
# that caller fires the Runway drone generation. See the module docstring.

def set_photoreal(job_id: str, tenant_id: str, path: str) -> Optional[str]:
    """Gate input A: the gpt-image-2 aerial is ready.

    Returns GENERATING_VIDEO when this call closed the gate (the exterior photo
    was already uploaded — fire Runway), AWAITING_INPUTS when we're now waiting
    on the photo, or None if the job moved on / doesn't exist.
    """
    now = _now()
    conn = _conn()
    with _tenants_db.write_lock:
        stored = conn.execute(
            "UPDATE video_tour_jobs SET photoreal_image_path = ?, updated_at = ? "
            "WHERE id = ? AND tenant_id = ? AND status = ?",
            (path, now, job_id, tenant_id, GENERATING_PHOTOREAL),
        )
        if stored.rowcount == 0:
            conn.commit()
            return None
        closed = conn.execute(
            "UPDATE video_tour_jobs SET status = ?, progress_pct = 40, updated_at = ? "
            "WHERE id = ? AND tenant_id = ? AND status = ? "
            "AND exterior_photo_path IS NOT NULL",
            (GENERATING_VIDEO, now, job_id, tenant_id, GENERATING_PHOTOREAL),
        )
        if closed.rowcount > 0:
            conn.commit()
            logger.info("Video tour %s: photoreal closed the gate", job_id)
            return GENERATING_VIDEO
        conn.execute(
            "UPDATE video_tour_jobs SET status = ?, progress_pct = 30, updated_at = ? "
            "WHERE id = ? AND tenant_id = ? AND status = ?",
            (AWAITING_INPUTS, now, job_id, tenant_id, GENERATING_PHOTOREAL),
        )
        conn.commit()
    logger.info("Video tour %s: photoreal ready, waiting on exterior photo", job_id)
    return AWAITING_INPUTS


def set_exterior_photo(job_id: str, tenant_id: str, path: str) -> Optional[str]:
    """Gate input B: the agent uploaded the exterior photo.

    Stored immediately and unconditionally while the job is live — step 2 must
    never block on anything. Returns GENERATING_VIDEO when this call closed the
    gate (fire Runway), 'stored' when the photoreal isn't ready yet, or None if
    the job is gone / already past the point where the photo matters.
    """
    now = _now()
    conn = _conn()
    with _tenants_db.write_lock:
        placeholders = ", ".join("?" for _ in LIVE_STATUSES)
        stored = conn.execute(
            f"UPDATE video_tour_jobs SET exterior_photo_path = ?, updated_at = ? "
            f"WHERE id = ? AND tenant_id = ? AND status IN ({placeholders})",
            (path, now, job_id, tenant_id, *LIVE_STATUSES),
        )
        if stored.rowcount == 0:
            conn.commit()
            return None
        closed = conn.execute(
            "UPDATE video_tour_jobs SET status = ?, progress_pct = 40, updated_at = ? "
            "WHERE id = ? AND tenant_id = ? AND status = ? "
            "AND photoreal_image_path IS NOT NULL",
            (GENERATING_VIDEO, now, job_id, tenant_id, AWAITING_INPUTS),
        )
        conn.commit()
    if closed.rowcount > 0:
        logger.info("Video tour %s: exterior photo closed the gate", job_id)
        return GENERATING_VIDEO
    return "stored"


def set_interior_photos(job_id: str, tenant_id: str, paths: list[str]) -> bool:
    """Store the interior photo paths from step 2. Independent of the gate —
    the interior tour isn't generated until the drone clip is done, so these
    can keep arriving while Runway is already working."""
    conn = _conn()
    with _tenants_db.write_lock:
        placeholders = ", ".join("?" for _ in LIVE_STATUSES)
        cur = conn.execute(
            f"UPDATE video_tour_jobs SET interior_photo_paths = ?, updated_at = ? "
            f"WHERE id = ? AND tenant_id = ? AND status IN ({placeholders})",
            (json.dumps(paths), _now(), job_id, tenant_id, *LIVE_STATUSES),
        )
        conn.commit()
    return cur.rowcount > 0


# ── generation stages ───────────────────────────────────────────────────────
def set_runway_task(job_id: str, tenant_id: str, task_id: Optional[str]) -> None:
    """Remember the in-flight Runway task id so a process restart can pick the
    poll back up instead of paying for the generation twice."""
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            "UPDATE video_tour_jobs SET runway_task_id = ?, updated_at = ? "
            "WHERE id = ? AND tenant_id = ?",
            (task_id, _now(), job_id, tenant_id),
        )
        conn.commit()


def set_progress(job_id: str, tenant_id: str, pct: int) -> None:
    """Progress for step 3's 'generating' state. Never moves backwards, so a
    slow poll arriving late can't make the bar jump back."""
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            "UPDATE video_tour_jobs SET progress_pct = ?, updated_at = ? "
            "WHERE id = ? AND tenant_id = ? AND progress_pct < ?",
            (int(pct), _now(), job_id, tenant_id, int(pct)),
        )
        conn.commit()


def set_drone_clip(job_id: str, tenant_id: str, path: str) -> bool:
    """Drone clip downloaded to disk — move on to the interior tour. Accepts
    the transition from 'generating_video' or from a retry that re-entered at
    the drone stage while the job sat in 'failed'."""
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "UPDATE video_tour_jobs SET drone_clip_path = ?, status = ?, "
            "runway_task_id = NULL, progress_pct = 60, updated_at = ? "
            "WHERE id = ? AND tenant_id = ? AND status IN (?, ?)",
            (
                path, GENERATING_INTERIOR, _now(), job_id, tenant_id,
                GENERATING_VIDEO, FAILED,
            ),
        )
        conn.commit()
    return cur.rowcount > 0


def set_interior_clip(job_id: str, tenant_id: str, path: str) -> bool:
    """The single multi-room interior clip is on disk — move on to the stitch."""
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "UPDATE video_tour_jobs SET interior_clip_path = ?, status = ?, "
            "runway_task_id = NULL, progress_pct = 85, updated_at = ? "
            "WHERE id = ? AND tenant_id = ? AND status IN (?, ?)",
            (path, STITCHING, _now(), job_id, tenant_id, GENERATING_INTERIOR, FAILED),
        )
        conn.commit()
    return cur.rowcount > 0


def set_ready(job_id: str, tenant_id: str, path: str) -> bool:
    """Final stitched video stored. Terminal success."""
    now = _now()
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "UPDATE video_tour_jobs SET video_path = ?, status = ?, "
            "progress_pct = 100, failed_stage = NULL, error = NULL, "
            "updated_at = ?, completed_at = ? "
            "WHERE id = ? AND tenant_id = ? AND status IN (?, ?)",
            (path, READY, now, now, job_id, tenant_id, STITCHING, FAILED),
        )
        conn.commit()
    if cur.rowcount > 0:
        logger.info("Video tour %s ready", job_id)
    return cur.rowcount > 0


# ── failure / retry ─────────────────────────────────────────────────────────
def fail(job_id: str, tenant_id: str, stage: str, error: str) -> None:
    """Mark a job failed. `stage` and `error` are for the dashboard and the
    logs only — retry works out where to resume from the artifacts on disk
    (see resume_stage), never from this column."""
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            "UPDATE video_tour_jobs SET status = ?, failed_stage = ?, error = ?, "
            "runway_task_id = NULL, updated_at = ? "
            "WHERE id = ? AND tenant_id = ? AND status != ?",
            (FAILED, stage, error[:500], _now(), job_id, tenant_id, READY),
        )
        conn.commit()
    logger.error("Video tour %s failed at %s: %s", job_id, stage, error)


def resume_stage(job: dict[str, Any]) -> str:
    """Which stage a retry should re-enter at, derived from what is missing on
    disk rather than from failed_stage.

    This is what makes retry cheap and idempotent: a stitch that failed after
    two successful Runway calls re-runs only ffmpeg, and can never re-bill the
    generations whose output is already sitting in the job directory.
    """
    if not job.get("drone_clip_path"):
        return STAGE_DRONE
    if not job.get("interior_clip_path"):
        return STAGE_INTERIOR
    return STAGE_STITCH


def _can_resume(job: dict[str, Any], stage: str) -> bool:
    """Whether `stage` has everything it needs to run.

    Retry deliberately never re-runs the tile capture, so a job that failed
    BEFORE producing an aerial has nothing for the drone stage to work from.
    Retrying it would fail again on the first missing reference and burn one of
    a small number of attempts, so it is refused up front instead — the agent
    is told the tour cannot be retried rather than watching it fail twice.
    """
    if stage == STAGE_DRONE:
        return bool(job.get("photoreal_image_path")) and bool(job.get("exterior_photo_path"))
    if stage == STAGE_INTERIOR:
        return bool(job.get("exterior_photo_path")) and bool(job.get("interior_photo_paths"))
    return bool(job.get("drone_clip_path")) and bool(job.get("interior_clip_path"))


def begin_retry(job_id: str, tenant_id: str, max_attempts: int) -> Optional[str]:
    """Claim a failed job for one more run. Returns the stage to resume at, or
    None if the job is not retryable or has burned its attempts.

    The attempt counter matters here in a way it would not for a free pipeline:
    every drone/interior retry spends real Runway credits, so a job failing
    persistently must stop rather than bill the agency in a loop.
    """
    conn = _conn()
    with _tenants_db.write_lock:
        row = conn.execute(
            "SELECT * FROM video_tour_jobs WHERE id = ? AND tenant_id = ?",
            (job_id, tenant_id),
        ).fetchone()
        if row is None or row["status"] != FAILED:
            return None
        if int(row["attempts"] or 0) >= max_attempts:
            logger.warning("Video tour %s exhausted %d retries", job_id, max_attempts)
            return None
        job = _row_to_dict(row)
        stage = resume_stage(job)
        if not _can_resume(job, stage):
            logger.info(
                "Video tour %s cannot resume at %s — inputs are missing", job_id, stage,
            )
            return None
        status = {
            STAGE_DRONE: GENERATING_VIDEO,
            STAGE_INTERIOR: GENERATING_INTERIOR,
            STAGE_STITCH: STITCHING,
        }[stage]
        conn.execute(
            "UPDATE video_tour_jobs SET status = ?, attempts = attempts + 1, "
            "error = NULL, updated_at = ? WHERE id = ? AND tenant_id = ? AND status = ?",
            (status, _now(), job_id, tenant_id, FAILED),
        )
        conn.commit()
    logger.info("Video tour %s retrying from stage %s", job_id, stage)
    return stage


def abandon(job_id: str, tenant_id: str) -> bool:
    """The agent walked away mid-wizard. Nothing is deleted here — the status
    is what changes, so the retention sweeper can reclaim the media early
    instead of holding it for the full window."""
    conn = _conn()
    with _tenants_db.write_lock:
        placeholders = ", ".join("?" for _ in LIVE_STATUSES)
        cur = conn.execute(
            f"UPDATE video_tour_jobs SET status = ?, updated_at = ? "
            f"WHERE id = ? AND tenant_id = ? AND status IN ({placeholders})",
            (ABANDONED, _now(), job_id, tenant_id, *LIVE_STATUSES),
        )
        conn.commit()
    return cur.rowcount > 0


# ── retention (see videotour/storage.py for the disk side) ──────────────────
def sweepable(ready_days: int, dead_days: int) -> list[dict[str, Any]]:
    """Jobs whose media is due for deletion, across ALL tenants.

    Deliberately not tenant-scoped: this is the janitor, not a dashboard read.
    Finished tours are kept for `ready_days`; abandoned and failed ones are
    dropped after `dead_days`, since nobody is coming back for them and their
    intermediates are the bulk of the disk usage.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    ready_cutoff = (now - datetime.timedelta(days=ready_days)).isoformat()
    dead_cutoff = (now - datetime.timedelta(days=dead_days)).isoformat()
    rows = _conn().execute(
        "SELECT * FROM video_tour_jobs WHERE "
        "(status = ? AND updated_at < ?) OR (status IN (?, ?) AND updated_at < ?)",
        (READY, ready_cutoff, ABANDONED, FAILED, dead_cutoff),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def forget_media(job_id: str) -> None:
    """Null out the media paths after the sweeper has deleted the files, so the
    row stops advertising a video that is no longer on disk. The job row itself
    is kept: it is the record that the tour was generated and billed."""
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            "UPDATE video_tour_jobs SET tile_image_path = NULL, "
            "photoreal_image_path = NULL, exterior_photo_path = NULL, "
            "interior_photo_paths = NULL, drone_clip_path = NULL, "
            "interior_clip_path = NULL, video_path = NULL, updated_at = ? "
            "WHERE id = ?",
            (_now(), job_id),
        )
        conn.commit()

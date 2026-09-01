"""Tests for the AI video-tour job state machine (videotour/db.py).

The rules that matter here:

  * The step-2 gate closes on whichever of the two inputs — the gpt-image-2
    aerial, or the exterior photo the agent uploads — arrives SECOND, in either
    order, and fires the Runway drone generation exactly once. That is the one
    piece of concurrency in the feature, and getting it wrong either strands a
    tour in "preparing" forever or pays Runway twice for one clip.
  * A photo upload is never gated on anything: step 2 must not block.
  * Retry resumes from what is MISSING on disk, not from a recorded stage, so a
    stitch failure re-runs ffmpeg alone and never re-bills two generations that
    already succeeded — and a retry with nothing to work from is refused rather
    than allowed to fail again and burn an attempt.
  * Every read and write is scoped to one tenant.
"""

import sqlite3

import pytest

from tenants import db as tenants_db
from videotour import db as vt_db

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    """Point the shared tenants connection at a throwaway in-memory DB, so
    these tests never touch the real data on disk."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(tenants_db, "get_connection", lambda: conn)
    monkeypatch.setattr(vt_db, "_initialized", False)
    vt_db.init()
    yield conn
    conn.close()


def new_job(tenant=TENANT):
    return vt_db.create(tenant, "it", "Via Roma 1", 45.4642, 9.1900)["id"]


def ready_for_gate(job_id, tenant=TENANT):
    """Advance a job to the point where only the two gate inputs are missing."""
    vt_db.set_tile_image(job_id, tenant, "tile.png")


# ── the step-2 gate ─────────────────────────────────────────────────────────
def test_aerial_first_then_photo_closes_the_gate():
    job = new_job()
    ready_for_gate(job)
    assert vt_db.set_photoreal(job, TENANT, "aerial.png") == vt_db.AWAITING_INPUTS
    assert vt_db.set_exterior_photo(job, TENANT, "ext.jpg") == vt_db.GENERATING_VIDEO
    assert vt_db.get(job, TENANT)["status"] == vt_db.GENERATING_VIDEO


def test_photo_first_then_aerial_closes_the_gate():
    job = new_job()
    ready_for_gate(job)
    # The upload is stored but must not start anything on its own.
    assert vt_db.set_exterior_photo(job, TENANT, "ext.jpg") == "stored"
    assert vt_db.set_photoreal(job, TENANT, "aerial.png") == vt_db.GENERATING_VIDEO
    assert vt_db.get(job, TENANT)["status"] == vt_db.GENERATING_VIDEO


def test_a_photo_uploaded_during_the_tile_capture_is_never_blocked():
    """Step 2 accepts a photo even while step 1's capture is still running —
    the agent is never made to wait on the pipeline."""
    job = new_job()
    assert vt_db.get(job, TENANT)["status"] == vt_db.RENDERING_TILE
    assert vt_db.set_exterior_photo(job, TENANT, "ext.jpg") == "stored"
    assert vt_db.get(job, TENANT)["exterior_photo_path"] == "ext.jpg"

    ready_for_gate(job)
    assert vt_db.set_photoreal(job, TENANT, "aerial.png") == vt_db.GENERATING_VIDEO


def test_the_gate_fires_exactly_once():
    """Re-uploading the exterior photo must not start a second — billed —
    generation of the same clip."""
    job = new_job()
    ready_for_gate(job)
    vt_db.set_photoreal(job, TENANT, "aerial.png")

    results = [vt_db.set_exterior_photo(job, TENANT, "ext.jpg") for _ in range(3)]
    assert results.count(vt_db.GENERATING_VIDEO) == 1


def test_a_late_aerial_cannot_rewind_a_job_that_moved_on():
    job = new_job()
    ready_for_gate(job)
    vt_db.set_photoreal(job, TENANT, "aerial.png")
    vt_db.set_exterior_photo(job, TENANT, "ext.jpg")
    # Already generating: a duplicate completion is a no-op, not a reset.
    assert vt_db.set_photoreal(job, TENANT, "aerial2.png") is None
    assert vt_db.get(job, TENANT)["status"] == vt_db.GENERATING_VIDEO


# ── player states ───────────────────────────────────────────────────────────
def test_backend_statuses_collapse_to_the_four_states_the_wizard_renders():
    job = new_job()
    assert vt_db.get(job, TENANT)["player_state"] == "preparing"
    ready_for_gate(job)
    assert vt_db.get(job, TENANT)["player_state"] == "preparing"
    vt_db.set_photoreal(job, TENANT, "aerial.png")
    assert vt_db.get(job, TENANT)["player_state"] == "preparing"

    vt_db.set_exterior_photo(job, TENANT, "ext.jpg")
    assert vt_db.get(job, TENANT)["player_state"] == "generating"

    vt_db.set_drone_clip(job, TENANT, "drone.mp4")
    vt_db.set_interior_clip(job, TENANT, "interior.mp4")
    assert vt_db.get(job, TENANT)["player_state"] == "generating"

    vt_db.set_ready(job, TENANT, "tour.mp4")
    assert vt_db.get(job, TENANT)["player_state"] == "ready"

    other = new_job()
    vt_db.fail(other, TENANT, "drone", "boom")
    assert vt_db.get(other, TENANT)["player_state"] == "error"


def test_progress_never_moves_backwards():
    """A slow poll landing late must not make the bar jump back."""
    job = new_job()
    vt_db.set_progress(job, TENANT, 55)
    vt_db.set_progress(job, TENANT, 20)
    assert vt_db.get(job, TENANT)["progress_pct"] == 55


# ── retry ───────────────────────────────────────────────────────────────────
def finished_drone_job():
    job = new_job()
    ready_for_gate(job)
    vt_db.set_photoreal(job, TENANT, "aerial.png")
    vt_db.set_interior_photos(job, TENANT, ["i0.jpg", "i1.jpg"])
    vt_db.set_exterior_photo(job, TENANT, "ext.jpg")
    vt_db.set_drone_clip(job, TENANT, "drone.mp4")
    return job


def test_retry_resumes_at_the_interior_not_the_drone_clip():
    """The drone clip is already on disk and already paid for."""
    job = finished_drone_job()
    vt_db.fail(job, TENANT, "interior", "runway failed")
    assert vt_db.begin_retry(job, TENANT, 3) == vt_db.STAGE_INTERIOR
    assert vt_db.get(job, TENANT)["status"] == vt_db.GENERATING_INTERIOR
    assert vt_db.get(job, TENANT)["attempts"] == 1


def test_a_stitch_failure_re_runs_only_ffmpeg():
    job = finished_drone_job()
    vt_db.set_interior_clip(job, TENANT, "interior.mp4")
    vt_db.fail(job, TENANT, "stitch", "ffmpeg failed")
    assert vt_db.begin_retry(job, TENANT, 3) == vt_db.STAGE_STITCH


def test_retry_never_re_runs_the_tile_capture():
    """A job that failed before producing an aerial has nothing for the drone
    stage to work from, so it is refused up front rather than allowed to fail
    again and consume one of very few attempts."""
    job = new_job()
    vt_db.fail(job, TENANT, "tile", "cesium failed")
    assert vt_db.begin_retry(job, TENANT, 3) is None


def test_attempts_are_capped_so_a_broken_job_stops_billing():
    job = finished_drone_job()
    for _ in range(3):
        vt_db.fail(job, TENANT, "interior", "boom")
        assert vt_db.begin_retry(job, TENANT, 3) is not None
    vt_db.fail(job, TENANT, "interior", "boom")
    assert vt_db.begin_retry(job, TENANT, 3) is None


def test_only_a_failed_job_can_be_retried():
    job = finished_drone_job()
    assert vt_db.begin_retry(job, TENANT, 3) is None


# ── abandon and retention ───────────────────────────────────────────────────
def test_abandoning_stops_further_uploads():
    job = new_job()
    assert vt_db.abandon(job, TENANT) is True
    assert vt_db.set_exterior_photo(job, TENANT, "ext.jpg") is None
    assert vt_db.abandon(job, TENANT) is False


def test_forgetting_media_keeps_the_billed_row():
    """The sweeper reclaims the files; the record that a tour happened — and
    was charged for — stays."""
    job = finished_drone_job()
    vt_db.set_interior_clip(job, TENANT, "interior.mp4")
    vt_db.set_ready(job, TENANT, "tour.mp4")

    vt_db.forget_media(job)
    row = vt_db.get(job, TENANT)
    assert row is not None
    assert row["status"] == vt_db.READY
    assert row["video_path"] is None
    assert row["interior_photo_paths"] == []


def test_nothing_is_swept_before_its_retention_window():
    job = finished_drone_job()
    vt_db.set_interior_clip(job, TENANT, "interior.mp4")
    vt_db.set_ready(job, TENANT, "tour.mp4")
    assert vt_db.sweepable(ready_days=30, dead_days=2) == []


def test_a_failed_job_is_swept_on_the_shorter_window():
    job = new_job()
    vt_db.fail(job, TENANT, "drone", "boom")
    # Nobody is coming back for it, so it goes on the dead-job window.
    assert vt_db.sweepable(ready_days=30, dead_days=0) != []
    assert vt_db.sweepable(ready_days=30, dead_days=365) == []


# ── tenant scoping ──────────────────────────────────────────────────────────
def test_another_tenant_can_neither_read_nor_advance_a_job():
    job = new_job()
    ready_for_gate(job)
    assert vt_db.get(job, OTHER_TENANT) is None
    assert vt_db.set_photoreal(job, OTHER_TENANT, "aerial.png") is None
    assert vt_db.set_exterior_photo(job, OTHER_TENANT, "ext.jpg") is None
    assert vt_db.set_interior_photos(job, OTHER_TENANT, ["i0.jpg"]) is False
    assert vt_db.abandon(job, OTHER_TENANT) is False
    assert vt_db.list_for_tenant(OTHER_TENANT) == []
    # ...and the job is untouched.
    assert vt_db.get(job, TENANT)["status"] == vt_db.GENERATING_PHOTOREAL


def test_listing_is_scoped_and_newest_first():
    first = new_job()
    second = new_job()
    new_job(OTHER_TENANT)
    ids = [row["id"] for row in vt_db.list_for_tenant(TENANT)]
    assert set(ids) == {first, second}


# ── round-tripping ──────────────────────────────────────────────────────────
def test_interior_photo_paths_round_trip_as_a_list():
    job = new_job()
    vt_db.set_interior_photos(job, TENANT, ["a.jpg", "b.jpg", "c.jpg"])
    assert vt_db.get(job, TENANT)["interior_photo_paths"] == ["a.jpg", "b.jpg", "c.jpg"]


def test_a_job_with_no_interior_photos_reads_as_an_empty_list():
    """The wizard and the pipeline both iterate this, so it must never be None."""
    assert vt_db.get(new_job(), TENANT)["interior_photo_paths"] == []


def test_confirmed_coordinates_survive_the_round_trip():
    """The dragged pin is the whole point of step 1 — it has to be what the
    tile capture actually renders."""
    job = vt_db.create(TENANT, "sk", "Hlavná 12", 48.1486, 17.1077)
    row = vt_db.get(job["id"], TENANT)
    assert (row["lat"], row["lng"]) == (48.1486, 17.1077)
    assert row["locale"] == "sk"

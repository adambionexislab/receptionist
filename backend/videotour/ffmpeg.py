"""ffmpeg work: downscaling stills for upload, and stitching the final tour.

ffmpeg earns its place twice over, which is why no image library was added
alongside it. Runway caps a data URI at 5MB and agents shoot on phones, so
every still has to be downscaled before it can be sent — and ffmpeg was already
a hard requirement for the stitch, so it does the resizing too rather than
pulling Pillow in for one call.

THE JUNCTION
------------
Both clips are generated in Seedance reference mode, which pins no frames (see
videotour/runway.py), so the drone clip does not end on the exact pixel content
the interior clip opens with. A hard cut would show that. What the two clips DO
share is the same building from the same angle, seconds apart, so a short
crossfade reads as continuous motion rather than as a transition. That is the
whole reason `_CROSSFADE_SECONDS` exists and why it is short.
"""

import asyncio
import logging
import shutil
from pathlib import Path


logger = logging.getLogger(__name__)

# Long side, in pixels, that stills are downscaled to before becoming data
# URIs. Runway asks for references between 640px and 4K; 1920 lands well inside
# the 5MB data-URI limit as JPEG while keeping room detail the model needs.
_MAX_STILL_EDGE = 1920

# Cross-dissolve at the drone/interior junction.
_CROSSFADE_SECONDS = 0.4

_ENCODE_ARGS = [
    "-c:v", "libx264",
    "-preset", "veryfast",
    "-crf", "20",
    "-pix_fmt", "yuv420p",
    # Lets the dashboard player start the video before the whole file lands.
    "-movflags", "+faststart",
]


class FfmpegError(Exception):
    """Raised when ffmpeg is missing or a command fails."""


def _binary(name: str) -> str:
    """Locate ffmpeg/ffprobe.

    Prefers whatever is on PATH (the Docker image installs it). Falls back to
    the static build imageio-ffmpeg bundles, so the pipeline still works on a
    plain Python runtime where apt is not available.
    """
    found = shutil.which(name)
    if found:
        return found
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    raise FfmpegError(
        "%s not found. Install it in the image, or add imageio-ffmpeg." % name
    )


async def _run(binary: str, args: list[str]) -> bytes:
    """Run one command, returning stdout. Raises with stderr on a bad exit."""
    proc = await asyncio.create_subprocess_exec(
        binary, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace")[-800:]
        logger.error("%s failed (%s): %s", binary, proc.returncode, detail)
        raise FfmpegError("%s failed: %s" % (Path(binary).name, detail))
    return stdout


async def downscale_still(source: Path, target: Path) -> Path:
    """Re-encode one still as a JPEG no larger than _MAX_STILL_EDGE on its long
    side, so it fits Runway's 5MB data-URI limit.

    scale=-2 keeps the aspect ratio and forces an even dimension. The
    conditional expressions leave an already-small image alone rather than
    upscaling it into blur.
    """
    await _run(_binary("ffmpeg"), [
        "-y", "-loglevel", "error",
        "-i", str(source),
        # Named w=/h= rather than positional, so the quoting around the two
        # expressions cannot be misread. The single quotes are for ffmpeg's own
        # filter parser, not a shell — these args go straight to execve.
        "-vf",
        "scale=w='if(gt(iw,ih),min(%d,iw),-2)':h='if(gt(iw,ih),-2,min(%d,ih))'"
        % (_MAX_STILL_EDGE, _MAX_STILL_EDGE),
        "-q:v", "4",
        str(target),
    ])
    return target


async def duration_seconds(video: Path) -> float:
    """Length of a video, via ffprobe. Needed to place the crossfade."""
    out = await _run(_binary("ffprobe"), [
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video),
    ])
    try:
        return float(out.decode().strip())
    except ValueError as exc:
        raise FfmpegError("Could not read the duration of %s" % video.name) from exc


async def stitch(drone: Path, interior: Path, target: Path) -> Path:
    """Join the drone fly-in to the interior tour with a short cross-dissolve.

    Falls back to a plain concatenation if the drone clip is somehow shorter
    than the crossfade — a degraded cut is a better outcome than failing a job
    whose two expensive generations already succeeded.
    """
    drone_length = await duration_seconds(drone)
    if drone_length <= _CROSSFADE_SECONDS:
        logger.warning(
            "Drone clip is only %.2fs — concatenating without a crossfade", drone_length,
        )
        return await _concat(drone, interior, target)

    offset = max(0.0, drone_length - _CROSSFADE_SECONDS)
    await _run(_binary("ffmpeg"), [
        "-y", "-loglevel", "error",
        "-i", str(drone),
        "-i", str(interior),
        "-filter_complex",
        "[0:v][1:v]xfade=transition=fade:duration=%s:offset=%.3f,format=yuv420p[v]"
        % (_CROSSFADE_SECONDS, offset),
        "-map", "[v]",
        *_ENCODE_ARGS,
        str(target),
    ])
    logger.info("Stitched tour written to %s", target.name)
    return target


async def _concat(drone: Path, interior: Path, target: Path) -> Path:
    """Hard-cut fallback. Re-encodes rather than stream-copying, because the two
    clips come back from separate generations and are not guaranteed to share
    an identical stream configuration."""
    await _run(_binary("ffmpeg"), [
        "-y", "-loglevel", "error",
        "-i", str(drone),
        "-i", str(interior),
        "-filter_complex",
        "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p[v]",
        "-map", "[v]",
        *_ENCODE_ARGS,
        str(target),
    ])
    return target

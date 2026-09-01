# Docker runtime, required by the AI video tour.
#
# The service ran on Render's native Python runtime until this feature landed.
# Two of the video tour's dependencies cannot be installed there: ffmpeg is a
# system binary and Chromium needs a couple of dozen shared libraries, and the
# native runtime gives you neither apt nor root. Everything else about the
# service is unchanged — same uvicorn command, same /data disk, same env vars.
#
# Pinned to the exact version in backend/.python-version. The native runtime
# read that file directly; Docker does not, so the two are kept in step by hand
# — python:3.11-slim would silently drift to the latest 3.11.x on rebuild.
FROM python:3.11.9-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Put the browser inside the image rather than in a per-user cache, so it
    # is present for the runtime user and survives as part of the layer.
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /app

# ffmpeg does double duty: it stitches the two generated clips, and it
# downscales every uploaded photo under Runway's 5MB data-URI limit. Installing
# it here is what keeps an image library out of requirements.txt.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --with-deps pulls Chromium's shared libraries (libnss3, libgbm1, libatk-*,
# and the rest) via apt. This is the step that has no equivalent on the native
# runtime, and the reason this Dockerfile exists.
RUN playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY . .

# Render provides $PORT. Shell form so it is expanded at runtime.
CMD uvicorn main:app --host 0.0.0.0 --port $PORT

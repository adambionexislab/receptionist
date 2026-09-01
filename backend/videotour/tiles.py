"""Photorealistic 3D Tiles capture, via headless Chromium.

Google publishes Photorealistic 3D Tiles as a streaming tileset, not as a
rendered-image endpoint — there is no "give me a picture of this place from
this angle" HTTP call. Something has to run a WebGL renderer, so this module
drives CesiumJS inside headless Chromium and screenshots one frame.

This is the heaviest thing the service does, and it runs in the same process
that answers live phone calls, so a few decisions here are about not disturbing
that:

  * Chromium is launched per capture and closed immediately. A resident browser
    would hold ~200MB permanently for a feature used a handful of times a day.
  * WebGL runs on SwiftShader, in software. There is no GPU on a Render
    instance, and without these flags Chromium silently falls back to a
    context that cannot draw the tileset at all.
  * Everything is bounded by a timeout. A tileset that never finishes streaming
    must fail the job, not pin the CPU indefinitely.

COVERAGE
--------
3D Tiles coverage is excellent in cities and patchy in the smaller Italian and
Slovak towns these agencies often sell in. A capture can legitimately come back
as flat, low-detail terrain. That is not treated as an error: the gpt-image-2
pass downstream (videotour/photoreal.py) exists precisely to make a weak tile
render usable, and Seedance only ever uses the result as a reference for the
zone, never as a pinned frame.
"""

import asyncio
import logging

from config import settings

logger = logging.getLogger(__name__)

# Chromium flags that make WebGL work without a GPU. --enable-unsafe-swiftshader
# is required on current Chromium: software WebGL is otherwise refused outright.
_CHROMIUM_ARGS = [
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    "--enable-webgl",
    "--ignore-gpu-blocklist",
    # /dev/shm is small in containers and Chromium will crash without this.
    "--disable-dev-shm-usage",
    "--no-sandbox",
]

_VIEWPORT = {"width": 1280, "height": 720}

# Software rendering plus tile streaming is slow. Generous, but bounded.
_LOAD_TIMEOUT_MS = 120_000
_NAV_TIMEOUT_MS = 60_000

# Camera placement for the establishing shot: high enough to read as an aerial
# of the neighbourhood, oblique enough to show building massing rather than a
# flat top-down. The drone prompt describes descending from roughly this view.
_CAMERA_HEIGHT_M = 420
_CAMERA_PITCH_DEG = -35
_CAMERA_BACK_OFF_M = 380

_PAGE_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<link rel="stylesheet" href="https://cesium.com/downloads/cesiumjs/releases/1.118/Build/Cesium/Widgets/widgets.css">
<script src="https://cesium.com/downloads/cesiumjs/releases/1.118/Build/Cesium/Cesium.js"></script>
<style>
  html, body, #c { margin:0; padding:0; width:100%; height:100%; overflow:hidden; background:#000; }
  .cesium-widget-credits, .cesium-viewer-bottom { display:none !important; }
</style>
</head>
<body>
<div id="c"></div>
<script>
window.captureReady = false;
window.captureError = null;

(async function () {
  try {
    Cesium.GoogleMaps.defaultApiKey = "__TILES_KEY__";

    // globe:false keeps Cesium from loading its own terrain and imagery, which
    // would need an ion token and would draw underneath the Google tileset.
    const viewer = new Cesium.Viewer("c", {
      globe: false,
      baseLayerPicker: false,
      imageryProvider: false,
      geocoder: false, homeButton: false, sceneModePicker: false,
      navigationHelpButton: false, animation: false, timeline: false,
      fullscreenButton: false, infoBox: false, selectionIndicator: false,
      requestRenderMode: false
    });
    viewer.scene.skyAtmosphere.show = true;

    const tileset = await Cesium.createGooglePhotorealisticTileset();
    viewer.scene.primitives.add(tileset);

    // Stand off from the target and look back down at it, so the building sits
    // in frame rather than directly beneath the camera.
    const pitch = Cesium.Math.toRadians(__PITCH__);
    const target = Cesium.Cartesian3.fromDegrees(__LNG__, __LAT__, 0);
    const offset = new Cesium.HeadingPitchRange(
      Cesium.Math.toRadians(0), pitch, __RANGE__
    );
    viewer.camera.lookAt(target, offset);
    viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);

    // tilesLoaded flips true the moment nothing is queued, which can happen
    // before anything meaningful has streamed in. Require it to hold across
    // several consecutive frames instead.
    let settled = 0;
    viewer.scene.postRender.addEventListener(function () {
      if (tileset.tilesLoaded) {
        settled += 1;
        if (settled > 45) { window.captureReady = true; }
      } else {
        settled = 0;
      }
    });
  } catch (err) {
    window.captureError = String((err && err.message) || err);
  }
})();
</script>
</body>
</html>
"""


class TileRenderError(Exception):
    """Raised when the capture cannot be produced."""


def _build_page() -> str:
    if not settings.GOOGLE_MAP_TILES_API_KEY:
        raise TileRenderError("GOOGLE_MAP_TILES_API_KEY not configured")
    return _PAGE_TEMPLATE.replace("__TILES_KEY__", settings.GOOGLE_MAP_TILES_API_KEY)


async def capture(lat: float, lng: float) -> bytes:
    """Render one oblique aerial still at the confirmed coordinates.

    Returns PNG bytes. Raises TileRenderError on any failure — the caller fails
    the job rather than proceeding, since everything downstream needs this
    image as the establishing reference.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise TileRenderError(
            "Playwright is not installed — the tile capture cannot run"
        ) from exc

    page_html = (
        _build_page()
        .replace("__LAT__", repr(float(lat)))
        .replace("__LNG__", repr(float(lng)))
        .replace("__PITCH__", repr(float(_CAMERA_PITCH_DEG)))
        .replace("__RANGE__", repr(float(_CAMERA_BACK_OFF_M)))
    )

    logger.info("Tile capture starting for %s, %s", lat, lng)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
            try:
                page = await browser.new_page(viewport=_VIEWPORT)
                page.set_default_timeout(_NAV_TIMEOUT_MS)
                await page.set_content(page_html, wait_until="load")

                # Surface a Cesium/tileset error as itself, rather than letting
                # it time out and be reported as slowness.
                await page.wait_for_function(
                    "window.captureReady === true || window.captureError !== null",
                    timeout=_LOAD_TIMEOUT_MS,
                )
                error = await page.evaluate("window.captureError")
                if error:
                    raise TileRenderError("Cesium failed: %s" % error)

                png = await page.screenshot(type="png")
            finally:
                await browser.close()
    except TileRenderError:
        raise
    except asyncio.TimeoutError as exc:
        raise TileRenderError("Tile capture timed out") from exc
    except Exception as exc:
        logger.error("Tile capture failed: %s", exc)
        raise TileRenderError("Tile capture failed: %s" % exc) from exc

    logger.info("Tile capture done (%.1f KB)", len(png) / 1024)
    return png

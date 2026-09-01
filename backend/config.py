from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings

_ENV_FILE = Path(__file__).parent.parent / ".env"

# Default reasoning model for every non-realtime text task (post-call lead
# summary, acquisizione transcript extraction). One constant so a model bump
# lands on all of them at once and no task is left behind on an old model;
# each still has its own env var when one task needs to diverge.
_TEXT_MODEL_DEFAULT = "gpt-5.6-terra"


class Settings(BaseSettings):
    # Apify / Immobiliare.it listings sync
    APIFY_TOKEN: Optional[str] = None
    IMMOBILIARE_SEARCH_URL: Optional[str] = None

    # GitHub / listings sync
    GITHUB_TOKEN: Optional[str] = None
    GITHUB_REPO: str = ""
    GITHUB_CSV_PATH: str = "backend/data/listings.csv"
    GITHUB_BRANCH: str = "main"
    LISTINGS_SYNC_INTERVAL_SECONDS: int = 900

    # OpenAI
    OPENAI_API_KEY: Optional[str] = None
    # Signing secret (whsec_…) of the OpenAI project webhook that delivers
    # `realtime.call.incoming` events to POST /call/incoming. When set, the
    # handler verifies the Standard-Webhooks signature and rejects forgeries.
    OPENAI_WEBHOOK_SECRET: Optional[str] = None

    # Twilio — numbers only. Inbound calls no longer hit a Twilio voice webhook:
    # each number's SIP trunk routes the call to OpenAI's SIP connector, which
    # notifies us via the OpenAI webhook above. ACCOUNT_SID/AUTH_TOKEN are used
    # only by the (now dormant) auto-provisioner; the live call path needs none.
    TWILIO_ACCOUNT_SID: Optional[str] = None
    TWILIO_AUTH_TOKEN: Optional[str] = None
    TWILIO_PHONE_NUMBER: Optional[str] = None
    # Optional second demo number for the Slovak-language demo tenant. When set,
    # a locale='sk' "Štúdio Demo" tenant (Slovak seed listings, Slovak agent) is
    # created/bound to it at startup, exactly as TWILIO_PHONE_NUMBER drives the
    # Italian demo tenant. Point an existing Twilio number's voice webhook here.
    TWILIO_PHONE_NUMBER_SK: Optional[str] = None
    # ISO country code to provision tenant numbers from. "AT" (Austria) is the
    # default: cheap to host, and on the intra-EU forwarding leg from an Italian
    # carrier it's price-capped/usually plan-included. "US" is cheapest to host
    # but bills tenants uncapped international on every forwarded call; "IT" is
    # free-forwarding for tenants but ~40x the hosting cost.
    TWILIO_NUMBER_COUNTRY: str = "AT"

    # Deployment
    PUBLIC_BASE_URL: str = "http://localhost:8000"

    # Multi-tenant
    ADMIN_TOKEN: Optional[str] = None
    DATA_DIR: str = "/data"
    # HMAC key used to sign the agency dashboard session cookie. Falls back to
    # ADMIN_TOKEN (already set in prod) so sessions still sign if this is unset;
    # a random ephemeral key is used only as a last resort (logs everyone out on
    # restart). Set a stable value in production.
    SESSION_SECRET: Optional[str] = None

    # Lead capture (sent via Resend's HTTP API — Render blocks outbound SMTP)
    LEAD_EMAIL: Optional[str] = None
    RESEND_API_KEY: Optional[str] = None
    RESEND_FROM: str = "onboarding@resend.dev"
    # Signing secret (whsec_…) of the Resend "email.received" webhook. When set,
    # POST /leads/inbound-email verifies the Svix signature and rejects forgeries.
    RESEND_WEBHOOK_SECRET: Optional[str] = None

    # Lead-generation / cold outreach (ApollonIA agency prospecting)
    # Shared staff password for the internal lead-gen dashboard at /leadgen
    # (campaigns, leads, meeting notes). Unset means nobody can log in — the
    # dashboard fails closed rather than open, so this must be set in prod.
    LEADGEN_PASSWORD: Optional[str] = None
    GOOGLE_PLACES_API_KEY: Optional[str] = None
    # Sender for outreach emails; falls back to RESEND_FROM when unset.
    OUTREACH_FROM_EMAIL: Optional[str] = None
    # Reply-To for outreach emails. When set, lead replies go here (e.g. your real
    # info@apollon-ia.com inbox) instead of the From address.
    OUTREACH_REPLY_TO: Optional[str] = None
    CALENDLY_LINK: Optional[str] = None

    # Stripe billing (Checkout)
    STRIPE_SECRET_KEY: Optional[str] = None
    STRIPE_WEBHOOK_SECRET: Optional[str] = None

    # Text (non-realtime) reasoning models — see _TEXT_MODEL_DEFAULT above.
    # Post-call one-sentence lead summary prepended to the lead email.
    SUMMARY_MODEL: str = _TEXT_MODEL_DEFAULT
    # Transcript → structured listing fields/notes.
    EXTRACTION_MODEL: str = _TEXT_MODEL_DEFAULT

    # Acquisizione (seller-meeting capture) — ships dark until this is set.
    ACQUISIZIONE_ENABLED: bool = False
    # Live streaming transcription model for the browser WebRTC meeting capture.
    REALTIME_TRANSCRIBE_MODEL: str = "gpt-realtime-whisper"
    # Latency/accuracy tradeoff for that model: minimal|low|medium|high|xhigh.
    # A seller meeting is transcribed for later extraction, not read live word
    # by word, so we buy the model the most audio context it will take.
    REALTIME_TRANSCRIBE_DELAY: str = "xhigh"
    # Property photo enhancement (declutter/relight/straighten).
    IMAGE_EDIT_MODEL: str = "gpt-image-2"

    # ── AI video tour (drone fly-in + interior room tour) ────────────────────
    # Ships dark until this is set, exactly like ACQUISIZIONE_ENABLED.
    VIDEO_TOUR_ENABLED: bool = False
    # Google Maps Platform. The geocoding key is server-side and must stay
    # secret; MAPS_BROWSER_KEY is embedded in the dashboard page for the
    # draggable-marker map, so it must be a SEPARATE, HTTP-referrer-restricted
    # key. Never reuse GOOGLE_PLACES_API_KEY here — that one is server-side.
    GOOGLE_GEOCODING_API_KEY: Optional[str] = None
    GOOGLE_MAPS_BROWSER_KEY: Optional[str] = None
    # Cloud-configured Map ID. Google's AdvancedMarkerElement requires one; when
    # this is unset the wizard falls back to the legacy draggable Marker, which
    # needs no Map ID. Set it to move off the deprecated marker API.
    GOOGLE_MAPS_MAP_ID: Optional[str] = None
    # Photorealistic 3D Tiles, streamed into headless Chromium by CesiumJS.
    GOOGLE_MAP_TILES_API_KEY: Optional[str] = None
    # Runway. Seedance 2.5 is the model for both clips — not for last-frame
    # keyframes (we pin no frames), but because it is the only family in
    # Runway's API with multi-image REFERENCE mode, which is what lets one
    # generation cover several rooms. gen4.5/gen4_turbo are first-frame only.
    RUNWAY_API_KEY: Optional[str] = None
    RUNWAY_API_VERSION: str = "2024-11-06"
    RUNWAY_VIDEO_MODEL: str = "seedance2_5"
    # 854:480 | 1280:720 | 1920:1080. Drives cost directly: at 20/30/68 credits
    # per second of output, a 30-second interior tour is $6.00 / $9.00 / $20.40.
    RUNWAY_VIDEO_RATIO: str = "1280:720"
    # Seconds for the drone fly-in. Seedance accepts 4-30 and bills per second.
    VIDEO_TOUR_DRONE_SECONDS: int = 6
    # Roughly how long each room gets in the single interior generation. The
    # total is clamped to Seedance's 30-second ceiling, which doubles as the
    # per-tour cost cap: 30s is 900 credits at 720p no matter how many rooms.
    VIDEO_TOUR_SECONDS_PER_ROOM: int = 4
    # Hard cap on interior photos. Seedance takes up to 30 reference images, so
    # this is a cost decision, not a model limit — every room added is more
    # generated seconds. Enforced server-side as well as in the wizard.
    VIDEO_TOUR_MAX_INTERIOR_PHOTOS: int = 8
    # Retries spend real Runway credits, so a persistently failing job has to
    # stop rather than bill the agency in a loop.
    VIDEO_TOUR_MAX_ATTEMPTS: int = 3
    # Retention. The disk is shared with the SQLite file every call writes to,
    # so these are what stop a full disk from becoming an outage.
    VIDEO_TOUR_KEEP_READY_DAYS: int = 30
    VIDEO_TOUR_KEEP_DEAD_DAYS: int = 2
    VIDEO_TOUR_SWEEP_INTERVAL_SECONDS: int = 6 * 3600

    model_config = {"env_file": str(_ENV_FILE)}


settings = Settings()

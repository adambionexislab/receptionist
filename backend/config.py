from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings

_ENV_FILE = Path(__file__).parent.parent / ".env"


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

    # Acquisizione (seller-meeting capture) — ships dark until this is set.
    ACQUISIZIONE_ENABLED: bool = False
    # Live streaming transcription model for the browser WebRTC meeting capture.
    REALTIME_TRANSCRIBE_MODEL: str = "gpt-realtime-whisper"
    # Transcript → structured listing fields/tasks (reasoning model).
    EXTRACTION_MODEL: str = "gpt-5.6-terra"
    # Property photo enhancement (declutter/relight/straighten).
    IMAGE_EDIT_MODEL: str = "gpt-image-2"

    model_config = {"env_file": str(_ENV_FILE)}


settings = Settings()

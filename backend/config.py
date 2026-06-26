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

    # Twilio
    TWILIO_ACCOUNT_SID: Optional[str] = None
    TWILIO_AUTH_TOKEN: Optional[str] = None
    TWILIO_PHONE_NUMBER: Optional[str] = None

    # Deployment
    PUBLIC_BASE_URL: str = "http://localhost:8000"

    # Multi-tenant
    ADMIN_TOKEN: Optional[str] = None
    DATA_DIR: str = "/data"

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
    CALENDLY_LINK: Optional[str] = None

    # Stripe billing (Checkout)
    STRIPE_SECRET_KEY: Optional[str] = None
    STRIPE_WEBHOOK_SECRET: Optional[str] = None

    model_config = {"env_file": str(_ENV_FILE)}


settings = Settings()

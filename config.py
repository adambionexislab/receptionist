from typing import Optional
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Apify / Immobiliare.it listings sync
    APIFY_TOKEN: Optional[str] = None
    IMMOBILIARE_SEARCH_URL: Optional[str] = None

    # GitHub / listings sync
    GITHUB_TOKEN: Optional[str] = None
    GITHUB_REPO: str = ""
    GITHUB_CSV_PATH: str = "data/listings.csv"
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

    # Lead capture (sent via Resend's HTTP API — Render blocks outbound SMTP)
    LEAD_EMAIL: Optional[str] = None
    RESEND_API_KEY: Optional[str] = None
    RESEND_FROM: str = "onboarding@resend.dev"

    model_config = {"env_file": ".env"}


settings = Settings()

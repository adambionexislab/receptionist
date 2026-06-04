from typing import Optional
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # GitHub / listings sync
    GITHUB_TOKEN: Optional[str] = None
    GITHUB_REPO: str = ""
    GITHUB_CSV_PATH: str = "listings.csv"
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

    # Lead capture
    LEAD_EMAIL: Optional[str] = None
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: Optional[int] = None
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None

    model_config = {"env_file": ".env"}


settings = Settings()

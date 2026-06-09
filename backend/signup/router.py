import asyncio
import csv
import datetime
import logging
import threading
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

_DATA_DIR    = Path(__file__).parent.parent / "data"
_SIGNUPS_CSV = _DATA_DIR / "signups.csv"
_ROME        = ZoneInfo("Europe/Rome")
_csv_lock    = threading.Lock()

_PLAN_PRICES = {"Base": "€145/mese", "Pro": "€395/mese", "Max": "€795/mese"}


class SignupData(BaseModel):
    studio_name: str
    immobiliare_url: str
    phone: str
    email: str
    plan: Literal["Base", "Pro", "Max"]
    pagamento: Literal["Mensile", "Annuale"]
    modalita: Literal["Tutte le chiamate", "Solo chiamate perse/cancellate"]

    @field_validator("studio_name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Il nome dello studio non può essere vuoto")
        return v

    @field_validator("immobiliare_url")
    @classmethod
    def url_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("L'URL non può essere vuoto")
        if not v.startswith(("http://", "https://")):
            v = "https://" + v
        return v

    @field_validator("phone")
    @classmethod
    def phone_valid(cls, v: str) -> str:
        v = v.strip()
        digits = "".join(c for c in v if c.isdigit())
        if len(digits) < 6:
            raise ValueError("Numero di telefono non valido")
        return v

    @field_validator("email")
    @classmethod
    def email_valid(cls, v: str) -> str:
        v = v.strip()
        if not v or "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Indirizzo email non valido")
        return v


def _write_csv(data: SignupData) -> None:
    _DATA_DIR.mkdir(exist_ok=True)
    first = not _SIGNUPS_CSV.exists()
    with _csv_lock:
        with open(_SIGNUPS_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if first:
                w.writerow(["timestamp", "studio_name", "immobiliare_url", "phone", "email", "plan", "pagamento", "modalita"])
            w.writerow([
                datetime.datetime.now(_ROME).isoformat(),
                data.studio_name,
                data.immobiliare_url,
                data.phone,
                data.email,
                data.plan,
                data.pagamento,
                data.modalita,
            ])


async def _send_notification(data: SignupData) -> None:
    if not settings.RESEND_API_KEY or not settings.LEAD_EMAIL:
        logger.warning("RESEND_API_KEY or LEAD_EMAIL not configured — signup email skipped")
        return

    price = _PLAN_PRICES.get(data.plan, "")
    body = "\n".join([
        f"Nuova richiesta di attivazione ApollonIA\n",
        f"Piano:              {data.plan} ({price})",
        f"Pagamento:          {data.pagamento}",
        f"Studio:             {data.studio_name}",
        f"Email:              {data.email}",
        f"Telefono:           {data.phone}",
        f"URL immobiliare.it: {data.immobiliare_url}",
        f"Modalità:           {data.modalita}",
        f"\nTimestamp: {datetime.datetime.now(_ROME).strftime('%d/%m/%Y %H:%M')} (Rome)",
    ])

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
                json={
                    "from": settings.RESEND_FROM,
                    "to": [settings.LEAD_EMAIL],
                    "subject": f"[ApollonIA] Nuovo signup {data.plan} — {data.studio_name}",
                    "text": body,
                },
            )
            resp.raise_for_status()
        logger.info("Signup notification sent for studio=%s plan=%s", data.studio_name, data.plan)
    except Exception as exc:
        logger.error("Failed to send signup notification: %s", exc)


@router.post("/signup")
async def create_signup(data: SignupData):
    await asyncio.to_thread(_write_csv, data)
    asyncio.create_task(_send_notification(data))
    return {"status": "ok"}

"""Meta WhatsApp Cloud API webhook — delivery statuses for the lead alerts.

Sending a template only gets a 200 meaning "accepted"; whether it reached the
phone (sent → delivered → read) or FAILED, and why, Meta reports only here.
This endpoint's whole job is to put that in the logs, keyed by the wamid that
services/whatsapp.py logs on send, so a missing alert can be explained.

Setup (Meta app → WhatsApp → Configuration → Webhook):
  Callback URL  https://<api>/whatsapp/webhook
  Verify token  the value of WHATSAPP_VERIFY_TOKEN
  then subscribe the "messages" field (it carries the status events).
"""

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/whatsapp/webhook")
async def verify(request: Request):
    """Meta's one-time subscription handshake: echo hub.challenge back when
    the verify token matches."""
    params = request.query_params
    token = params.get("hub.verify_token") or ""
    if (
        params.get("hub.mode") == "subscribe"
        and settings.WHATSAPP_VERIFY_TOKEN
        and hmac.compare_digest(token, settings.WHATSAPP_VERIFY_TOKEN)
    ):
        return PlainTextResponse(params.get("hub.challenge") or "")
    raise HTTPException(status_code=403, detail="Verification failed")


def _signature_ok(body: bytes, header: str) -> bool:
    if not settings.WHATSAPP_APP_SECRET or not header.startswith("sha256="):
        return False
    expected = hmac.new(
        settings.WHATSAPP_APP_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256="), expected)


def log_statuses(payload: dict) -> None:
    """Log every status event in a webhook payload; failures at ERROR, with
    Meta's error code and explanation."""
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            for status in (change.get("value") or {}).get("statuses") or []:
                state = status.get("status")
                wamid = status.get("id")
                to = status.get("recipient_id")
                if state == "failed":
                    for error in status.get("errors") or [{}]:
                        logger.error(
                            "WhatsApp alert FAILED: to=%s wamid=%s code=%s %s — %s",
                            to, wamid, error.get("code"), error.get("title"),
                            (error.get("error_data") or {}).get("details")
                            or error.get("message"),
                        )
                else:
                    logger.info(
                        "WhatsApp alert %s: to=%s wamid=%s", state, to, wamid
                    )


@router.post("/whatsapp/webhook")
async def receive(request: Request):
    body = await request.body()
    if not _signature_ok(body, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(status_code=401, detail="Bad signature")
    try:
        log_statuses(json.loads(body))
    except Exception:
        # Always 200 on a signed payload: a non-2xx makes Meta retry the same
        # event for days, and a logging slip is not worth that.
        logger.exception("Could not read WhatsApp webhook payload")
    return {"ok": True}

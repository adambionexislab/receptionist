"""REST API for the ApollonIA lead-generation pipeline.

Campaign CRUD + the background pipeline (scrape → email) that the static
dashboard drives, plus manual response logging and a stub inbound-email
webhook for future Resend integration.

The scrape + send work runs in a FastAPI BackgroundTask. Both stages are
SYNCHRONOUS functions, so Starlette executes them in a worker thread and the
HTTP response returns immediately.
"""

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Literal, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

from config import settings
from leadgen import db
from services.outreach import (
    DEFAULT_BODY,
    DEFAULT_SUBJECT,
    PLACEHOLDERS,
    get_template,
    save_template,
    send_outreach_emails,
    send_test_email,
)
from services.places_scraper import (
    DEFAULT_EXCLUSIONS,
    get_exclusions_raw,
    save_exclusions,
    scrape_city,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── request models ───────────────────────────────────────────────────────────
class CampaignCreate(BaseModel):
    city: str = Field(..., min_length=1)
    max_results: int = Field(60, ge=1, le=60)


class StatusUpdate(BaseModel):
    status: Literal["running", "paused"]


class LeadResponse(BaseModel):
    lead_id: int
    response_type: Literal["interested", "not_interested", "booked"]
    notes: Optional[str] = None


class TemplateUpdate(BaseModel):
    subject: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1)


class TestEmail(BaseModel):
    to: str = Field(..., min_length=3)
    # Optional overrides so the dashboard can test the on-screen (unsaved) draft;
    # falls back to the saved template when omitted.
    subject: Optional[str] = None
    body: Optional[str] = None
    agency_name: Optional[str] = None


class ExclusionsUpdate(BaseModel):
    # Comma-separated agency-name keywords. Empty string clears the blocklist.
    keywords: str = ""


# ── background pipeline ──────────────────────────────────────────────────────
def run_campaign(campaign_id: int) -> None:
    """Scrape the city, then email the leads. Resilient: each stage is guarded
    and a pause flips the campaign out of the pipeline cleanly.

    Idempotent enough to double as a resume: the scraper skips known place_ids
    and outreach only touches still-pending leads.
    """
    camp = db.get_campaign(campaign_id)
    if not camp:
        logger.error("run_campaign: campaign %s not found", campaign_id)
        return

    db.mark_started(campaign_id)
    try:
        scrape_city(camp["city"], campaign_id, camp.get("max_results") or 60)
    except Exception:
        logger.exception("Scrape stage crashed for campaign %s", campaign_id)
        db.log_event(campaign_id, "error", "scrape stage crashed (see logs)")

    camp = db.get_campaign(campaign_id)
    if not camp or camp["status"] == "paused":
        return  # paused mid-scrape: don't start emailing

    try:
        send_outreach_emails(campaign_id)
    except Exception:
        logger.exception("Outreach stage crashed for campaign %s", campaign_id)
        db.log_event(campaign_id, "error", "outreach stage crashed (see logs)")

    camp = db.get_campaign(campaign_id)
    if camp and camp["status"] != "paused":
        db.mark_completed(campaign_id)


# ── campaign endpoints ───────────────────────────────────────────────────────
@router.post("/campaigns")
def create_campaign(data: CampaignCreate):
    campaign = db.create_campaign(data.city.strip(), data.max_results)
    db.log_event(campaign["id"], "campaign_created", f'city="{campaign["city"]}"')
    return campaign


@router.post("/campaigns/{campaign_id}/start")
def start_campaign(campaign_id: int, background_tasks: BackgroundTasks):
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign["status"] == "running":
        raise HTTPException(status_code=409, detail="Campaign already running")

    db.set_status(campaign_id, "running")
    db.log_event(campaign_id, "campaign_started", "background pipeline queued")
    background_tasks.add_task(run_campaign, campaign_id)
    return {"status": "running", "campaign_id": campaign_id}


@router.get("/campaigns")
def list_campaigns():
    return db.list_campaigns()


@router.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: int):
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


@router.get("/campaigns/{campaign_id}/leads")
def get_campaign_leads(
    campaign_id: int,
    page: int = 1,
    limit: int = 50,
    status: Optional[str] = None,
):
    if not db.get_campaign(campaign_id):
        raise HTTPException(status_code=404, detail="Campaign not found")
    return db.get_leads(campaign_id, page=page, limit=limit, status=status)


@router.get("/campaigns/{campaign_id}/logs")
def get_campaign_logs(campaign_id: int, limit: int = 100):
    if not db.get_campaign(campaign_id):
        raise HTTPException(status_code=404, detail="Campaign not found")
    return db.get_logs(campaign_id, limit=limit)


@router.patch("/campaigns/{campaign_id}/status")
def update_campaign_status(
    campaign_id: int, data: StatusUpdate, background_tasks: BackgroundTasks
):
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    db.set_status(campaign_id, data.status)
    db.log_event(campaign_id, "status_changed", data.status)

    # Resuming re-queues the pipeline; the scraper/outreach dedupe so it picks
    # up where it left off rather than redoing finished work.
    if data.status == "running":
        background_tasks.add_task(run_campaign, campaign_id)

    return {"status": data.status, "campaign_id": campaign_id}


# ── lead response endpoints ──────────────────────────────────────────────────
_RESPONSE_TO_COUNTER = {
    "interested": "total_responded",
    "not_interested": "total_responded",
    "booked": "total_booked",
}


@router.post("/leads/response")
def record_response(data: LeadResponse):
    lead = db.get_lead(data.lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    db.set_lead_response(data.lead_id, data.response_type, data.notes)

    counter = _RESPONSE_TO_COUNTER[data.response_type]
    # Only bump a campaign counter on the first transition into that state, so
    # editing a response or re-submitting doesn't inflate the totals.
    if lead["campaign_id"] and lead["response_status"] != data.response_type:
        if data.response_type == "booked":
            db.increment_campaign(lead["campaign_id"], "total_booked")
        elif lead["response_status"] in ("none", "booked"):
            db.increment_campaign(lead["campaign_id"], "total_responded")

    db.log_event(
        lead["campaign_id"], "response_received",
        f"{data.response_type}" + (f": {data.notes}" if data.notes else ""),
        lead_id=data.lead_id,
    )
    return {"status": "ok", "lead_id": data.lead_id, "response_type": data.response_type}


# ── outreach template ────────────────────────────────────────────────────────
@router.get("/outreach/template")
def get_outreach_template():
    subject, body = get_template()
    return {
        "subject": subject,
        "body": body,
        "default_subject": DEFAULT_SUBJECT,
        "default_body": DEFAULT_BODY,
        "placeholders": list(PLACEHOLDERS),
    }


@router.put("/outreach/template")
def update_outreach_template(data: TemplateUpdate):
    save_template(data.subject, data.body)
    db.log_event(None, "template_updated", "outreach email template saved")
    return {"status": "ok", "subject": data.subject, "body": data.body}


@router.post("/outreach/test")
def send_outreach_test(data: TestEmail):
    to = data.to.strip()
    if "@" not in to or "." not in to.split("@")[-1]:
        raise HTTPException(status_code=422, detail="Indirizzo email non valido")
    try:
        result = send_test_email(to, data.subject, data.body, data.agency_name)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Invio fallito: {exc}")
    return {"status": "ok", **result}


# ── agency-name blocklist ─────────────────────────────────────────────────────
@router.get("/outreach/exclusions")
def get_outreach_exclusions():
    return {"keywords": get_exclusions_raw(), "default_keywords": DEFAULT_EXCLUSIONS}


@router.put("/outreach/exclusions")
def update_outreach_exclusions(data: ExclusionsUpdate):
    save_exclusions(data.keywords)
    db.log_event(None, "exclusions_updated", data.keywords or "(empty)")
    return {"status": "ok", "keywords": data.keywords}


# ── inbound email (Resend "email.received" webhook) ──────────────────────────
def _extract_address(raw_from: str) -> str:
    """Pull a bare address out of a From value that may be 'Studio Rossi
    <info@rossi.it>' or just 'info@rossi.it'. Lower-cased for matching."""
    value = (raw_from or "").strip()
    if "<" in value and ">" in value:
        value = value[value.index("<") + 1 : value.index(">")]
    return value.strip().strip('"').lower()


def _svix_header(headers, name: str) -> str:
    """Resend signs with Svix; accept both the svix-* and newer webhook-* names."""
    return headers.get(f"svix-{name}") or headers.get(f"webhook-{name}") or ""


def _verify_resend_signature(secret: str, headers, raw_body: bytes) -> bool:
    """Svix manual verification: HMAC-SHA256 over '{id}.{timestamp}.{body}',
    base64-compared (constant-time) against the signature header's v1 entries."""
    msg_id = _svix_header(headers, "id")
    timestamp = _svix_header(headers, "timestamp")
    signatures = _svix_header(headers, "signature")
    if not (msg_id and timestamp and signatures):
        return False
    # Drop stale deliveries (>5 min) to blunt replay attacks.
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except ValueError:
        return False
    key = secret.split("_", 1)[1] if secret.startswith("whsec_") else secret
    try:
        key_bytes = base64.b64decode(key)
    except Exception:
        return False
    signed = msg_id.encode() + b"." + timestamp.encode() + b"." + raw_body
    expected = base64.b64encode(
        hmac.new(key_bytes, signed, hashlib.sha256).digest()
    ).decode()
    for entry in signatures.split():
        _, _, sig = entry.partition(",")
        if sig and hmac.compare_digest(sig, expected):
            return True
    return False


async def _fetch_received_email(email_id: str) -> dict:
    """The webhook carries only metadata — pull the actual body via the Resend
    Received-emails API. Best-effort: returns {} on any failure."""
    if not (email_id and settings.RESEND_API_KEY):
        return {}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.resend.com/emails/receiving/{email_id}",
                headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning("Could not fetch received email %s: %s", email_id, exc)
        return {}


@router.post("/leads/inbound-email")
async def inbound_email(request: Request):
    """Resend inbound webhook. On an 'email.received' event we verify the
    signature, match the sender to a cold-outreach lead, fetch the reply body,
    and record it (response_status='replied' + body in notes + a log event) so
    it surfaces in the dashboard. Returns 200 on every accepted delivery so
    Resend doesn't retry; only a bad signature is rejected."""
    raw = await request.body()

    secret = settings.RESEND_WEBHOOK_SECRET
    if secret:
        if not _verify_resend_signature(secret, request.headers, raw):
            logger.warning("Inbound email webhook: signature verification failed")
            raise HTTPException(status_code=401, detail="Invalid signature")
    else:
        logger.warning(
            "RESEND_WEBHOOK_SECRET not set — accepting inbound webhook UNVERIFIED"
        )

    try:
        payload = json.loads(raw)
    except Exception:
        logger.warning("Inbound email webhook: body was not JSON")
        return {"status": "ignored"}

    if payload.get("type") != "email.received":
        logger.info("Inbound webhook ignored (type=%s)", payload.get("type"))
        return {"status": "ignored"}

    data = payload.get("data") or {}
    email_id = data.get("email_id") or data.get("id")
    sender = _extract_address(data.get("from", ""))
    subject = data.get("subject", "")

    received = await _fetch_received_email(email_id)
    body_text = (received.get("text") or "").strip()
    if not body_text and received.get("html"):
        body_text = "(corpo solo HTML — vedi Resend)"
    preview = body_text[:500] if body_text else f"(oggetto: {subject})"

    lead = db.get_lead_by_email(sender) if sender else None
    if lead is None:
        db.log_event(
            None, "inbound_email_unmatched",
            f"da {sender or '?'} · oggetto: {subject}",
        )
        logger.info("Inbound email from %s matched no lead", sender)
        return {"status": "ok", "matched": False}

    # Count the response once, on the first transition out of 'none'. A human can
    # later re-classify 'replied' → interested/not_interested/booked in the UI.
    already_responded = lead["response_status"] not in ("none", None)
    db.set_lead_response(lead["id"], "replied", preview)
    if lead["campaign_id"] and not already_responded:
        db.increment_campaign(lead["campaign_id"], "total_responded")
    db.log_event(
        lead["campaign_id"], "response_received",
        f"Risposta da {sender}: {preview[:200]}",
        lead_id=lead["id"],
    )
    logger.info("Inbound reply matched lead %s (%s)", lead["id"], sender)
    return {"status": "ok", "matched": True, "lead_id": lead["id"]}

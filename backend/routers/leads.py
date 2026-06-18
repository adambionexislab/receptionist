"""REST API for the ApollonIA lead-generation pipeline.

Campaign CRUD + the background pipeline (scrape → email) that the static
dashboard drives, plus manual response logging and a stub inbound-email
webhook for future Resend integration.

The scrape + send work runs in a FastAPI BackgroundTask. Both stages are
SYNCHRONOUS functions, so Starlette executes them in a worker thread and the
HTTP response returns immediately.
"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

from leadgen import db
from services.outreach import (
    DEFAULT_BODY,
    DEFAULT_SUBJECT,
    PLACEHOLDERS,
    get_template,
    save_template,
    send_outreach_emails,
)
from services.places_scraper import scrape_city

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


@router.post("/leads/inbound-email")
async def inbound_email(request: Request):
    """Stub for a future Resend inbound webhook. Logs the payload and 200s so
    Resend treats the delivery as accepted; no parsing/matching yet."""
    try:
        payload = await request.json()
    except Exception:
        payload = {"raw": (await request.body()).decode("utf-8", "replace")[:2000]}

    detail = str(payload)[:1000]
    db.log_event(None, "inbound_email", detail)
    logger.info("Inbound email webhook received: %s", detail)
    return {"status": "ok"}

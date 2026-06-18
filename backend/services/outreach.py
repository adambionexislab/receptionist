"""Cold-outreach email sender for ApollonIA lead generation.

Sends the personalised Italian outreach email to every campaign lead that has
an address and hasn't been contacted yet, via Resend's HTTP API — the same
transport the signup flow uses (Render blocks outbound SMTP).

Synchronous on purpose so it can run inside a FastAPI BackgroundTask without
blocking the event loop during the 1s inter-send throttle.
"""

import logging
import time

import httpx

from config import settings
from leadgen import db

logger = logging.getLogger(__name__)

_RESEND_URL = "https://api.resend.com/emails"
_SEND_DELAY = 1.0  # seconds between sends, to stay under Resend rate limits


def _subject(agency_name: str) -> str:
    name = agency_name or "la vostra agenzia"
    return f"{name} – un'idea per qualificare i lead in entrata"


def _body() -> str:
    return (
        "Buongiorno,\n\n"
        "mi chiamo Adam e volevo presentarvi ApollonIA, un assistente vocale AI "
        "per agenzie immobiliari.\n\n"
        "Apollonia gestisce le chiamate in arrivo, risponde alle domande sugli "
        "immobili e qualifica i contatti, segnalandovi solo chi è davvero "
        "interessato a comprare o vendere.\n\n"
        "Il modo più semplice per capirla è provarla: potete parlare con lei "
        "gratuitamente sul sito 👉 https://apollon-ia.com\n\n"
        "Bastano un paio di minuti. Se poi vi interessa, vi spiego volentieri "
        "come potrebbe funzionare per la vostra agenzia.\n\n"
        "Un saluto,\n"
        "Adam"
    )


def _from_address() -> str:
    return settings.OUTREACH_FROM_EMAIL or settings.RESEND_FROM


def _send_one(client: httpx.Client, to: str, subject: str, body: str) -> None:
    resp = client.post(
        _RESEND_URL,
        headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
        json={"from": _from_address(), "to": [to], "subject": subject, "text": body},
        timeout=15.0,
    )
    resp.raise_for_status()


def send_outreach_emails(campaign_id: int) -> int:
    """Email every pending lead with an address. Returns the number sent.

    Never raises: a failure on one lead is logged and the loop continues.
    """
    if not settings.RESEND_API_KEY:
        db.log_event(campaign_id, "error", "RESEND_API_KEY not configured")
        logger.error("RESEND_API_KEY not set — campaign %s outreach skipped", campaign_id)
        return 0

    leads = db.get_pending_email_leads(campaign_id)
    if not leads:
        db.log_event(campaign_id, "outreach_skipped", "no pending leads with an email")
        return 0

    db.log_event(campaign_id, "outreach_started", f"{len(leads)} leads to email")
    body = _body()
    sent = 0

    with httpx.Client() as client:
        for lead in leads:
            # Respect a pause that arrived mid-run.
            camp = db.get_campaign(campaign_id)
            if not camp or camp["status"] == "paused":
                db.log_event(campaign_id, "outreach_paused", f"stopped after {sent} sent")
                break

            to = lead["email"]
            try:
                _send_one(client, to, _subject(lead["agency_name"]), body)
            except Exception as exc:
                # Leave email_status='pending' so a later resume can retry.
                db.log_event(
                    campaign_id, "error",
                    f"send to {to} failed: {exc}", lead_id=lead["id"],
                )
                logger.warning("Outreach send failed for lead %s (%s): %s", lead["id"], to, exc)
                continue

            db.mark_email_sent(lead["id"])
            db.increment_campaign(campaign_id, "total_emailed")
            db.log_event(campaign_id, "email_sent", to, lead_id=lead["id"])
            sent += 1
            time.sleep(_SEND_DELAY)

    db.log_event(campaign_id, "outreach_finished", f"{sent} emails sent")
    logger.info("Campaign %s outreach done: %d sent", campaign_id, sent)
    return sent

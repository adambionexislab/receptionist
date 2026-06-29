"""Cold-outreach email sender for ApollonIA lead generation.

Sends the personalised Italian outreach email to every campaign lead that has
an address and hasn't been contacted yet, via Resend's HTTP API — the same
transport the signup flow uses (Render blocks outbound SMTP).

Synchronous on purpose so it can run inside a FastAPI BackgroundTask without
blocking the event loop during the 1s inter-send throttle.
"""

import logging
import time
from typing import Optional

import httpx

from config import settings
from leadgen import db

logger = logging.getLogger(__name__)

_RESEND_URL = "https://api.resend.com/emails"
_SEND_DELAY = 1.0  # seconds between sends, to stay under Resend rate limits

# Settings keys for the (editable) template stored in app_settings.
_SUBJECT_KEY = "outreach_subject"
_BODY_KEY = "outreach_body"

# Placeholders the dashboard advertises and that _render substitutes.
PLACEHOLDERS = ("{agency_name}", "{city}", "{calendly_link}")

DEFAULT_SUBJECT = "{agency_name} – un'idea per qualificare i lead in entrata"

DEFAULT_BODY = (
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


def get_template() -> tuple[str, str]:
    """Return the (subject, body) template — the dashboard-saved version if any,
    otherwise the built-in defaults."""
    subject = db.get_setting(_SUBJECT_KEY) or DEFAULT_SUBJECT
    body = db.get_setting(_BODY_KEY) or DEFAULT_BODY
    return subject, body


def save_template(subject: str, body: str) -> None:
    db.set_setting(_SUBJECT_KEY, subject)
    db.set_setting(_BODY_KEY, body)


def _render(template: str, agency_name: str = "", city: str = "") -> str:
    """Safe placeholder substitution (plain .replace, never str.format — the
    template is user-edited and may contain stray braces)."""
    return (
        template
        .replace("{agency_name}", agency_name or "la vostra agenzia")
        .replace("{city}", city or "")
        .replace("{calendly_link}", settings.CALENDLY_LINK or "")
    )


def _from_address() -> str:
    return settings.OUTREACH_FROM_EMAIL or settings.RESEND_FROM


def _send_one(client: httpx.Client, to: str, subject: str, body: str) -> None:
    payload = {"from": _from_address(), "to": [to], "subject": subject, "text": body}
    # Route replies to a real inbox (e.g. info@apollon-ia.com) when configured,
    # so prospects can answer and reach a human directly.
    if settings.OUTREACH_REPLY_TO:
        payload["reply_to"] = settings.OUTREACH_REPLY_TO
    resp = client.post(
        _RESEND_URL,
        headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
        json=payload,
        timeout=15.0,
    )
    resp.raise_for_status()


def send_test_email(
    to: str,
    subject: Optional[str] = None,
    body: Optional[str] = None,
    agency_name: Optional[str] = None,
) -> dict:
    """Send ONE outreach email to an arbitrary address for testing — independent
    of campaigns and leads. Uses the supplied subject/body (so the dashboard can
    test unsaved edits) or the saved template, rendered with a sample agency.

    Returns the rendered subject, the From address, and the Reply-To (where a
    reply will land) so the caller can tell the user. Raises on misconfiguration
    / send failure.
    """
    if not settings.RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY non configurata")

    tmpl_subject, tmpl_body = get_template()
    name = agency_name or "La Tua Agenzia"
    rendered_subject = _render(subject or tmpl_subject, name, "Milano")
    rendered_body = _render(body or tmpl_body, name, "Milano")

    with httpx.Client() as client:
        _send_one(client, to, f"[TEST] {rendered_subject}", rendered_body)

    db.log_event(None, "test_email_sent", f"to {to} · from {_from_address()}")
    logger.info("Test outreach email sent to %s (from %s)", to, _from_address())
    return {
        "to": to,
        "from": _from_address(),
        "reply_to": settings.OUTREACH_REPLY_TO,
        "subject": rendered_subject,
    }


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
    subject_tmpl, body_tmpl = get_template()
    start_camp = db.get_campaign(campaign_id)
    city = start_camp["city"] if start_camp else ""
    sent = 0

    # Never email the same address twice — across all campaigns (already-sent) and
    # within this run (same email on two leads). Belt-and-suspenders on top of the
    # scrape-time dedup, so pre-existing duplicate rows can't be re-contacted.
    already_sent = db.sent_emails()
    seen_this_run: set = set()

    # Log exactly what goes out — rendered for the first lead — once per run, so
    # it's auditable in the dashboard feed and Render logs without flooding them
    # with one full body per recipient.
    sample = leads[0]
    preview_subject = _render(subject_tmpl, sample["agency_name"], city)
    preview_body = _render(body_tmpl, sample["agency_name"], city)
    db.log_event(
        campaign_id, "email_preview",
        f"FROM: {_from_address()}\nSUBJECT: {preview_subject}\n\n{preview_body}",
    )
    logger.info(
        "Campaign %s outreach — from=%s | subject=%r\n%s",
        campaign_id, _from_address(), preview_subject, preview_body,
    )

    with httpx.Client() as client:
        for lead in leads:
            # Respect a pause that arrived mid-run.
            camp = db.get_campaign(campaign_id)
            if not camp or camp["status"] == "paused":
                db.log_event(campaign_id, "outreach_paused", f"stopped after {sent} sent")
                break

            to = lead["email"]
            norm = (to or "").strip().lower()
            if norm in already_sent or norm in seen_this_run:
                db.set_email_status(lead["id"], "duplicate")
                db.log_event(
                    campaign_id, "skipped_duplicate_email",
                    f"{to} già contattata — non reinviata", lead_id=lead["id"],
                )
                continue

            subject = _render(subject_tmpl, lead["agency_name"], city)
            body = _render(body_tmpl, lead["agency_name"], city)
            try:
                _send_one(client, to, subject, body)
            except Exception as exc:
                # Leave email_status='pending' so a later resume can retry.
                db.log_event(
                    campaign_id, "error",
                    f"send to {to} failed: {exc}", lead_id=lead["id"],
                )
                logger.warning("Outreach send failed for lead %s (%s): %s", lead["id"], to, exc)
                continue

            db.mark_email_sent(lead["id"])
            seen_this_run.add(norm)
            db.increment_campaign(campaign_id, "total_emailed")
            db.log_event(campaign_id, "email_sent", f'{to} · "{subject}"', lead_id=lead["id"])
            sent += 1
            time.sleep(_SEND_DELAY)

    db.log_event(campaign_id, "outreach_finished", f"{sent} emails sent")
    logger.info("Campaign %s outreach done: %d sent", campaign_id, sent)
    return sent

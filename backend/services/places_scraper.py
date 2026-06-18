"""Google Places scraper for ApollonIA lead generation.

Given an Italian city, runs a Google Places Text Search for real-estate
agencies ("agenzia immobiliare {city}"), fetches Place Details for each hit,
and — when Places returns no email (it never does) — falls back to scraping the
agency website's contact page for an address.

This module is SYNCHRONOUS on purpose: it is meant to run inside a FastAPI
BackgroundTask (Starlette runs sync callables in a worker thread), so the 2s
inter-page sleeps Google requires won't block the event loop. It shares the
single SQLite connection via leadgen.db, which is opened check_same_thread=False.

Every step is logged to agent_logs and failures never abort the whole run.
"""

import logging
import re
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

from config import settings
from leadgen import db

logger = logging.getLogger(__name__)

_TEXTSEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"

_MAX_PAGES = 3            # Google returns up to 20 results/page → 60 max
_PAGE_SLEEP = 2.0         # Google needs ~2s before a next_page_token is valid
_RESULTS_PER_PAGE = 20

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Free-mailbox domains: kept only as a last resort (an agency's real inbox is
# almost always on its own domain).
_GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "libero.it", "hotmail.com",
    "hotmail.it", "outlook.com", "outlook.it", "live.it", "live.com",
    "icloud.com", "tin.it", "alice.it", "virgilio.it", "pec.it",
}

# Substrings that signal a regex false-positive rather than a contact address
# (asset filenames, tracking pixels, placeholders embedded in HTML/JS).
_EMAIL_NOISE = (
    "@2x", "@3x", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    "sentry", "wixpress", "example.com", "domain.com", "@sentry",
    "your-email", "email@", "name@",
)

_CONTACT_PATHS = ("contatti", "contact", "")  # "" = the homepage itself


def _is_generic(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].lower()
    return domain in _GENERIC_DOMAINS


def _clean_emails(html: str) -> list[str]:
    """Extract plausible contact emails from HTML, de-duplicated, order-preserving."""
    found: list[str] = []
    seen: set[str] = set()
    for raw in _EMAIL_RE.findall(html or ""):
        email = raw.strip().strip(".").lower()
        if email in seen:
            continue
        if any(noise in email for noise in _EMAIL_NOISE):
            continue
        seen.add(email)
        found.append(email)
    return found


def _scrape_website_email(client: httpx.Client, website: str) -> Optional[str]:
    """Try the contact page then the homepage; prefer a non-generic address."""
    if not website:
        return None
    parsed = urlparse(website if "://" in website else f"https://{website}")
    base = f"{parsed.scheme}://{parsed.netloc}"
    if not parsed.netloc:
        return None

    candidates: list[str] = []
    for path in _CONTACT_PATHS:
        url = urljoin(base + "/", path)
        try:
            resp = client.get(url, follow_redirects=True, timeout=8.0)
            if resp.status_code >= 400 or "text/html" not in resp.headers.get(
                "content-type", "text/html"
            ):
                continue
            for email in _clean_emails(resp.text):
                if email not in candidates:
                    candidates.append(email)
            # A non-generic hit on this page is good enough — stop early.
            if any(not _is_generic(e) for e in candidates):
                break
        except Exception as exc:
            logger.debug("Contact-page fetch failed for %s: %s", url, exc)
            continue

    if not candidates:
        return None
    for email in candidates:
        if not _is_generic(email):
            return email
    return candidates[0]  # only generic ones available — better than nothing


def _details(client: httpx.Client, place_id: str) -> dict:
    resp = client.get(
        _DETAILS_URL,
        params={
            "place_id": place_id,
            # Google never returns an `email` field; we request it anyway so the
            # day they ever do, the code below picks it up for free.
            "fields": "name,formatted_address,formatted_phone_number,website,email",
            "language": "it",
            "key": settings.GOOGLE_PLACES_API_KEY,
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") not in ("OK", "ZERO_RESULTS"):
        logger.warning("Place Details status=%s for %s", data.get("status"), place_id)
    return data.get("result", {}) or {}


def scrape_city(city: str, campaign_id: int, max_results: int = 60) -> int:
    """Scrape real-estate agencies in `city` into the campaign's lead list.

    Returns the number of NEW leads added. Never raises: per-place failures are
    logged and skipped so one bad agency can't abort the campaign.
    """
    if not settings.GOOGLE_PLACES_API_KEY:
        db.log_event(campaign_id, "error", "GOOGLE_PLACES_API_KEY not configured")
        logger.error("GOOGLE_PLACES_API_KEY not set — campaign %s scrape skipped", campaign_id)
        return 0

    query = f"agenzia immobiliare {city}"
    max_results = min(max_results, _MAX_PAGES * _RESULTS_PER_PAGE)
    db.log_event(campaign_id, "scrape_started", f'query="{query}", max={max_results}')

    new_leads = 0
    api_calls = 0  # credit-usage estimate: 1 per text-search page + 1 per details
    next_page_token: Optional[str] = None

    with httpx.Client(
        headers={"User-Agent": "ApollonIA-LeadBot/1.0 (+https://apollon-ia.com)"}
    ) as client:
        for page in range(_MAX_PAGES):
            if new_leads >= max_results:
                break

            # A paused/stopped campaign should not keep spending API credits.
            camp = db.get_campaign(campaign_id)
            if not camp or camp["status"] == "paused":
                db.log_event(campaign_id, "scrape_paused", f"stopped at page {page + 1}")
                break

            params = {"query": query, "language": "it", "region": "it",
                      "key": settings.GOOGLE_PLACES_API_KEY}
            if next_page_token:
                params = {"pagetoken": next_page_token,
                          "key": settings.GOOGLE_PLACES_API_KEY}
                # next_page_token needs a moment to become valid (Google quirk).
                time.sleep(_PAGE_SLEEP)

            try:
                resp = client.get(_TEXTSEARCH_URL, params=params, timeout=15.0)
                resp.raise_for_status()
                payload = resp.json()
                api_calls += 1
            except Exception as exc:
                db.log_event(campaign_id, "error", f"text search page {page + 1} failed: {exc}")
                logger.exception("Text Search failed for campaign %s", campaign_id)
                break

            status = payload.get("status")
            if status not in ("OK", "ZERO_RESULTS"):
                db.log_event(campaign_id, "error", f"text search status={status}")
                logger.error("Text Search status=%s for campaign %s", status, campaign_id)
                break

            results = payload.get("results", [])
            db.log_event(
                campaign_id, "page_fetched",
                f"page {page + 1}: {len(results)} results (~1 search credit)",
            )

            for place in results:
                if new_leads >= max_results:
                    break
                place_id = place.get("place_id")
                if not place_id or db.lead_exists(place_id):
                    continue

                try:
                    new_leads += _process_place(client, campaign_id, place, place_id)
                    api_calls += 1  # the Place Details lookup
                except Exception as exc:
                    db.log_event(
                        campaign_id, "error",
                        f"place {place.get('name', place_id)} failed: {exc}",
                        lead_id=None,
                    )
                    logger.exception("Lead processing failed for place %s", place_id)
                    continue

            next_page_token = payload.get("next_page_token")
            if not next_page_token:
                break

    db.log_event(
        campaign_id, "scrape_finished",
        f"{new_leads} new leads · ~{api_calls} Google API calls (credit estimate)",
    )
    logger.info(
        "Campaign %s scrape done: %d new leads, ~%d API calls",
        campaign_id, new_leads, api_calls,
    )
    return new_leads


def _process_place(client: httpx.Client, campaign_id: int, place: dict, place_id: str) -> int:
    """Fetch details + enrich one place, persist as a lead. Returns 1 if added."""
    details = _details(client, place_id)

    name = details.get("name") or place.get("name")
    address = details.get("formatted_address") or place.get("formatted_address")
    phone = details.get("formatted_phone_number")
    website = details.get("website")
    email = details.get("email")  # essentially always absent from Google

    if not email and website:
        email = _scrape_website_email(client, website)

    email_status = "pending" if email else "no_email"
    lead_id = db.add_lead(
        campaign_id=campaign_id,
        agency_name=name,
        address=address,
        phone=phone,
        website=website,
        email=email,
        google_place_id=place_id,
        email_status=email_status,
    )
    if lead_id is None:
        return 0  # raced with another insert of the same place_id

    db.log_event(campaign_id, "place_found", name or place_id, lead_id=lead_id)
    if email:
        db.log_event(campaign_id, "email_found", f"{name}: {email}", lead_id=lead_id)
    else:
        db.log_event(campaign_id, "no_email", name or place_id, lead_id=lead_id)

    db.increment_campaign(campaign_id, "total_found")
    return 1

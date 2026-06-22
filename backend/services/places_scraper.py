"""Google Places scraper for ApollonIA lead generation.

Given an Italian city, runs a Places API (New) Text Search for real-estate
agencies ("agenzia immobiliare {city}"). searchText returns name, address,
website and phone inline (one POST per page of 20) via a field mask, so no
per-place Place Details call is needed. When Places has no email (it never
does), we fall back to scraping the agency website's contact page.

This module is SYNCHRONOUS on purpose: it runs inside a FastAPI BackgroundTask
(Starlette runs sync callables in a worker thread), so network waits don't block
the event loop. It shares the single SQLite connection via leadgen.db, which is
opened check_same_thread=False.

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

# Places API (New) — searchText returns Place Details fields inline via a field
# mask, so one POST per page replaces the old textsearch + per-place details fan-out.
_TEXTSEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

# Fields Google should return. Unknown names are rejected predictably at the mask
# level (no risk of one bad field nuking the whole request, as on the legacy API).
_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,"
    "places.websiteUri,places.nationalPhoneNumber,"
    "places.internationalPhoneNumber,nextPageToken"
)

_MAX_PAGES = 3            # 20 results/page → 60 max
_RESULTS_PER_PAGE = 20
_PAGE_RETRY_SLEEP = 1.5   # brief backoff if a fresh pageToken isn't ready yet

# Many agency sites block non-browser user agents (403/empty); present a real one.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

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

# Common Italian/English contact-page paths, tried in addition to the homepage
# and any contact links discovered on it.
_CONTACT_PATHS = ("contatti", "contatti/", "contact", "contattaci", "chi-siamo")

# ── agency-name blocklist (dashboard-editable) ───────────────────────────────
# A comma-separated, case-insensitive substring list stored in app_settings. Any
# place whose name contains one of these is skipped, so excluded franchises
# (e.g. Tecnocasa) never become a lead. Defaults to empty (no exclusions) — set
# the blocklist from the dashboard.
_EXCLUSIONS_KEY = "exclude_keywords"
DEFAULT_EXCLUSIONS = ""


def get_exclusions_raw() -> str:
    """The raw comma-separated blocklist string (saved value, or the default)."""
    value = db.get_setting(_EXCLUSIONS_KEY)
    return DEFAULT_EXCLUSIONS if value is None else value


def get_exclusion_keywords() -> list[str]:
    """The blocklist parsed into lowercase, non-empty terms."""
    return [k.strip().lower() for k in get_exclusions_raw().split(",") if k.strip()]


def save_exclusions(raw: str) -> None:
    """Persist the blocklist. An empty string clears it (no exclusions)."""
    db.set_setting(_EXCLUSIONS_KEY, raw)


def _is_excluded(name: str, keywords: list[str]) -> bool:
    n = (name or "").lower()
    return any(k in n for k in keywords)


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


_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)


def _fetch_html(client: httpx.Client, url: str) -> Optional[str]:
    """GET a URL, returning its text if it looks like a fetchable HTML page."""
    try:
        resp = client.get(url, follow_redirects=True, timeout=10.0)
    except Exception as exc:
        logger.debug("Fetch failed for %s: %s", url, exc)
        return None
    if resp.status_code >= 400:
        return None
    ctype = resp.headers.get("content-type", "")
    if ctype and not any(t in ctype for t in ("html", "text", "xml")):
        return None  # binary (pdf/image/etc.) — nothing to scrape
    return resp.text


def _discover_contact_links(base: str, html: str) -> list[str]:
    """Find up to 3 same-site links whose href hints at a contact page."""
    links: list[str] = []
    for href in _HREF_RE.findall(html or ""):
        low = href.lower()
        if "contat" in low or "contact" in low:
            url = urljoin(base + "/", href)
            if url.startswith(base) and url not in links:
                links.append(url)
                if len(links) >= 3:
                    break
    return links


def _scrape_website_email(client: httpx.Client, website: str) -> Optional[str]:
    """Scrape an agency site for a contact email: read the homepage (and the
    contact links it exposes, plus common guessed paths), preferring a
    non-generic address. Returns None if nothing usable is found."""
    if not website:
        return None
    parsed = urlparse(website if "://" in website else f"https://{website}")
    if not parsed.netloc:
        return None
    base = f"{parsed.scheme}://{parsed.netloc}"

    candidates: list[str] = []
    # Homepage first: it often has the email in the footer AND links to /contatti.
    home = _fetch_html(client, base + "/")
    discovered: list[str] = []
    if home:
        candidates.extend(e for e in _clean_emails(home) if e not in candidates)
        discovered = _discover_contact_links(base, home)

    # Then discovered contact links + guessed paths, until a real address shows up.
    urls = list(discovered)
    for path in _CONTACT_PATHS:
        u = urljoin(base + "/", path)
        if u not in urls:
            urls.append(u)

    for url in urls:
        if any(not _is_generic(e) for e in candidates):
            break
        html = _fetch_html(client, url)
        if not html:
            continue
        for email in _clean_emails(html):
            if email not in candidates:
                candidates.append(email)

    if not candidates:
        return None
    for email in candidates:
        if not _is_generic(email):
            return email
    return candidates[0]  # only generic ones available — better than nothing


def _error_message(resp: httpx.Response) -> str:
    """Best-effort human-readable reason from a Places API (New) error response.
    The New API reports failures via HTTP status + an {"error": {...}} body."""
    try:
        return resp.json().get("error", {}).get("message") or f"HTTP {resp.status_code}"
    except Exception:
        return (resp.text or f"HTTP {resp.status_code}")[:200]


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
    exclusions = get_exclusion_keywords()
    db.log_event(campaign_id, "scrape_started", f'query="{query}", max={max_results}')
    if exclusions:
        db.log_event(campaign_id, "exclusions_active", ", ".join(exclusions))

    new_leads = 0
    excluded = 0
    duplicates = 0  # places already in the DB from earlier campaigns (skipped)
    api_calls = 0   # credit-usage estimate: 1 searchText request per page
    page_token: Optional[str] = None

    api_headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": settings.GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": _FIELD_MASK,
    }

    with httpx.Client(headers={"User-Agent": _BROWSER_UA}) as client:
        for page in range(_MAX_PAGES):
            if new_leads >= max_results:
                break

            # A paused/stopped campaign should not keep spending API credits.
            camp = db.get_campaign(campaign_id)
            if not camp or camp["status"] == "paused":
                db.log_event(campaign_id, "scrape_paused", f"stopped at page {page + 1}")
                break

            body = {
                "textQuery": query,
                "languageCode": "it",
                "regionCode": "IT",
                "pageSize": _RESULTS_PER_PAGE,
            }
            if page_token:
                body["pageToken"] = page_token

            # A freshly-issued pageToken can briefly return 400 INVALID_ARGUMENT;
            # retry that specific case before giving up.
            payload = None
            for attempt in range(3):
                try:
                    resp = client.post(
                        _TEXTSEARCH_URL, headers=api_headers, json=body, timeout=15.0
                    )
                    api_calls += 1
                except Exception as exc:
                    db.log_event(campaign_id, "error", f"search page {page + 1} failed: {exc}")
                    logger.exception("searchText request failed for campaign %s", campaign_id)
                    break

                if resp.status_code == 200:
                    payload = resp.json()
                    break

                err = _error_message(resp)
                if page_token and resp.status_code == 400 and attempt < 2:
                    time.sleep(_PAGE_RETRY_SLEEP)  # token not ready yet — retry
                    continue
                if page_token and resp.status_code == 400:
                    # Page-1 results are already saved; Google just wouldn't serve
                    # the next page. Not fatal — stop paginating.
                    db.log_event(
                        campaign_id, "pagination_stopped",
                        f"page {page + 1}: pageToken rejected ({err}) — page 1 results kept",
                    )
                    logger.warning(
                        "Campaign %s pagination stopped at page %d — %s",
                        campaign_id, page + 1, err,
                    )
                else:
                    db.log_event(
                        campaign_id, "error",
                        f"searchText HTTP {resp.status_code}: {err}",
                    )
                    logger.error(
                        "searchText HTTP %s for campaign %s — %s",
                        resp.status_code, campaign_id, err,
                    )
                break

            if payload is None:
                break

            places = payload.get("places", [])
            page_new0, page_dupe0, page_excl0 = new_leads, duplicates, excluded

            for place in places:
                if new_leads >= max_results:
                    break
                place_id = place.get("id")
                if not place_id:
                    continue
                if db.lead_exists(place_id):
                    duplicates += 1
                    continue

                name = (place.get("displayName") or {}).get("text", "")
                # Skip blocklisted franchises before doing any work for them.
                if exclusions and _is_excluded(name, exclusions):
                    excluded += 1
                    db.log_event(campaign_id, "skipped_excluded", name or place_id)
                    continue

                try:
                    new_leads += _process_place(client, campaign_id, place)
                except Exception as exc:
                    db.log_event(
                        campaign_id, "error", f"place {name or place_id} failed: {exc}"
                    )
                    logger.exception("Lead processing failed for place %s", place_id)
                    continue

            db.log_event(
                campaign_id, "page_fetched",
                f"page {page + 1}: {len(places)} results · "
                f"{new_leads - page_new0} new · "
                f"{duplicates - page_dupe0} già presenti · "
                f"{excluded - page_excl0} esclusi",
            )

            page_token = payload.get("nextPageToken")
            if not page_token:
                break

    db.log_event(
        campaign_id, "scrape_finished",
        f"{new_leads} new leads · {duplicates} già presenti · {excluded} esclusi · "
        f"~{api_calls} Google API calls (credit estimate)",
    )
    logger.info(
        "Campaign %s scrape done: %d new leads, ~%d API calls",
        campaign_id, new_leads, api_calls,
    )
    return new_leads


def _process_place(client: httpx.Client, campaign_id: int, place: dict) -> int:
    """Persist one searchText place as a lead, enriching email from its website.
    Returns 1 if a new lead was added, 0 on a duplicate place_id."""
    place_id = place.get("id")
    name = (place.get("displayName") or {}).get("text") or None
    address = place.get("formattedAddress")
    phone = place.get("nationalPhoneNumber") or place.get("internationalPhoneNumber")
    website = place.get("websiteUri")

    # Google never returns an email; derive it from the agency website if any.
    email = _scrape_website_email(client, website) if website else None

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

    site_note = website if website else "nessun sito su Google"
    db.log_event(campaign_id, "place_found", f"{name or place_id} · {site_note}", lead_id=lead_id)
    if email:
        db.log_event(campaign_id, "email_found", f"{name}: {email}", lead_id=lead_id)
    elif website:
        db.log_event(
            campaign_id, "no_email",
            f"{name} — sito {website}, nessuna email trovata", lead_id=lead_id,
        )
    else:
        db.log_event(
            campaign_id, "no_email",
            f"{name} — nessun sito web su Google", lead_id=lead_id,
        )

    db.increment_campaign(campaign_id, "total_found")
    return 1

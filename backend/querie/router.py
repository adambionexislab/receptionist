"""Querie landing page at /querie on the ApollonIA domain.

Pages are rendered per request (Jinja2) rather than served from the static
mount, because what the page offers depends on the date — waitlist, founder
offer with countdown, or plain trial — and the founder offer must vanish from
the HTML the moment it ends. Rendering is a few milliseconds; nothing to cache.

Routes:
  GET  /querie, /querie/                 landing page
  GET  /querie/ochrana-osobnych-udajov   privacy policy (placeholder)
  GET  /querie/vitajte                   after Stripe Checkout
  POST /querie/api/waitlist              waitlist sign-up
  POST /querie/api/checkout              store sign-up, open trial Checkout
  GET  /querie/zrusit, /querie/sprava    signed one-click cancel / manage links
  GET  /querie/pdf_report                PDF report page (email → download)
  POST /querie/api/report                store the email, return the signed download link
  GET  /querie/pdf_report/stiahnut       the PDF, for a signed link
  GET  /admin/querie-signups             CSV export (ADMIN_TOKEN)
  GET  /admin/querie-report-leads        CSV export of report emails (ADMIN_TOKEN)
"""

import asyncio
import csv
import hmac
import io
import logging
import time
from pathlib import Path
from typing import Optional

import stripe
from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, ValidationError, field_validator

from config import settings
from querie import billing, db, notify, offer, report
from querie import config as qc

logger = logging.getLogger(__name__)

router = APIRouter()

ASSETS_DIR = Path(__file__).parent / "assets"
_templates = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
_templates.filters["eur"] = offer.eur
_templates.filters["date_sk"] = offer.date_sk
_templates.filters["weekday_sk"] = offer.weekday_sk
_templates.filters["weekday_acc_sk"] = offer.weekday_acc_sk


# ── pages ────────────────────────────────────────────────────────────────────
def _context() -> dict:
    at = offer.now()
    phase = offer.current_phase(at)
    return {
        "product": qc.PRODUCT_NAME,
        "base": qc.BASE_PATH,
        "site_url": qc.SITE_URL,
        "phase": phase,
        # Waitlist before the launch date; waitlist after it only as the
        # "Stripe not configured" fallback, which must not mention a launch date.
        "launch_upcoming": at < qc.LAUNCH_AT,
        "founder_open": offer.founder_open(at),
        "launch_at": qc.LAUNCH_AT,
        "founder_ends_at": qc.FOUNDER_ENDS_AT,
        "founder_last_minute": offer.founder_deadline_display(),
        "server_now_ms": int(at.timestamp() * 1000),
        "price": qc.PRICE_EUR,
        "founder_price": offer.founder_price(),
        # Yearly billing is shown as pricing info on the waitlist, but offered at
        # checkout only once its Stripe price exists.
        "annual_available": phase == "waitlist" or offer.annual_ready(),
        "annual_monthly": qc.ANNUAL_MONTHLY_EUR,
        "annual_total": offer.annual_total(),
        "annual_saving": offer.annual_saving(),
        "annual_saving_percent": offer.annual_saving_percent(),
        "founder_percent": qc.FOUNDER_DISCOUNT_PERCENT,
        "founder_months": qc.FOUNDER_MONTHS,
        "trial_days": qc.TRIAL_DAYS,
        "first_charge": offer.first_charge_date(at),
        "refund_days": qc.REFUND_WINDOW_DAYS,
        "reminder_days": qc.REMINDER_DAYS_BEFORE,
        "testimonials": qc.TESTIMONIALS,
        "company_name": qc.COMPANY_NAME,
        "company_address": qc.COMPANY_ADDRESS,
        "company_id": qc.COMPANY_ID,
        "contact_email": qc.CONTACT_EMAIL,
        "terms_url": qc.TERMS_URL,
        "privacy_url": qc.PRIVACY_URL,
        "report_path": qc.REPORT_PATH,
        "report_title": qc.REPORT_TITLE,
        "meta_pixel_id": qc.meta_pixel_id(),
        "tiktok_pixel_id": qc.tiktok_pixel_id(),
        "year": at.year,
    }


def _render(template: str, **extra) -> HTMLResponse:
    html = _templates.get_template(template).render(**_context(), **extra)
    # Short cache only: the page changes at the launch and founder deadlines.
    return HTMLResponse(html, headers={"Cache-Control": "public, max-age=60"})


@router.get("/querie", include_in_schema=False)
async def querie_no_slash(request: Request):
    # The catch-all static mount would 404 this, so redirect explicitly —
    # keeping the query string, which carries the ad campaign's UTM tags.
    query = request.url.query
    return RedirectResponse(f"{qc.BASE_PATH}/" + (f"?{query}" if query else ""), status_code=308)


@router.get("/querie/", response_class=HTMLResponse, include_in_schema=False)
async def querie_page():
    return _render("index.html")


@router.get("/querie/ochrana-osobnych-udajov", response_class=HTMLResponse, include_in_schema=False)
async def querie_privacy():
    return _render("privacy.html")


@router.get("/querie/vitajte", response_class=HTMLResponse, include_in_schema=False)
async def querie_welcome():
    return _render("welcome.html")


# ── sign-up API ──────────────────────────────────────────────────────────────
_ATTRIBUTION_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "fbclid", "ttclid"}


def _clean_email(v: str) -> str:
    v = v.strip().lower()
    local, _, domain = v.partition("@")
    if not local or "." not in domain or " " in v or len(v) > 200:
        raise ValueError("Skontrolujte, prosím, e-mailovú adresu.")
    return v


def _clean_attribution(v) -> Optional[dict]:
    if not isinstance(v, dict):
        return None
    clean = {k: str(val)[:200] for k, val in v.items() if k in _ATTRIBUTION_KEYS and val}
    return clean or None


class SignupIn(BaseModel):
    name: str
    email: str
    phone: str
    company: Optional[str] = None
    consent: bool = False
    billing: str = "monthly"
    # Honeypot: invisible to people, filled in by naive bots.
    website: Optional[str] = None
    attribution: Optional[dict] = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("Vyplňte, prosím, meno.")
        if len(v) > 120:
            raise ValueError("Meno je príliš dlhé.")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _clean_email(v)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        v = v.strip()
        digits = "".join(c for c in v if c.isdigit())
        if not 9 <= len(digits) <= 15 or len(v) > 30:
            raise ValueError("Skontrolujte, prosím, telefónne číslo.")
        return v

    @field_validator("company")
    @classmethod
    def _company(cls, v: Optional[str]) -> Optional[str]:
        v = " ".join((v or "").split())
        return v[:160] or None

    @field_validator("billing")
    @classmethod
    def _billing(cls, v: str) -> str:
        if v not in ("monthly", "annual"):
            raise ValueError("Zvoľte, prosím, mesačnú alebo ročnú platbu.")
        return v

    @field_validator("attribution")
    @classmethod
    def _attribution(cls, v: Optional[dict]) -> Optional[dict]:
        return _clean_attribution(v)


def _parse(payload, consent_error: str) -> SignupIn:
    try:
        data = SignupIn.model_validate(payload)
    except ValidationError as exc:
        err = exc.errors()[0]
        if err.get("type") == "missing":
            raise HTTPException(422, "Vyplňte, prosím, všetky povinné polia.")
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        raise HTTPException(422, msg or "Skontrolujte, prosím, vyplnené údaje.")
    if not data.consent:
        raise HTTPException(422, consent_error)
    return data


# Per-IP limit on submissions; a campaign landing page attracts form spam.
_RATE_WINDOW_SECONDS = 10 * 60
_RATE_MAX = 8
_hits: dict[str, list[float]] = {}


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit(request: Request) -> None:
    key = _client_key(request)
    cutoff = time.time() - _RATE_WINDOW_SECONDS
    recent = [t for t in _hits.get(key, []) if t > cutoff]
    if len(recent) >= _RATE_MAX:
        raise HTTPException(429, "Príliš veľa pokusov. Skúste to, prosím, o chvíľu.")
    recent.append(time.time())
    _hits[key] = recent


async def _store(data: SignupIn, status: str, request: Request) -> dict:
    return await asyncio.to_thread(
        db.upsert_signup,
        email=data.email,
        name=data.name,
        phone=data.phone,
        company=data.company,
        status=status,
        consent_version=qc.CONSENT_VERSION,
        attribution=data.attribution,
        user_agent=(request.headers.get("user-agent") or "")[:300],
    )


@router.post("/querie/api/waitlist")
async def waitlist(request: Request, background: BackgroundTasks, payload: dict = Body(...)):
    _rate_limit(request)
    data = _parse(payload, "Bez súhlasu so spracovaním osobných údajov vás nemôžeme zapísať.")
    if data.website:
        logger.info("Querie waitlist honeypot tripped — dropped")
        return {"ok": True}
    signup = await _store(data, "waitlist", request)
    background.add_task(notify.notify_owner_waitlist, signup, data.attribution)
    logger.info("Querie waitlist sign-up id=%s", signup["id"])
    return {"ok": True}


@router.post("/querie/api/checkout")
async def checkout(request: Request, payload: dict = Body(...)):
    _rate_limit(request)
    data = _parse(payload, "Na pokračovanie potvrďte súhlas so Všeobecnými obchodnými podmienkami "
                           "a Zásadami ochrany osobných údajov.")
    if data.website:
        return JSONResponse({"detail": "Skúste to, prosím, znova."}, status_code=400)
    if offer.current_phase() == "waitlist":
        raise HTTPException(409, "Registrácia ešte nie je otvorená. Zapíšte sa na čakaciu listinu.")
    annual = data.billing == "annual"
    if annual and not offer.annual_ready():
        raise HTTPException(409, "Ročná platba momentálne nie je dostupná. Zvoľte, prosím, mesačnú.")
    await _store(data, "checkout_started", request)
    try:
        session_id, url, founder = await asyncio.to_thread(
            billing.create_checkout,
            email=data.email, name=data.name, phone=data.phone, company=data.company, annual=annual,
        )
    except stripe.error.StripeError as exc:
        logger.error("Querie checkout session failed: %s", exc)
        raise HTTPException(502, "Platobnú bránu sa nepodarilo otvoriť. Skúste to, prosím, znova.")
    await asyncio.to_thread(db.set_checkout_session, data.email, session_id, founder)
    return {"url": url}


# ── PDF report (lead magnet) ─────────────────────────────────────────────────
@router.get(qc.REPORT_PATH, response_class=HTMLResponse, include_in_schema=False)
async def report_page():
    return _render("report.html")


@router.get(qc.REPORT_PATH + "/", include_in_schema=False)
async def report_page_slash(request: Request):
    query = request.url.query
    return RedirectResponse(qc.REPORT_PATH + (f"?{query}" if query else ""), status_code=308)


@router.post("/querie/api/report")
async def report_request(request: Request, background: BackgroundTasks, payload: dict = Body(...)):
    _rate_limit(request)
    if not isinstance(payload, dict):
        raise HTTPException(422, "Skontrolujte, prosím, e-mailovú adresu.")
    try:
        email = _clean_email(str(payload.get("email") or ""))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if payload.get("consent") is not True:
        raise HTTPException(422, "Na stiahnutie reportu potvrďte, prosím, súhlas so spracovaním e-mailu.")
    if payload.get("website"):
        logger.info("Querie report honeypot tripped — dropped")
        return {"url": qc.REPORT_PATH}
    await asyncio.to_thread(
        db.upsert_report_lead,
        email=email,
        consent_version=qc.REPORT_CONSENT_VERSION,
        attribution=_clean_attribution(payload.get("attribution")),
        user_agent=(request.headers.get("user-agent") or "")[:300],
    )
    url = report.download_url(email)
    if not url:
        logger.error("Querie report: no SESSION_SECRET/ADMIN_TOKEN to sign download links")
        raise HTTPException(503, "Report sa teraz nepodarilo pripraviť. Skúste to, prosím, o chvíľu.")
    background.add_task(notify.send_report, to=email, url=report.download_url(email, absolute=True))
    logger.info("Querie report lead stored")
    return {"url": url}


_REPORT_MISSING = (
    "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
    "<title>Report</title><body style='font-family:system-ui;background:#0b0b0c;color:#f4f4f5;padding:2rem'>"
    "<p>{msg} Napíšte nám na <a style='color:#c49a00' href='mailto:{email}'>{email}</a> a pošleme vám ho.</p>"
)


@router.get(qc.REPORT_PATH + "/stiahnut", include_in_schema=False)
async def report_download(t: str = ""):
    email = report.verify_token(t)
    if not email:
        msg = "Tento odkaz na report je neplatný."
        return HTMLResponse(_REPORT_MISSING.format(msg=msg, email=qc.CONTACT_EMAIL), status_code=400)
    path = report.pdf_path()
    if not path:
        logger.error("Querie report PDF missing: %s", qc.REPORT_DIR / qc.REPORT_FILE)
        msg = "Report sa práve nedá stiahnuť."
        return HTMLResponse(_REPORT_MISSING.format(msg=msg, email=qc.CONTACT_EMAIL), status_code=503)
    await asyncio.to_thread(db.count_report_download, email)
    return FileResponse(path, media_type="application/pdf", filename=qc.REPORT_FILE)


# ── one-click cancel / manage ────────────────────────────────────────────────
_BAD_LINK = (
    "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
    "<title>Neplatný odkaz</title><body style='font-family:system-ui;background:#0b0b0c;color:#f4f4f5;padding:2rem'>"
    "<p>Tento odkaz je neplatný alebo už nefunguje. Napíšte nám na "
    "<a style='color:#c49a00' href='mailto:{email}'>{email}</a> a predplatné zrušíme za vás.</p>"
)


async def _portal_redirect(token: str, cancel: bool):
    subscription_id = billing.verify_token(token)
    bad = HTMLResponse(_BAD_LINK.format(email=qc.CONTACT_EMAIL), status_code=400)
    if not subscription_id or not settings.STRIPE_SECRET_KEY:
        return bad
    try:
        url = await asyncio.to_thread(billing.portal_url, subscription_id, cancel)
    except stripe.error.StripeError as exc:
        logger.error("Querie portal session for %s failed: %s", subscription_id, exc)
        return bad
    return RedirectResponse(url, status_code=303)


@router.get("/querie/zrusit", include_in_schema=False)
async def cancel_link(t: str = ""):
    return await _portal_redirect(t, cancel=True)


@router.get("/querie/sprava", include_in_schema=False)
async def manage_link(t: str = ""):
    return await _portal_redirect(t, cancel=False)


# ── admin export ─────────────────────────────────────────────────────────────
_admin_scheme = HTTPBearer(auto_error=False)


def _require_admin(credentials: Optional[HTTPAuthorizationCredentials] = Depends(_admin_scheme)) -> None:
    token = credentials.credentials if credentials else ""
    if not settings.ADMIN_TOKEN or not hmac.compare_digest(token, settings.ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _csv(rows: list[dict], columns: list[str], filename: str) -> Response:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    # BOM so Excel opens the Slovak diacritics correctly.
    return Response(
        "﻿" + out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/admin/querie-signups", dependencies=[Depends(_require_admin)])
async def export_signups():
    rows = await asyncio.to_thread(db.all_signups)
    return _csv(rows, [
        "created_at", "status", "name", "email", "phone", "company", "founder",
        "consent_at", "consent_version", "attribution", "stripe_customer_id",
        "stripe_subscription_id", "updated_at",
    ], "querie-signups.csv")


@router.get("/admin/querie-report-leads", dependencies=[Depends(_require_admin)])
async def export_report_leads():
    rows = await asyncio.to_thread(db.all_report_leads)
    return _csv(rows, [
        "created_at", "email", "downloads", "consent_at", "consent_version", "attribution", "updated_at",
    ], "querie-report-leads.csv")

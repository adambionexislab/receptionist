"""Querie emails, sent through Resend's HTTP API (Render blocks SMTP).

Owner notifications go to QUERIE_NOTIFY_EMAIL (or LEAD_EMAIL). Customer emails
are in Slovak and always carry the three promises the page makes: what is
charged and when, the reminder, and the one-click cancel link.
"""

import datetime
import logging
from decimal import Decimal
from typing import Optional

import httpx

from config import settings
from querie import config as qc
from querie import offer

logger = logging.getLogger(__name__)

_RESEND_URL = "https://api.resend.com/emails"


async def _send(to: str, subject: str, body: str) -> None:
    if not settings.RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not configured — Querie email to %s skipped", to)
        return
    payload = {
        "from": settings.QUERIE_EMAIL_FROM or settings.RESEND_FROM,
        "to": [to],
        "subject": subject,
        "text": body,
        "reply_to": qc.CONTACT_EMAIL,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _RESEND_URL,
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
            json=payload,
        )
        resp.raise_for_status()


async def _send_safely(to: Optional[str], subject: str, body: str, what: str) -> None:
    """Email is best-effort: the sign-up is already stored when this runs."""
    if not to:
        logger.warning("No recipient for Querie %s email — skipped", what)
        return
    try:
        await _send(to, subject, body)
        logger.info("Querie %s email sent to %s", what, to)
    except Exception as exc:
        logger.error("Querie %s email to %s failed: %s", what, to, exc)


def _owner() -> Optional[str]:
    return settings.QUERIE_NOTIFY_EMAIL or settings.LEAD_EMAIL


def _row(label: str, value) -> str:
    return f"{label + ':':<14}{value if value not in (None, '') else '—'}"


def _stamp() -> str:
    return offer.now().strftime("%d.%m.%Y %H:%M")


async def notify_owner_waitlist(signup: dict, attribution: Optional[dict]) -> None:
    lines = [
        f"Nový záujemca na čakacej listine {qc.PRODUCT_NAME}",
        "",
        _row("Meno", signup.get("name")),
        _row("E-mail", signup.get("email")),
        _row("Telefón", signup.get("phone")),
        _row("Firma", signup.get("company")),
        _row("Zdroj", ", ".join(f"{k}={v}" for k, v in (attribution or {}).items())),
        "",
        f"{_stamp()} (Bratislava)",
    ]
    await _send_safely(
        _owner(),
        f"[{qc.PRODUCT_NAME}] Čakacia listina — {signup.get('name')}",
        "\n".join(lines),
        "waitlist owner",
    )


async def notify_owner_trial(
    *, name: Optional[str], email: Optional[str], phone: Optional[str], company: Optional[str],
    quantity: int, founder: bool, first_charge: datetime.date, subscription_id: Optional[str],
) -> None:
    lines = [
        f"Nová skúšobná verzia {qc.PRODUCT_NAME}",
        "",
        _row("Meno", name),
        _row("E-mail", email),
        _row("Telefón", phone),
        _row("Firma", company),
        _row("Používatelia", quantity),
        _row("Zakladateľ", "áno (−25 % na 3 mesiace)" if founder else "nie"),
        _row("1. platba", offer.date_sk(first_charge)),
        _row("Subscription", subscription_id),
        "",
        f"{_stamp()} (Bratislava)",
    ]
    await _send_safely(
        _owner(),
        f"[{qc.PRODUCT_NAME}] Nová skúšobná verzia — {name or email}",
        "\n".join(lines),
        "trial owner",
    )


def _amount_lines(quantity: int, founder: bool) -> list[str]:
    unit = offer.founder_price() if founder else qc.PRICE_EUR
    total = unit * Decimal(quantity)
    per_user = f" ({quantity} × {offer.eur(unit)})" if quantity > 1 else ""
    lines = [f"Suma: {offer.eur(total)} bez DPH{per_user}"]
    if founder:
        lines.append(
            f"Zakladateľská cena platí prvé {qc.FOUNDER_MONTHS} mesiace, "
            f"potom {offer.eur(qc.PRICE_EUR)} mesačne za používateľa bez DPH."
        )
    return lines


def _cancel_lines(cancel_url: Optional[str]) -> list[str]:
    if cancel_url:
        return [f"Zrušiť jedným klikom: {cancel_url}"]
    return [f"Ak chcete zrušiť, stačí odpovedať na tento e-mail alebo napísať na {qc.CONTACT_EMAIL}."]


def _guarantee() -> str:
    return (
        f"Zabudli ste zrušiť? Ozvite sa do {qc.REFUND_WINDOW_DAYS} dní od prvej platby "
        "a vrátime vám ju celú, bez otázok."
    )


async def send_trial_welcome(
    *, to: Optional[str], name: Optional[str], quantity: int, founder: bool,
    first_charge: datetime.date, cancel_url: Optional[str],
) -> None:
    greeting = f"Dobrý deň, {name}," if name else "Dobrý deň,"
    lines = [
        greeting,
        "",
        f"ďakujeme! Vaša {qc.TRIAL_DAYS}-dňová skúšobná verzia {qc.PRODUCT_NAME} je aktívna.",
        "",
        "Dnes ste nezaplatili nič.",
        f"Prvá platba: {offer.date_sk(first_charge)}, ak predtým nezrušíte.",
        *_amount_lines(quantity, founder),
        f"{qc.REMINDER_DAYS_BEFORE} dni pred prvou platbou vám pošleme pripomienku.",
        "",
        *_cancel_lines(cancel_url),
        _guarantee(),
        "",
        # TODO: describe the real onboarding step (app download, call forwarding).
        "Čoskoro sa vám ozveme s nastavením aplikácie a presmerovania hovorov.",
        "",
        "S pozdravom,",
        f"tím {qc.PRODUCT_NAME}",
        f"{qc.COMPANY_NAME}, {qc.COMPANY_ADDRESS}",
    ]
    await _send_safely(
        to, f"{qc.PRODUCT_NAME}: skúšobná verzia je aktívna", "\n".join(lines), "trial welcome"
    )


async def send_trial_reminder(
    *, to: Optional[str], name: Optional[str], quantity: int, founder: bool,
    first_charge: datetime.date, cancel_url: Optional[str],
) -> None:
    greeting = f"Dobrý deň, {name}," if name else "Dobrý deň,"
    lines = [
        greeting,
        "",
        f"ako sme sľúbili, pripomíname sa: vaša skúšobná verzia {qc.PRODUCT_NAME} "
        f"končí a {offer.date_sk(first_charge)} prebehne prvá platba.",
        "",
        *_amount_lines(quantity, founder),
        "",
        f"Ak chcete {qc.PRODUCT_NAME} používať ďalej, nemusíte robiť nič.",
        *_cancel_lines(cancel_url),
        _guarantee(),
        "",
        "S pozdravom,",
        f"tím {qc.PRODUCT_NAME}",
    ]
    await _send_safely(
        to,
        f"{qc.PRODUCT_NAME}: prvá platba {offer.date_sk(first_charge)}",
        "\n".join(lines),
        "trial reminder",
    )

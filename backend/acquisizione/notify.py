"""Meeting-summary email for a confirmed Acquisizione record.

Until this shipped, confirming a record only wrote it to the database — the
agent got no copy of the commitments they'd just made in the meeting. This
emails the whole thing (missing data, tasks, listing text, property data, and
the transcript) to the same address the phone agent's lead summaries go to:
the tenant's `lead_email`, falling back to the platform owner's LEAD_EMAIL.

Sent via Resend's HTTP API with httpx, matching call/router._send_lead_email
(Render blocks outbound SMTP). Written in the record's market language using
the per-market strings in acquisizione/{content,locales}.py.

Best-effort by design: the record is already saved and published before this
runs, so a mail failure is logged and reported, never raised.
"""

import logging
from typing import Any, Optional

import httpx

from acquisizione import content, locales, schema
from config import settings

logger = logging.getLogger(__name__)

_RESEND_URL = "https://api.resend.com/emails"

_CONTENT: dict[str, dict] = {"it": content.IT, "sk": locales.SK}


def _content_for(market: str) -> dict:
    return _CONTENT.get(market, content.IT)


def _format_value(value: Any, c: dict) -> str:
    if isinstance(value, bool):
        return c["email_yes"] if value else c["email_no"]
    return str(value)


def _task_lines(tasks: list[dict], c: dict) -> list[str]:
    """Tasks grouped by who owes them, since the agent's own to-dos and what
    they're waiting on from the seller are acted on differently."""
    lines: list[str] = []
    for owner in ("agente", "venditore"):
        owned = [t for t in tasks if (t.get("owner") or "agente") == owner]
        if not owned:
            continue
        lines.append(f"— {c['email_owner_' + owner]} —")
        for task in owned:
            flags = []
            if task.get("scadenza"):
                flags.append(f"{c['email_task_due']}: {task['scadenza']}")
            if task.get("blocca_pubblicazione"):
                flags.append(c["email_task_blocking"])
            suffix = f"  [{' · '.join(flags)}]" if flags else ""
            lines.append(f"  • {task.get('descrizione', '')}{suffix}")
            if task.get("citazione"):
                lines.append(f"      “{task['citazione']}”")
        lines.append("")
    return lines


def build_body(record: dict[str, Any]) -> str:
    """The plain-text email body for a confirmed record."""
    market = record.get("market") or "it"
    c = _content_for(market)
    fields = record.get("listing_fields") or {}
    labels = c["field_labels"]

    lines: list[str] = [c["email_intro"], ""]

    missing = record.get("missing_required") or []
    lines.append(c["email_section_missing"])
    if missing:
        lines += [f"  • {labels.get(f, f)}" for f in missing]
    else:
        lines.append(f"  {c['email_none_missing']}")
    lines.append("")

    tasks = record.get("tasks") or []
    lines.append(c["email_section_tasks"])
    lines += _task_lines(tasks, c) if tasks else [f"  {c['email_no_tasks']}", ""]

    lines.append(c["email_section_listing_text"])
    lines.append(record.get("listing_text") or c["email_no_text"])
    lines.append("")

    lines.append(c["email_section_fields"])
    for name in schema.field_names(market):
        value = fields.get(name)
        if value is None or value == "":
            continue
        lines.append(f"  {labels.get(name, name)}: {_format_value(value, c)}")
    lines.append("")

    transcript = (record.get("transcript") or "").strip()
    if transcript:
        lines.append(c["email_section_transcript"])
        lines.append(transcript)

    return "\n".join(lines)


async def send_meeting_summary(tenant: dict[str, Any], record: dict[str, Any]) -> bool:
    """Email a confirmed record's summary to the agency. Returns False (and
    logs) when it couldn't be sent — the caller reports that to the dashboard
    rather than failing the confirmation."""
    recipient: Optional[str] = tenant.get("lead_email") or settings.LEAD_EMAIL
    if not settings.RESEND_API_KEY or not recipient:
        logger.warning(
            "RESEND_API_KEY/lead email not configured — acquisizione summary skipped"
        )
        return False

    market = record.get("market") or "it"
    c = _content_for(market)
    address = (record.get("listing_fields") or {}).get("indirizzo_o_zona") or "—"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                _RESEND_URL,
                headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
                json={
                    "from": settings.RESEND_FROM,
                    "to": [recipient],
                    "subject": c["email_subject"].format(address=address),
                    "text": build_body(record),
                },
            )
            resp.raise_for_status()
        logger.info("Acquisizione summary emailed to %s (record %s)", recipient, record.get("id"))
        return True
    except Exception as exc:
        logger.error("Failed to send acquisizione summary email: %s", exc)
        return False

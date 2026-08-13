"""Transcript → structured sales-meeting note.

One AI call over the rep's spoken debrief, using the Responses API's
structured-output mode (text.format = json_schema, strict) so the output is
guaranteed to match salesnotes/schema.py's shape. Same mechanics as
acquisizione/extraction.py — this is the general-purpose sibling of that
property-intake extraction, with no listing fields in sight.

Fails loudly (raises ExtractionError) rather than silently falling back: a
failure leaves the transcript untouched (see router.py's /finish) so the rep
can just retry.
"""

import json
import logging
from typing import Any

import httpx

from config import settings
from salesnotes import content, schema

logger = logging.getLogger(__name__)

_RESPONSES_URL = "https://api.openai.com/v1/responses"


class ExtractionError(Exception):
    """Raised when the extraction call fails or returns something that doesn't
    validate. Callers leave the note untouched and let the rep retry, rather
    than silently discarding the transcript."""


def _extract_output_text(data: dict[str, Any]) -> str:
    """Pull the assistant's JSON text out of a Responses API payload. Prefers
    the top-level `output_text` convenience field, falling back to walking the
    `output` array (mirrors acquisizione/extraction.py)."""
    top = data.get("output_text")
    if isinstance(top, str) and top:
        return top
    parts: list[str] = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    parts.append(part.get("text", ""))
    return "".join(parts)


async def extract(transcript: str, language: str) -> dict[str, Any]:
    """Run the extraction call and return storage-ready note fields."""
    if not settings.OPENAI_API_KEY:
        raise ExtractionError("OPENAI_API_KEY not configured")
    if not transcript.strip():
        raise ExtractionError("Transcript is empty")

    body = {
        "model": settings.EXTRACTION_MODEL,
        "instructions": content.instructions(language),
        "input": transcript,
        "reasoning": {"effort": "medium"},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "sales_meeting_note",
                "strict": True,
                "schema": schema.envelope_schema(),
            }
        },
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                _RESPONSES_URL,
                headers={
                    "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            if resp.status_code >= 400:
                logger.error(
                    "Note extraction request failed: %s — %s", resp.status_code, resp.text,
                )
                raise ExtractionError(
                    f"OpenAI extraction call rejected (status {resp.status_code})"
                )
            data = resp.json()
    except httpx.HTTPError as exc:
        logger.error("Note extraction request error: %s", exc)
        raise ExtractionError(f"Extraction request failed: {exc}") from exc

    raw_text = _extract_output_text(data).strip()
    if not raw_text:
        raise ExtractionError("Extraction returned no output")

    try:
        parsed = json.loads(raw_text)
        result = schema.ExtractionResult.model_validate(parsed)
    except Exception as exc:
        logger.error("Note extraction failed validation: %s — raw=%s", exc, raw_text)
        raise ExtractionError(f"Extraction output did not match the schema: {exc}") from exc

    return schema.normalize(result.model_dump())

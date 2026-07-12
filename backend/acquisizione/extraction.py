"""Transcript → structured listing extraction (Phase 3).

One AI call over the meeting transcript, market-scoped: instructions, field
schema, and required-fields list all come from acquisizione/{content,locales,
schema}.py. Uses the Responses API's structured-output mode (text.format =
json_schema, strict) so the model's output is guaranteed to match the schema
shape; acquisizione/schema.py's Pydantic models still validate/coerce it
before it's trusted.

Fails loudly (raises ExtractionError) rather than silently falling back, per
the operator's instruction — an extraction failure leaves the record's
transcript untouched (see router.py's /finish) so the agent can just retry.
"""

import json
import logging
from typing import Any

import httpx

from acquisizione import content, locales, schema
from config import settings

logger = logging.getLogger(__name__)

_RESPONSES_URL = "https://api.openai.com/v1/responses"

_CONTENT: dict[str, dict[str, str]] = {"it": content.IT, "sk": locales.SK}


def _content_for(market: str) -> dict[str, str]:
    return _CONTENT.get(market, content.IT)


class ExtractionError(Exception):
    """Raised when the extraction call fails or returns something that
    doesn't validate. Callers should leave the record untouched and let the
    agent retry, rather than silently discarding the transcript."""


def _extract_output_text(data: dict[str, Any]) -> str:
    """Pull the assistant's JSON text out of a Responses API payload. Prefers
    the top-level `output_text` convenience field, falling back to walking the
    `output` array (mirrors call/router.py's _extract_response_text)."""
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


async def extract(transcript: str, market: str) -> dict[str, Any]:
    """Run the extraction call and return the storage-ready envelope:
    {market, listing_fields, missing_required, listing_text, tasks}."""
    if not settings.OPENAI_API_KEY:
        raise ExtractionError("OPENAI_API_KEY not configured")
    if not transcript.strip():
        raise ExtractionError("Transcript is empty")

    market = market if market in _CONTENT else "it"
    body = {
        "model": settings.EXTRACTION_MODEL,
        "instructions": _content_for(market)["extraction_instructions"],
        "input": transcript,
        "reasoning": {"effort": "medium"},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "acquisizione_listing",
                "strict": True,
                "schema": schema.envelope_schema(market),
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
                    "Extraction request failed: %s — %s", resp.status_code, resp.text,
                )
                raise ExtractionError(
                    f"OpenAI extraction call rejected (status {resp.status_code})"
                )
            data = resp.json()
    except httpx.HTTPError as exc:
        logger.error("Extraction request error: %s", exc)
        raise ExtractionError(f"Extraction request failed: {exc}") from exc

    raw_text = _extract_output_text(data).strip()
    if not raw_text:
        raise ExtractionError("Extraction returned no output")

    try:
        parsed = json.loads(raw_text)
        result = schema.ExtractionResult.model_validate(parsed)
    except Exception as exc:
        logger.error("Extraction output failed validation: %s — raw=%s", exc, raw_text)
        raise ExtractionError(f"Extraction output did not match the schema: {exc}") from exc

    missing = schema.missing_required(market, result.listing_fields)

    return {
        "market": market,
        "listing_fields": result.listing_fields,
        "missing_required": missing,
        "listing_text": result.listing_text,
        "tasks": [t.model_dump() for t in result.tasks],
    }

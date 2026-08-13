"""Shape of a sales-meeting note: the JSON Schema sent to OpenAI, and the
Pydantic model that validates what comes back (see salesnotes/extraction.py).

Same split as acquisizione/schema.py, and for the same reason: the schema is
hand-written rather than derived from the model below, because strict
Structured Outputs has requirements Pydantic's generated schema doesn't meet
(every property in `required`, even nullable ones; additionalProperties: false
at every level). Pydantic only validates/coerces the response.
"""

from typing import Any

from pydantic import BaseModel, Field

# How the meeting went overall. Untranslated internal tokens — the dashboard
# renders them as Italian badges, exactly like the lead statuses next to them.
OUTCOMES = ("positive", "neutral", "negative")

# The list-valued fields, which are also the ones stored together as JSON in
# the note's `details` column (see salesnotes/db.py).
LIST_FIELDS = ("went_well", "went_wrong", "objections", "next_steps")

# The scalar fields, stored as their own columns so a note can be listed and
# counted without unpacking JSON.
TEXT_FIELDS = ("title", "customer", "outcome", "summary")

_LIST_DESCRIPTIONS = {
    "went_well": "Cosa è andato bene nella riunione, una voce per punto",
    "went_wrong": "Cosa è andato male o è stato difficile, una voce per punto",
    "objections": "Obiezioni, dubbi e resistenze espressi dal cliente",
    "next_steps": "Cose da fare e impegni presi, senza attribuirli a nessuno",
}


def envelope_schema() -> dict[str, Any]:
    """JSON Schema for the extraction call's structured output."""
    props: dict[str, Any] = {
        "title": {
            "type": "string",
            "description": "Titolo breve della riunione (con chi e di cosa)",
        },
        "customer": {
            "type": ["string", "null"],
            "description": "Cliente o azienda di cui si parla; null se non nominato",
        },
        "outcome": {
            "type": ["string", "null"],
            "enum": [*OUTCOMES, None],
            "description": "Come è andata nel complesso; null se non deducibile",
        },
        "summary": {
            "type": "string",
            "description": "2-4 frasi che raccontano la riunione",
        },
    }
    for name in LIST_FIELDS:
        props[name] = {
            "type": "array",
            "items": {"type": "string"},
            "description": _LIST_DESCRIPTIONS[name],
        }
    return {
        "type": "object",
        "properties": props,
        "required": list(props.keys()),
        "additionalProperties": False,
    }


class ExtractionResult(BaseModel):
    title: str = ""
    customer: str | None = None
    outcome: str | None = None
    summary: str = ""
    went_well: list[str] = Field(default_factory=list)
    went_wrong: list[str] = Field(default_factory=list)
    objections: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


def _clean_text(value: Any) -> str | None:
    text = ("" if value is None else str(value)).strip()
    return text or None


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in (_clean_text(v) for v in value) if item]


def normalize(fields: dict[str, Any]) -> dict[str, Any]:
    """Storage-ready note fields: trimmed text, no blank list entries, and an
    outcome that is either one of OUTCOMES or None.

    Used on both the extraction output and the rep's own edits, so a note saved
    by hand can't hold a shape the extraction would never produce.
    """
    outcome = _clean_text(fields.get("outcome"))
    normalized: dict[str, Any] = {
        "title": _clean_text(fields.get("title")),
        "customer": _clean_text(fields.get("customer")),
        "outcome": outcome if outcome in OUTCOMES else None,
        "summary": _clean_text(fields.get("summary")),
    }
    for name in LIST_FIELDS:
        normalized[name] = _clean_list(fields.get(name))
    return normalized

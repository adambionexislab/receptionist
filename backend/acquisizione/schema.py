"""Per-market schema for the seller-meeting → structured-listing extraction
(see acquisizione/extraction.py). Field NAMES stay fixed Italian-flavoured
identifiers for both markets — like the vendita/affitto tokens in
call/locales.py — only the free-text VALUES the model fills in are written in
the market's language.

The JSON Schema sent to OpenAI is hand-written rather than derived from the
Pydantic models below, because the API's strict Structured Outputs mode has
specific requirements (every property listed in `required`, even nullable
ones, and `additionalProperties: false` at every object level) that don't
fall out of Pydantic's default schema generation. Pydantic is used only to
validate/coerce the response we get back.
"""

from typing import Any, Literal, Optional

from pydantic import BaseModel

# name -> (json_type, description_it, description_sk)
_COMMON_FIELDS: dict[str, tuple[str, str, str]] = {
    "tipologia": ("string", "Tipo di immobile (es. appartamento, villa, attico)", "Typ nehnuteľnosti (napr. byt, dom, mezonet)"),
    "indirizzo_o_zona": ("string", "Indirizzo esatto o zona/quartiere dell'immobile", "Presná adresa alebo lokalita/štvrť nehnuteľnosti"),
    "superficie_mq": ("number", "Superficie in metri quadrati", "Úžitková plocha v metroch štvorcových"),
    "locali": ("number", "Numero di locali", "Počet izieb (napr. 3-izbový)"),
    "camere": ("number", "Numero di camere da letto", "Počet spální"),
    "bagni": ("number", "Numero di bagni", "Počet kúpeľní"),
    "piano": ("string", "Piano dell'immobile (es. 2, terra, attico)", "Poschodie nehnuteľnosti (napr. 2., prízemie, posledné)"),
    "piani_totali": ("number", "Numero totale di piani dell'edificio", "Celkový počet poschodí budovy"),
    "ascensore": ("boolean", "Presenza dell'ascensore", "Prítomnosť výťahu"),
    "riscaldamento": ("string", "Tipo di riscaldamento (es. autonomo, centralizzato)", "Typ kúrenia (napr. individuálne, centrálne, plynové)"),
    "stato_immobile": ("string", "Stato dell'immobile (es. nuovo, buono, da ristrutturare)", "Stav nehnuteľnosti (napr. novostavba, dobrý, na rekonštrukciu)"),
    "anno_costruzione": ("number", "Anno di costruzione", "Rok výstavby"),
    "esposizione": ("string", "Esposizione dell'immobile", "Orientácia nehnuteľnosti"),
    "spazi_esterni": ("string", "Spazi esterni (balcone, terrazzo, giardino)", "Vonkajšie priestory (balkón, terasa, lodžia, záhrada)"),
    "posto_auto": ("string", "Posto auto o box", "Garáž alebo parkovacie miesto"),
    "cantina": ("boolean", "Presenza di cantina", "Prítomnosť pivnice"),
    "arredato": ("string", "Stato dell'arredamento (arredato, parziale, vuoto)", "Stav zariadenia (zariadený, čiastočne, nezariadený)"),
    "prezzo_richiesto": ("number", "Prezzo richiesto in EUR", "Požadovaná cena v EUR"),
    "note_venditore": ("string", "Altre note rilevanti riferite dal venditore", "Ďalšie relevantné poznámky od predávajúceho"),
}

# name -> (json_type, description)
_IT_EXTENSION: dict[str, tuple[str, str]] = {
    "classe_energetica": ("string", "Classe energetica (A4–G)"),
    "ipe": ("number", "Indice di prestazione energetica in kWh/m²a"),
    "spese_condominiali": ("number", "Spese condominiali mensili in EUR"),
    "tipo_proprieta": ("string", "Tipo di proprietà (piena/nuda)"),
}

_SK_EXTENSION: dict[str, tuple[str, str]] = {
    "energeticka_trieda": ("string", "Energetická trieda (A0–G)"),
    "energeticky_certifikat_esiste": ("boolean", "Či existuje energetický certifikát"),
    "mesacne_poplatky": ("number", "Mesačné poplatky / fond opráv v EUR"),
    "druh_vlastnictva": ("string", "Druh vlastníctva (osobné/družstevné)"),
}

# Per-market required fields — drives "missing_required" / blocking. Confirmed
# with the operator as-specified; see prompt_realtor_tool.md.
REQUIRED_FIELDS: dict[str, list[str]] = {
    "it": ["superficie_mq", "prezzo_richiesto", "classe_energetica", "indirizzo_o_zona", "spese_condominiali"],
    "sk": ["superficie_mq", "prezzo_richiesto", "energeticka_trieda", "indirizzo_o_zona", "druh_vlastnictva"],
}


def _prop(json_type: str, description: str) -> dict[str, Any]:
    return {"type": [json_type, "null"], "description": description}


def _common_props(market: str) -> dict[str, Any]:
    idx = 2 if market == "sk" else 1
    return {name: _prop(spec[0], spec[idx]) for name, spec in _COMMON_FIELDS.items()}


def _extension_props(market: str) -> dict[str, Any]:
    ext = _SK_EXTENSION if market == "sk" else _IT_EXTENSION
    return {name: _prop(jtype, desc) for name, (jtype, desc) in ext.items()}


def listing_fields_schema(market: str) -> dict[str, Any]:
    """JSON Schema for `listing_fields`: common fields + that market's
    extension. Every property is nullable but still listed in `required` —
    strict mode requires every property to appear there; nullability comes
    from the `type` array instead."""
    props = {**_common_props(market), **_extension_props(market)}
    return {
        "type": "object",
        "properties": props,
        "required": list(props.keys()),
        "additionalProperties": False,
    }


_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "descrizione": {"type": "string", "description": "Task description, written in the market's language"},
        "owner": {"type": "string", "enum": ["agente", "venditore"]},
        "scadenza": {"type": ["string", "null"], "description": "Due date as an ISO date, or null if not stated"},
        "blocca_pubblicazione": {"type": "boolean", "description": "True if this task gates publishing the listing"},
        "citazione": {"type": ["string", "null"], "description": "Short verbatim transcript snippet justifying this task"},
    },
    "required": ["descrizione", "owner", "scadenza", "blocca_pubblicazione", "citazione"],
    "additionalProperties": False,
}


def envelope_schema(market: str) -> dict[str, Any]:
    """Full JSON Schema for the extraction call's structured output. Does NOT
    ask the model for `missing_required` — that's computed deterministically
    from `REQUIRED_FIELDS` after validation (see `missing_required` below),
    so it can never drift from the actual required list."""
    return {
        "type": "object",
        "properties": {
            "listing_fields": listing_fields_schema(market),
            "listing_text": {"type": "string", "description": "Draft listing description, written in the market's language"},
            "tasks": {"type": "array", "items": _TASK_SCHEMA},
        },
        "required": ["listing_fields", "listing_text", "tasks"],
        "additionalProperties": False,
    }


class Task(BaseModel):
    descrizione: str
    owner: Literal["agente", "venditore"]
    scadenza: Optional[str] = None
    blocca_pubblicazione: bool = False
    citazione: Optional[str] = None


class ExtractionResult(BaseModel):
    listing_fields: dict[str, Any]
    listing_text: str
    tasks: list[Task]


def _is_missing(value: Any) -> bool:
    return value is None or value == ""


def missing_required(market: str, listing_fields: dict[str, Any]) -> list[str]:
    """Required fields for `market` that are null/absent in listing_fields."""
    return [f for f in REQUIRED_FIELDS.get(market, []) if _is_missing(listing_fields.get(f))]

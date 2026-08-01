import pytest

from acquisizione import schema


def test_required_fields_differ_on_energy_certificate():
    assert "classe_energetica" in schema.REQUIRED_FIELDS["it"]
    assert "energeticka_trieda" in schema.REQUIRED_FIELDS["sk"]
    assert "classe_energetica" not in schema.REQUIRED_FIELDS["sk"]
    assert "energeticka_trieda" not in schema.REQUIRED_FIELDS["it"]


def test_missing_required_flags_null_and_absent_fields():
    fields = {"superficie_mq": 80, "prezzo_richiesto": None, "indirizzo_o_zona": "Via Roma 5"}
    missing = schema.missing_required("it", fields)
    assert "superficie_mq" not in missing
    assert "prezzo_richiesto" in missing  # explicitly null
    assert "classe_energetica" in missing  # absent entirely
    assert "indirizzo_o_zona" not in missing


def test_missing_required_treats_zero_and_false_as_present():
    fields = {
        "tipo_annuncio": "vendita", "locali": 3,
        "superficie_mq": 80, "prezzo_richiesto": 0, "classe_energetica": "G",
        "indirizzo_o_zona": "Via Roma 5", "spese_condominiali": 0,
    }
    assert schema.missing_required("it", fields) == []


@pytest.mark.parametrize("market", ["it", "sk"])
def test_room_count_is_required_in_both_markets(market):
    """rooms:0 would exclude a listing from every room-based search the phone
    agent runs, so a missing count has to be surfaced to the agent instead of
    silently shipping a listing nobody can find."""
    assert "locali" in schema.REQUIRED_FIELDS[market]
    assert "locali" in schema.missing_required(market, {})


def test_missing_required_treats_empty_string_as_missing():
    fields = {"indirizzo_o_zona": "", "superficie_mq": 80}
    assert "indirizzo_o_zona" in schema.missing_required("it", fields)


def test_missing_required_unknown_market_returns_empty():
    assert schema.missing_required("fr", {}) == []


@pytest.mark.parametrize("market", ["it", "sk"])
def test_listing_fields_schema_is_strict_mode_compliant(market):
    """Every property must be nullable-typed but still listed in `required`,
    and additionalProperties must be false — OpenAI's strict Structured
    Outputs mode requires this shape."""
    fields_schema = schema.listing_fields_schema(market)
    assert fields_schema["additionalProperties"] is False
    assert set(fields_schema["required"]) == set(fields_schema["properties"].keys())
    for prop in fields_schema["properties"].values():
        assert "null" in prop["type"]


@pytest.mark.parametrize("market", ["it", "sk"])
def test_listing_fields_schema_includes_market_extension(market):
    fields_schema = schema.listing_fields_schema(market)
    for field in schema.REQUIRED_FIELDS[market]:
        assert field in fields_schema["properties"]


def test_envelope_schema_omits_missing_required_from_model_output():
    """missing_required is computed server-side, not requested from the
    model — see extraction.py's docstring."""
    env = schema.envelope_schema("it")
    assert "missing_required" not in env["properties"]
    assert set(env["required"]) == {"listing_fields", "listing_text", "notes"}


@pytest.mark.parametrize("market", ["it", "sk"])
def test_extraction_prompt_tells_the_model_to_count_enumerated_rooms(market):
    """Sellers routinely name each room without ever stating a total, so the
    prompt has to carve out counting as an explicit exception to the
    otherwise-strict 'never derive a value' rule."""
    from acquisizione import extraction
    instructions = extraction._content_for(market)["extraction_instructions"]
    # The exclusion list is what keeps the count matching portal convention.
    excluded = ["bagni", "corridoi"] if market == "it" else ["kúpeľne", "chodby"]
    for term in excluded:
        assert term in instructions
    counting_verb = "CONTA" if market == "it" else "SPOČÍTAJTE"
    assert counting_verb in instructions


def test_extraction_result_validates_a_well_formed_response():
    result = schema.ExtractionResult.model_validate({
        "listing_fields": {"superficie_mq": 80},
        "listing_text": "Bell'appartamento in centro.",
        "notes": "PUNTI CHIAVE\n- Vende per trasferimento.\n\nDA RISOLVERE\n- Planimetria mancante.",
    })
    assert "DA RISOLVERE" in result.notes


def test_to_listing_maps_fields_onto_the_phone_agent_shape():
    listing = schema.to_listing({
        "tipo_annuncio": "affitto",
        "indirizzo_o_zona": "Via Roma 5, Lodi",
        "locali": 3, "superficie_mq": 85, "prezzo_richiesto": 1200,
    }, "Bell'appartamento in centro.")
    assert listing["type"] == "affitto"
    assert listing["address"] == "Via Roma 5, Lodi"
    assert listing["rooms"] == 3
    assert listing["size_sqm"] == 85
    assert listing["price"] == 1200
    assert listing["text"] == "Bell'appartamento in centro."
    assert listing["available"] is True


def test_to_listing_defaults_unknown_type_to_vendita():
    """An unrecognised/missing tipo_annuncio must still yield a searchable
    listing — the phone agent filters on exactly vendita/affitto."""
    assert schema.to_listing({}, "")["type"] == "vendita"
    assert schema.to_listing({"tipo_annuncio": "boh"}, "")["type"] == "vendita"


def test_to_listing_coerces_non_numeric_values():
    """The model can return a float, a numeric string, or null for numbers —
    none of which may blow up the listing insert."""
    listing = schema.to_listing(
        {"superficie_mq": "85.5", "prezzo_richiesto": None, "locali": 3.0}, ""
    )
    assert listing["size_sqm"] == 85
    assert listing["price"] == 0
    assert listing["rooms"] == 3


def test_extraction_result_requires_notes():
    """notes carries the meeting's commitments now, so a response without it
    is incomplete rather than merely sparse."""
    with pytest.raises(Exception):
        schema.ExtractionResult.model_validate({
            "listing_fields": {},
            "listing_text": "x",
        })


@pytest.mark.parametrize("market", ["it", "sk"])
def test_notes_prompt_forbids_assigning_tasks_to_a_person(market):
    """Attributing a commitment to the agent vs. the seller from a transcript
    proved unreliable, so the prompt must explicitly leave tasks unassigned."""
    from acquisizione import extraction
    instructions = extraction._content_for(market)["extraction_instructions"]
    headings = (
        ["PUNTI CHIAVE", "DA RISOLVERE"] if market == "it"
        else ["KĽÚČOVÉ BODY", "NA VYRIEŠENIE"]
    )
    for heading in headings:
        assert heading in instructions
    forbid = "NON indicare chi" if market == "it" else "NEUVÁDZAJTE, kto"
    assert forbid in instructions

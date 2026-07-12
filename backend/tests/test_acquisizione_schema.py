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
        "superficie_mq": 80, "prezzo_richiesto": 0, "classe_energetica": "G",
        "indirizzo_o_zona": "Via Roma 5", "spese_condominiali": 0,
    }
    assert schema.missing_required("it", fields) == []


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
    assert set(env["required"]) == {"listing_fields", "listing_text", "tasks"}


def test_extraction_result_validates_a_well_formed_response():
    result = schema.ExtractionResult.model_validate({
        "listing_fields": {"superficie_mq": 80},
        "listing_text": "Bell'appartamento in centro.",
        "tasks": [{
            "descrizione": "Invia planimetria",
            "owner": "agente",
            "scadenza": None,
            "blocca_pubblicazione": False,
            "citazione": "le mando la planimetria",
        }],
    })
    assert result.tasks[0].owner == "agente"


def test_extraction_result_rejects_invalid_owner():
    with pytest.raises(Exception):
        schema.ExtractionResult.model_validate({
            "listing_fields": {},
            "listing_text": "x",
            "tasks": [{"descrizione": "x", "owner": "not_a_valid_owner"}],
        })

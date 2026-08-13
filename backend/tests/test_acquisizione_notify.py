"""Tests for the confirmed-meeting summary email body (acquisizione/notify.py).

The body is the agency's only copy of what was committed to in the meeting,
so these check the parts that would quietly lose information: the meeting
notes (key facts + unassigned open points), the missing-data list, and the
per-market language of both.
"""

import pytest

from acquisizione import notify

_RECORD = {
    "id": "r1",
    "market": "it",
    "listing_fields": {
        "tipo_annuncio": "vendita",
        "indirizzo_o_zona": "Via Roma 5, Lodi",
        "superficie_mq": 85,
        "ascensore": True,
        "cantina": False,
        "prezzo_richiesto": None,   # omitted from the body
        "note_venditore": "",       # omitted from the body
    },
    "missing_required": ["prezzo_richiesto", "classe_energetica"],
    "listing_text": "Bell'appartamento in centro.",
    "notes": (
        "PUNTI CHIAVE\n"
        "- Vende per trasferimento di lavoro, tempi stretti.\n"
        "\n"
        "DA RISOLVERE\n"
        "- Recuperare la planimetria aggiornata.\n"
        "- Ottenere l'APE (blocca la pubblicazione)\n"
        "\n"
        "DETTO DAL VENDITORE\n"
        "- \"le mando la planimetria\"\n"
    ),
    "transcript": "Agente: buongiorno...",
}


def test_body_reproduces_the_meeting_notes_verbatim():
    """The notes are already structured plain text by the time they reach the
    email — the agent edited them — so nothing may be reformatted or dropped."""
    body = notify.build_body(_RECORD)
    for line in _RECORD["notes"].strip().splitlines():
        assert line in body


def test_body_keeps_tasks_unassigned():
    """Attributing a task to a person is exactly what this design removed;
    the email must not reintroduce owner grouping."""
    body = notify.build_body(_RECORD)
    assert "Recuperare la planimetria aggiornata." in body
    assert "(blocca la pubblicazione)" in body
    assert "— Agente —" not in body
    assert "— Venditore —" not in body


def test_body_lists_missing_fields_by_readable_label():
    body = notify.build_body(_RECORD)
    assert "Classe energetica" in body
    assert "Prezzo richiesto" in body


def test_body_includes_listing_text_and_transcript():
    body = notify.build_body(_RECORD)
    assert "Bell'appartamento in centro." in body
    assert "Agente: buongiorno..." in body


def test_body_renders_booleans_and_omits_empty_fields():
    body = notify.build_body(_RECORD)
    assert "Ascensore: sì" in body
    assert "Cantina: no" in body
    # None/"" valued fields are skipped rather than printed as blanks
    assert "Note del venditore:" not in body


def test_listing_type_is_spelled_out_in_the_market_language():
    """tipo_annuncio is stored as an untranslated 'vendita'/'affitto' token —
    the agency must never be shown the raw token, least of all in Slovak."""
    assert "Tipo di annuncio: Vendita" in notify.build_body(_RECORD)
    sk = notify.build_body({**_RECORD, "market": "sk"})
    assert "Typ inzerátu: Predaj" in sk
    assert "vendita" not in sk.lower()


def test_body_handles_a_record_with_no_notes_or_missing_fields():
    body = notify.build_body({
        **_RECORD, "notes": "", "missing_required": [], "transcript": "",
    })
    assert "Nessuna nota." in body
    assert "tutti i dati obbligatori sono presenti" in body
    assert "Trascrizione" not in body  # section omitted when there's none


def test_slovak_record_renders_in_slovak():
    body = notify.build_body({**_RECORD, "market": "sk"})
    assert "Poznámky zo stretnutia" in body
    assert "Úžitková plocha (m²): 85" in body
    # No Italian section headings/labels leak in. (The notes and transcript
    # are verbatim meeting content and are deliberately never translated.)
    assert "Note della riunione" not in body
    assert "Chýbajúce údaje" in body


def test_unknown_market_falls_back_to_italian():
    body = notify.build_body({**_RECORD, "market": "fr"})
    assert "Note della riunione" in body


@pytest.mark.asyncio
async def test_send_returns_false_when_resend_is_not_configured(monkeypatch):
    """A missing API key must be reported, not raised — the confirmation has
    already been saved by the time this runs."""
    monkeypatch.setattr(notify.settings, "RESEND_API_KEY", None)
    assert await notify.send_meeting_summary({"lead_email": "a@b.com"}, _RECORD) is False

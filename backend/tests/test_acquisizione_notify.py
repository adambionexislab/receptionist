"""Tests for the confirmed-meeting summary email body (acquisizione/notify.py).

The body is the agency's only copy of what was committed to in the meeting,
so these check the parts that would quietly lose information: tasks (grouped
by owner, with due dates/blocking flags/quotes), the missing-data list, and
the per-market language of both.
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
    "tasks": [
        {"descrizione": "Inviare planimetria", "owner": "agente",
         "scadenza": "2026-08-05", "blocca_pubblicazione": False,
         "citazione": "le mando la planimetria"},
        {"descrizione": "Portare l'APE", "owner": "venditore",
         "scadenza": None, "blocca_pubblicazione": True, "citazione": None},
    ],
    "transcript": "Agente: buongiorno...",
}


def test_body_includes_tasks_with_owner_due_date_and_blocking_flag():
    body = notify.build_body(_RECORD)
    assert "Inviare planimetria" in body
    assert "Portare l'APE" in body
    assert "Agente" in body and "Venditore" in body
    assert "2026-08-05" in body
    assert "BLOCCA PUBBLICAZIONE" in body
    assert "le mando la planimetria" in body  # the justifying quote


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


def test_body_handles_a_record_with_no_tasks_or_missing_fields():
    body = notify.build_body({
        **_RECORD, "tasks": [], "missing_required": [], "transcript": "",
    })
    assert "Nessun impegno registrato" in body
    assert "tutti i dati obbligatori sono presenti" in body
    assert "Trascrizione" not in body  # section omitted when there's none


def test_slovak_record_renders_in_slovak():
    body = notify.build_body({**_RECORD, "market": "sk"})
    assert "Úlohy" in body
    assert "Maklér" in body and "Predávajúci" in body
    assert "BLOKUJE ZVEREJNENIE" in body
    assert "Úžitková plocha (m²): 85" in body
    # No Italian section headings/labels leak in. (The transcript itself is
    # verbatim meeting content and is deliberately never translated.)
    assert "Attività da svolgere" not in body
    assert "Chýbajúce údaje" in body


def test_unknown_market_falls_back_to_italian():
    body = notify.build_body({**_RECORD, "market": "fr"})
    assert "Attività da svolgere" in body


@pytest.mark.asyncio
async def test_send_returns_false_when_resend_is_not_configured(monkeypatch):
    """A missing API key must be reported, not raised — the confirmation has
    already been saved by the time this runs."""
    monkeypatch.setattr(notify.settings, "RESEND_API_KEY", None)
    assert await notify.send_meeting_summary({"lead_email": "a@b.com"}, _RECORD) is False

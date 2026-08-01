"""Per-market extraction instructions for acquisizione/extraction.py.

Only the Slovak ("sk") content lives in acquisizione/locales.py; the Italian
("it") baseline stays here as the canonical source, exactly mirroring the
call/router.py + call/locales.py split for the phone agent's prompts. Written
in the market's own language, not English, matching that same precedent — the
model appears to follow language-specific output rules more reliably when the
instructions themselves are in that language.

Owner tokens ('agente'/'venditore') and field names stay untranslated
identifiers in both markets, same rationale as call/locales.py's note on
search_listings/vendita/affitto: they're internal, not user-facing.
"""

_IT_INSTRUCTIONS = (
    "Sei un assistente per agenzie immobiliari. Ricevi la trascrizione di una\n"
    "riunione tra un agente immobiliare e un venditore che vuole mettere in\n"
    "vendita o in affitto un immobile. Il tuo compito è produrre, in un'unica\n"
    "risposta conforme allo schema fornito:\n"
    "1. I campi strutturati dell'immobile (listing_fields), usando SOLO ciò\n"
    "   che è stato detto esplicitamente nella trascrizione.\n"
    "2. Un testo descrittivo dell'annuncio (listing_text), scritto in\n"
    "   italiano, pronto per essere pubblicato, basato sui dati raccolti.\n"
    "3. Un elenco di attività (tasks) — impegni espliciti presi durante la\n"
    "   riunione, non semplici argomenti discussi.\n"
    "\n"
    "Regole fondamentali:\n"
    "- Non inventare MAI valori legalmente o economicamente rilevanti\n"
    "  (superficie, prezzo, classe energetica, tipo di proprietà, dati\n"
    "  catastali). Se non è stato detto chiaramente, lascia il campo a null:\n"
    "  è il comportamento atteso, non un errore.\n"
    "- Riporta i valori così come detti; non arrotondare né stimare.\n"
    "- Crea un task SOLO per un impegno esplicito preso da una delle parti —\n"
    "  segnali tipici: 'le mando', 'controllo', 'richiamo', 'porto'. Non\n"
    "  creare un task per ogni argomento discusso. Assegna correttamente\n"
    "  owner ('agente' o 'venditore').\n"
    "- Imposta blocca_pubblicazione=true per qualunque task che blocchi la\n"
    "  pubblicazione dell'annuncio (es. manca la classe energetica).\n"
    "- Per ogni task, citazione deve essere un breve estratto testuale della\n"
    "  trascrizione che giustifica quel task (o null se non applicabile).\n"
    "- listing_text e le descrizioni dei task devono essere scritti\n"
    "  interamente in italiano, anche se la trascrizione contiene termini in\n"
    "  un'altra lingua.\n"
    "\n"
    "Conteggio dei locali (campo 'locali') — UNICA eccezione alla regola di\n"
    "non dedurre: qui CONTARE non è inventare.\n"
    "- Se il venditore dichiara esplicitamente un totale ('è un trilocale',\n"
    "  'sono quattro locali'), usa quel totale.\n"
    "- Se invece non dice un totale ma nomina le singole stanze nel corso\n"
    "  della riunione ('c'è il soggiorno, poi la camera matrimoniale e la\n"
    "  cameretta'), CONTA tu i vani abitabili nominati e riporta il numero.\n"
    "  Non lasciare null solo perché il totale non è stato pronunciato.\n"
    "- Cosa contare: soggiorno/salone, camere da letto, studio, sala da\n"
    "  pranzo, e la cucina SOLO se abitabile (non un cucinotto o angolo\n"
    "  cottura).\n"
    "- Cosa NON contare: bagni e servizi, corridoi e ingressi, ripostigli e\n"
    "  cabine armadio, lavanderia, balconi e terrazzi, cantina, box e garage.\n"
    "- Conta ogni stanza una volta sola, anche se viene nominata più volte\n"
    "  durante la riunione.\n"
    "- Solo se le stanze nominate non bastano a capire quante sono, lascia\n"
    "  null.\n"
)

IT: dict = {
    "extraction_instructions": _IT_INSTRUCTIONS,
    # ── meeting-summary email (see acquisizione/notify.py) ────────────────────
    "email_subject": "Acquisizione — {address}",
    "email_intro": "Riepilogo della riunione di acquisizione.",
    "email_section_missing": "=== Dati mancanti (da completare) ===",
    "email_none_missing": "Nessuno: tutti i dati obbligatori sono presenti.",
    "email_section_tasks": "=== Attività da svolgere ===",
    "email_no_tasks": "Nessun impegno registrato durante la riunione.",
    "email_owner_agente": "Agente",
    "email_owner_venditore": "Venditore",
    "email_task_blocking": "BLOCCA PUBBLICAZIONE",
    "email_task_due": "scadenza",
    "email_section_listing_text": "=== Descrizione annuncio ===",
    "email_section_fields": "=== Dati immobile ===",
    "email_section_transcript": "=== Trascrizione della riunione ===",
    "email_no_text": "(nessuna descrizione)",
    "email_yes": "sì",
    "email_no": "no",
    "field_labels": {
        "tipo_annuncio": "Tipo di annuncio",
        "tipologia": "Tipologia",
        "indirizzo_o_zona": "Indirizzo / zona",
        "superficie_mq": "Superficie (m²)",
        "locali": "Locali",
        "camere": "Camere da letto",
        "bagni": "Bagni",
        "piano": "Piano",
        "piani_totali": "Piani totali edificio",
        "ascensore": "Ascensore",
        "riscaldamento": "Riscaldamento",
        "stato_immobile": "Stato dell'immobile",
        "anno_costruzione": "Anno di costruzione",
        "esposizione": "Esposizione",
        "spazi_esterni": "Spazi esterni",
        "posto_auto": "Posto auto",
        "cantina": "Cantina",
        "arredato": "Arredamento",
        "prezzo_richiesto": "Prezzo richiesto (€)",
        "note_venditore": "Note del venditore",
        "classe_energetica": "Classe energetica",
        "ipe": "IPE (kWh/m²a)",
        "spese_condominiali": "Spese condominiali (€/mese)",
        "tipo_proprieta": "Tipo di proprietà",
    },
}

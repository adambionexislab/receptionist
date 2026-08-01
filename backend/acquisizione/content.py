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
    "3. Le note della riunione (notes): un testo strutturato in italiano con\n"
    "   i fatti importanti emersi E i punti aperti da risolvere.\n"
    "\n"
    "Regole fondamentali:\n"
    "- Non inventare MAI valori legalmente o economicamente rilevanti\n"
    "  (superficie, prezzo, classe energetica, tipo di proprietà, dati\n"
    "  catastali). Se non è stato detto chiaramente, lascia il campo a null:\n"
    "  è il comportamento atteso, non un errore.\n"
    "- Riporta i valori così come detti; non arrotondare né stimare.\n"
    "- listing_text e notes devono essere scritti interamente in italiano,\n"
    "  anche se la trascrizione contiene termini in un'altra lingua.\n"
    "\n"
    "Note della riunione (campo 'notes')\n"
    "Scrivi un testo in chiaro, pronto da leggere e da modificare a mano\n"
    "dall'agente. Usa ESATTAMENTE queste tre sezioni, in quest'ordine e con\n"
    "queste intestazioni:\n"
    "\n"
    "PUNTI CHIAVE\n"
    "- Fatti importanti emersi nella riunione che NON sono già nei campi\n"
    "  strutturati: motivo della vendita, tempistiche, situazione del\n"
    "  venditore, vincoli, lavori fatti o da fare, particolarità\n"
    "  dell'immobile, aspettative sul prezzo, eventuali criticità.\n"
    "\n"
    "DA RISOLVERE\n"
    "- Punti aperti e cose da fare emersi dalla riunione: documenti da\n"
    "  procurare, verifiche da fare, informazioni mancanti, promesse fatte\n"
    "  ('le mando la planimetria', 'controllo in catasto', 'richiamo').\n"
    "- NON indicare chi se ne deve occupare: scrivi solo la cosa da fare.\n"
    "  Non scrivere 'l'agente deve...' né 'il venditore deve...'.\n"
    "- Se un punto blocca la pubblicazione dell'annuncio, aggiungi in fondo\n"
    "  alla riga '(blocca la pubblicazione)'.\n"
    "\n"
    "DETTO DAL VENDITORE\n"
    "- Poche citazioni testuali brevi e rilevanti, tra virgolette, così\n"
    "  l'agente può verificare da dove arrivano le informazioni.\n"
    "\n"
    "Regole per le note:\n"
    "- Usa trattini (-) per gli elenchi, una riga per punto, frasi brevi.\n"
    "- Niente Markdown (niente #, *, **): è testo semplice.\n"
    "- Non ripetere i campi strutturati (mq, prezzo, locali...) se non\n"
    "  aggiungono contesto.\n"
    "- Se una sezione non ha contenuto, scrivi comunque l'intestazione e\n"
    "  sotto una riga '- nessuno'.\n"
    "- Basati SOLO sulla trascrizione: non aggiungere consigli o supposizioni.\n"
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
    "email_section_notes": "=== Note della riunione ===",
    "email_no_notes": "Nessuna nota.",
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

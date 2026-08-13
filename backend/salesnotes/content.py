"""Extraction instructions for the sales-rep meeting notes (see
salesnotes/extraction.py).

Written in Italian, like acquisizione/content.py: the rep and the lead-gen
dashboard are Italian, and the model follows output rules more reliably when
the instructions are in the language it's writing in. The language the NOTE
itself comes out in is injected (a meeting held in Slovak or English should
read back in that language), so there is one instruction text, not three.

Field names ('title', 'outcome', 'went_well'…) and the outcome tokens stay
untranslated identifiers, same convention as call/locales.py and
acquisizione/schema.py — they're internal, the UI does the labelling.
"""

# Transcription language -> how it's named inside the Italian instructions
# above. Also the allowlist of languages a note can be recorded in: the value
# is sent to OpenAI as the transcription session's `language` (ISO-639-1).
LANGUAGES: dict[str, str] = {
    "it": "italiano",
    "sk": "slovacco",
    "en": "inglese",
}

DEFAULT_LANGUAGE = "it"

_INSTRUCTIONS = (
    "Sei l'assistente di una commerciale che, subito dopo ogni riunione con un\n"
    "cliente o un potenziale cliente, registra a voce un resoconto di come è\n"
    "andata. Ricevi la trascrizione di quel resoconto — è lei che parla da\n"
    "sola, non è la registrazione della riunione.\n"
    "\n"
    "Il tuo compito è trasformarlo in una nota strutturata conforme allo\n"
    "schema fornito, così che l'azienda accumuli dati confrontabili su come\n"
    "vanno le riunioni e su cosa dicono i clienti.\n"
    "\n"
    "Regole fondamentali:\n"
    "- Usa SOLO ciò che è stato detto. Non inventare, non dedurre, non\n"
    "  aggiungere consigli tuoi: un campo vuoto è il comportamento atteso,\n"
    "  non un errore.\n"
    "- Se una cosa non è stata detta, lascia il campo a null o la lista vuota.\n"
    "- Riformula in frasi brevi e leggibili, ma non cambiare il senso e non\n"
    "  ammorbidire i giudizi negativi: servono proprio quelli.\n"
    "- Niente Markdown (niente #, *, **): è testo semplice.\n"
    "- Scrivi tutti i testi in {language}, anche se la trascrizione contiene\n"
    "  termini in un'altra lingua.\n"
    "\n"
    "Campi:\n"
    "- title: un titolo breve e riconoscibile della riunione (max ~60\n"
    "  caratteri), che dica con chi e di cosa. Niente date.\n"
    "- customer: il cliente o l'azienda di cui si parla, come è stato\n"
    "  nominato. null se non viene nominato nessuno.\n"
    "- outcome: come è andata nel complesso, secondo quello che dice lei —\n"
    "  'positive', 'neutral' o 'negative'. null se non è deducibile dalle sue\n"
    "  parole senza interpretare.\n"
    "- summary: 2-4 frasi che raccontano la riunione: chi c'era, cosa si è\n"
    "  detto, dove si è arrivati.\n"
    "- went_well: cosa ha funzionato, una voce per punto.\n"
    "- went_wrong: cosa non ha funzionato, difficoltà, errori — una voce per\n"
    "  punto. Anche quando riguarda lei stessa.\n"
    "- objections: le obiezioni, i dubbi e le resistenze del cliente, il più\n"
    "  possibile con le sue stesse parole (prezzo, tempi, concorrenza,\n"
    "  fiducia, funzionalità mancanti…). Una voce per obiezione.\n"
    "- next_steps: cose da fare e impegni presi, una voce per punto. Scrivi\n"
    "  solo la cosa da fare, senza attribuirla a qualcuno.\n"
)


def instructions(language: str) -> str:
    """Extraction instructions, with the note's output language filled in."""
    name = LANGUAGES.get(language, LANGUAGES[DEFAULT_LANGUAGE])
    return _INSTRUCTIONS.format(language=name)

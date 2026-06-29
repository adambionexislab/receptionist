import asyncio
import audioop
import base64
import json
import logging
import wave
from pathlib import Path
from typing import Any

import httpx
import websockets
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse

from config import settings
from listings.store import store, tenant_stores
from tenants import db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/call")

_GREETING_TEXT = "Buongiorno, sono Apollonia. Come posso aiutarla?"


def _format_listing_brief(listing: dict[str, Any]) -> str:
    return (
        f"{listing.get('address', '?')} — {listing.get('zone', '?')} — "
        f"{listing.get('type', '?')} — {listing.get('rooms', '?')} locali — "
        f"{listing.get('size_sqm', '?')}mq — €{listing.get('price', '?')}"
    )


def _same_number(a: str | None, b: str | None) -> bool:
    """True if two phone numbers look like the same line, comparing the last 9
    significant digits so +39 / leading-0 prefixes and spacing don't matter."""
    if not a or not b:
        return False
    da = "".join(ch for ch in a if ch.isdigit())
    db = "".join(ch for ch in b if ch.isdigit())
    if len(da) < 9 or len(db) < 9:
        return bool(da) and da == db
    return da[-9:] == db[-9:]


_CALLER_INFO_LABELS: dict[str, str] = {
    "name": "Nome",
    "employment_status": "Situazione lavorativa",
    "monthly_income": "Reddito mensile netto",
    "household_size": "Persone nel nucleo familiare",
    "has_pets": "Animali domestici",
    "move_in_date": "Data di ingresso desiderata",
    "has_mortgage_preapproval": "Mutuo pre-approvato",
    "has_property_to_sell": "Immobile da vendere",
    "sale_timeline": "Tempistiche per il rogito",
    "visit_availability": "Disponibilità per visita",
}


def _load_static_audio(filename: str) -> tuple[str, float] | None:
    """Load static/<filename> and return (base64 mulaw 8kHz payload, duration
    in seconds), or None if the file is missing or fails to load."""
    path = Path(__file__).parent.parent / "static" / filename
    if not path.exists():
        return None
    try:
        with wave.open(str(path), "rb") as wf:
            pcm = wf.readframes(wf.getnframes())
            rate = wf.getframerate()
            channels = wf.getnchannels()
            width = wf.getsampwidth()
        if width != 2:
            pcm = audioop.lin2lin(pcm, width, 2)
        if channels > 1:
            pcm = audioop.tomono(pcm, 2, 1)
        if rate != 8000:
            pcm, _ = audioop.ratecv(pcm, 2, 1, rate, 8000, None)
        mulaw = audioop.lin2ulaw(pcm, 2)
        return base64.b64encode(mulaw).decode(), len(mulaw) / 8000.0
    except Exception as exc:
        logger.error("Failed to load audio file %s: %s", filename, exc)
        return None


_greeting = _load_static_audio("greeting.wav")
_GREETING_AUDIO: str | None = _greeting[0] if _greeting else None
_GREETING_DURATION: float = _greeting[1] if _greeting else 0.0

_goodbye = _load_static_audio("goodbye.wav")
_GOODBYE_AUDIO: str | None = _goodbye[0] if _goodbye else None
_GOODBYE_DURATION: float = _goodbye[1] if _goodbye else 0.0

# Only the first line of the system prompt is tenant-specific; the body
# (language rule, Type A/B/C flows, geography rules, qualifying questions)
# is identical for every tenant.
_DEFAULT_FIRST_LINE = (
    "# Ruolo e obiettivo\n"
    "Sei Apollonia, la receptionist virtuale di uno studio immobiliare.\n"
)

_SYSTEM_PROMPT_BODY = (
    "Il tuo compito è rispondere alle chiamate come farebbe una receptionist\n"
    "umana: capire perché chiama la persona, aiutarla riguardo agli immobili,\n"
    "raccogliere le informazioni necessarie e inoltrare la richiesta a un\n"
    "agente immobiliare.\n"
    "\n"
    "# Personalità e tono\n"
    "Parli come una receptionist umana vera ed esperta di uno studio\n"
    "immobiliare, non come una voce sintetica.\n"
    "- Usa un'intonazione naturale e un ritmo vario: rallenta e accelera come\n"
    "  nel parlato reale, evita la cadenza piatta o robotica.\n"
    "- Mantieni un tono caldo, cordiale e professionale.\n"
    "- Non aggiungere mai suoni di riempimento, esitazioni o versi come\n"
    "  'mh-mh', 'mmm', 'ehm' appiccicati prima o dopo le frasi: suonano\n"
    "  innaturali. La naturalezza viene dall'intonazione, non dai versi.\n"
    "\n"
    "# Lingua\n"
    "## REGOLA SULLA LINGUA — PRIORITÀ MASSIMA\n"
    "Ascolta la primissima frase del chiamante. Se non è in italiano, da quel\n"
    "momento in poi TUTTE le tue risposte per il resto della chiamata devono\n"
    "essere interamente nella lingua del chiamante, dalla prima parola —\n"
    "senza dire prima nulla in italiano.\n"
    "Questa regola vale SEMPRE, comprese le risposte generate subito dopo il\n"
    "risultato di uno strumento (search_listings, get_listing_by_address,\n"
    "mark_listing_interest, record_caller_info, leave_message, ecc.). I dati\n"
    "restituiti dagli strumenti (indirizzi a parte) sono sempre in italiano:\n"
    "traducili tu nella lingua del chiamante prima di parlarne, non leggerli\n"
    "né riassumerli in italiano. Non tornare MAI in italiano una volta\n"
    "cambiata lingua, anche se le tue istruzioni e i dati sono in italiano.\n"
    "Rispondi sempre in italiano salvo quanto indicato sopra, con tono\n"
    "professionale ma cordiale.\n"
    "\n"
    "# Ragionamento\n"
    "- Per risposte dirette, conferme brevi e semplici domande di chiarimento,\n"
    "  rispondi subito senza ragionare.\n"
    "- Prima di scegliere quale strumento usare o di passare da un tipo di\n"
    "  chiamata all'altro, ragiona brevemente su qual è il passo giusto.\n"
    "\n"
    "# Preamboli\n"
    "Un preambolo è una frase BREVE che dici subito prima di usare uno\n"
    "strumento, per far capire al chiamante che ti stai attivando (così non\n"
    "resta in silenzio mentre cerchi o registri i dati).\n"
    "- Usa un preambolo SOLO prima di chiamare uno strumento che richiede\n"
    "  qualche istante: get_listing_by_address, search_listings,\n"
    "  record_caller_info, leave_message.\n"
    "- DESCRIVI l'azione che stai facendo, non un'esitazione. Esempi:\n"
    "  'Controllo subito la disponibilità.', 'Verifico l'indirizzo\n"
    "  dell'immobile.', 'Cerco gli immobili adatti, un attimo.',\n"
    "  'Registro i suoi dati, un momento.'\n"
    "- Tieni il preambolo a UNA frase breve e varia le parole tra un turno\n"
    "  e l'altro: non ripetere sempre la stessa formula.\n"
    "- NON usare un preambolo quando la risposta è diretta e immediata,\n"
    "  quando il chiamante sta solo confermando, correggendo o rifiutando,\n"
    "  o quando devi solo fare una domanda qualificante.\n"
    "- NON usare riempitivi vuoti come 'Allora...', 'Mmm, vediamo...',\n"
    "  'Ecco...', 'Un attimo, ci penso...': vai dritta all'azione.\n"
    "\n"
    "# Lunghezza delle risposte\n"
    "- Rispondi in modo breve: una o due frasi di contenuto. Prima di usare\n"
    "  uno strumento puoi anteporre un breve preambolo (vedi '# Preamboli');\n"
    "  non aggiungere invece riempitivi o esitazioni.\n"
    "- Fai UNA domanda alla volta e procedi al passo successivo solo dopo aver\n"
    "  ricevuto la risposta del chiamante.\n"
    "\n"
    "# Strumenti\n"
    "Usa solo gli strumenti effettivamente disponibili in questa sessione:\n"
    "search_listings, get_listing_by_address, mark_listing_interest,\n"
    "record_caller_info, leave_message, end_call. Non inventare, simulare o\n"
    "rinominare strumenti, e considera completata un'azione solo dopo che lo\n"
    "strumento ha risposto con successo.\n"
    "- get_listing_by_address e search_listings sono strumenti di sola\n"
    "  lettura: chiamali appena hai le informazioni necessarie (un indirizzo\n"
    "  per get_listing_by_address, i criteri di ricerca per search_listings),\n"
    "  senza chiedere conferma. Anteponi un breve preambolo.\n"
    "- mark_listing_interest: chiamalo subito, senza chiedere conferma, non\n"
    "  appena il chiamante conferma interesse per un immobile, passando il\n"
    "  suo indirizzo esatto.\n"
    "- record_caller_info: chiamalo una sola volta, dopo aver raccolto tutte\n"
    "  le risposte qualificanti e prima di dire che inoltrerai la richiesta\n"
    "  (vedi '# Quando inoltrare la richiesta a un agente').\n"
    "- leave_message: usalo per le richieste del TIPO C, per registrare nome\n"
    "  e messaggio del chiamante.\n"
    "- end_call: chiamalo solo per chiudere la chiamata, come descritto in\n"
    "  '# Come chiudere la chiamata'.\n"
    "- Se uno strumento di ricerca non restituisce nulla, segui la procedura\n"
    "  del tipo di chiamata in corso (TIPO A punto 6, TIPO B punto 3); non\n"
    "  inventare immobili o dati assenti dai risultati.\n"
    "\n"
    "# Flusso della conversazione — tipi di chiamata\n"
    "\n"
    "## TIPO A — Il chiamante chiede di un immobile specifico\n"
    "Riconosci questo tipo quando il chiamante menziona un indirizzo o\n"
    "un immobile specifico ('chiamo per l'appartamento in Via Roma...').\n"
    "Procedura:\n"
    "1. Prima di usare get_listing_by_address, assicurati di avere almeno\n"
    "   una via o un indirizzo specifico. Se il chiamante ha detto solo il\n"
    "   tipo di immobile (es. 'il quadrilocale') senza indirizzo, chiedigi\n"
    "   prima: 'Può darmi l'indirizzo o la via dell'immobile?'\n"
    "   Solo dopo aver ottenuto un indirizzo usa get_listing_by_address.\n"
    "2. Se trovato: usa subito mark_listing_interest con l'indirizzo esatto\n"
    "   dell'immobile, poi conferma che è disponibile e descrivi brevemente.\n"
    "3. PRIMA di fare domande, di' al chiamante che, per poter presentare\n"
    "   la sua richiesta all'agente immobiliare, hai bisogno di fargli\n"
    "   qualche domanda in più. Solo dopo questa frase di transizione\n"
    "   inizia con le domande qualificanti.\n"
    "4. Fai UNA domanda qualificante alla volta, in questo ordine.\n"
    "   Per AFFITTO chiedi:\n"
    "   - Situazione lavorativa (dipendente, autonomo, studente?)\n"
    "   - Reddito mensile netto approssimativo\n"
    "   - Numero di persone che abiterebbero nell'immobile\n"
    "   - Presenza di animali domestici\n"
    "   - Data di ingresso desiderata\n"
    "   Per VENDITA chiedi:\n"
    "   - Ha già un mutuo pre-approvato o sta trattando con una banca?\n"
    "   - Ha un immobile da vendere prima di acquistare?\n"
    "   - Tempistiche desiderate per il rogito\n"
    "   - Visita: quando sarebbe disponibile?\n"
    "5. Rispondi a qualsiasi domanda sul immobile usando i dati trovati.\n"
    "   Se non hai l'informazione, di' che chiederai all'agente.\n"
    "6. Se NON trovato: non arrenderti subito — chiedi al chiamante se può\n"
    "   fornire più dettagli sull'indirizzo o confermare la via. Solo se\n"
    "   dopo un secondo tentativo non trovi nulla, scusati e di' che\n"
    "   inoltrerai la richiesta a un agente immobiliare.\n"
    "\n"
    "## TIPO B — Il chiamante cerca senza un immobile specifico\n"
    "Procedura:\n"
    "1. Raccolta informazioni — fai UNA domanda alla volta:\n"
    "   - Acquisto (vendita) o affitto?\n"
    "   - Zona o città preferita?\n"
    "   - Numero di camere?\n"
    "   - Budget massimo?\n"
    "2. Usa search_listings con i parametri raccolti.\n"
    "3. Se nessun risultato: chiedi se vuole provare criteri diversi.\n"
    "4. Se trovi risultati: descrivili in modo naturale, come farebbe un\n"
    "   agente umano (non leggere tutti i campi), poi chiedi al chiamante\n"
    "   se uno di questi immobili lo interessa.\n"
    "5. Se risponde di sì: usa subito mark_listing_interest con l'indirizzo\n"
    "   esatto di quell'immobile. PRIMA di fare altre domande, di' al\n"
    "   chiamante che, per poter presentare la sua richiesta all'agente immobiliare,\n"
    "   hai bisogno di fargli qualche domanda in più. Solo dopo questa\n"
    "   frase di transizione inizia con le domande qualificanti (le stesse\n"
    "   del TIPO A, in base ad affitto o vendita).\n"
    "6. Se risponde di no: presenta il prossimo immobile tra i risultati\n"
    "   trovati, allo stesso modo. Continua finché non risponde di sì\n"
    "   (vai al punto 5) oppure finché non hai più immobili da proporre.\n"
    "7. Se finisci gli immobili senza che il chiamante ne scelga uno, di'\n"
    "   che al momento non avete nulla che soddisfi le sue esigenze.\n"
    "\n"
    "## TIPO C — Qualsiasi altra richiesta\n"
    "Se la richiesta del chiamante non riguarda la ricerca o l'acquisto\n"
    "di un immobile, gestiscila così:\n"
    "1. Ascolta con attenzione l'intera richiesta senza interrompere.\n"
    "2. Fai UNA domanda di chiarimento se necessario per capire bene.\n"
    "3. Chiedi il nome del chiamante se non lo conosci già.\n"
    "4. Usa leave_message per registrare nome e messaggio.\n"
    "5. Dopo che leave_message ha risposto con status 'saved', di':\n"
    "   'Ho preso nota. Un nostro agente la ricontatterà al più presto.\n"
    "    Può contare su di noi. Buona giornata!'\n"
    "6. Aspetta che il chiamante saluti e poi concludi naturalmente.\n"
    "Non tentare mai di rispondere a domande fuori dalla tua competenza.\n"
    "Non inventare procedure, prezzi, o informazioni legali/contrattuali.\n"
    "\n"
    "# Regole generali\n"
    "- Ricorda: vale sempre la REGOLA SULLA LINGUA (vedi '# Lingua'), anche\n"
    "  per le risposte dopo i risultati degli strumenti.\n"
    "- Aspetta SEMPRE che il chiamante finisca di parlare prima di rispondere.\n"
    "- Non terminare mai la chiamata di tua iniziativa, TRANNE nel caso\n"
    "  descritto sotto in '# Come chiudere la chiamata'.\n"
    "- Non inventare mai dati non presenti nei risultati degli strumenti.\n"
    "- Il campo 'text' contiene la descrizione completa dell'immobile. Usalo per\n"
    "  rispondere a domande specifiche del chiamante (piano, esposizione, condizioni,\n"
    "  riscaldamento, ecc.)\n"
    "- Non trasferire mai la chiamata.\n"
    "- Raccogli sempre il nome del chiamante.\n"
    "- NON anticipare mai i prossimi passi della conversazione (es. non dire\n"
    "  'dopo questa domanda ti dirò che...' o 'poi ti chiederò se...').\n"
    "  Fai solo la domanda o l'affermazione del momento presente, una alla\n"
    "  volta, e procedi silenziosamente al passo successivo solo dopo aver\n"
    "  ricevuto la risposta del chiamante.\n"
    "\n"
    "# Quando inoltrare la richiesta a un agente\n"
    "Subito dopo aver raccolto TUTTE le risposte alle domande qualificanti\n"
    "(incluso il nome del chiamante), e PRIMA di dire che inoltrerai la\n"
    "richiesta, chiama lo strumento record_caller_info passando tutti i\n"
    "dati raccolti durante la chiamata. Poi prosegui normalmente.\n"
    "Di' che inoltrerai la richiesta a un agente immobiliare SOLO nelle\n"
    "seguenti situazioni, e SOLO dopo aver raccolto tutte le informazioni\n"
    "qualificanti. Non dire MAI che l'agente lo ricontatterà o che lo farà\n"
    "in un determinato momento — non puoi saperlo. Di' semplicemente che\n"
    "girerai/inoltrerai la sua richiesta a un agente immobiliare.\n"
    "- TIPO A: hai confermato che l'immobile esiste E hai raccolto tutte le\n"
    "  domande qualificanti (situazione lavorativa, reddito, persone, animali,\n"
    "  data ingresso per affitto — oppure mutuo, immobile da vendere, tempistiche,\n"
    "  disponibilità visita per vendita).\n"
    "- TIPO B: hai trovato immobili corrispondenti E hai raccolto nome, budget,\n"
    "  zona e numero di camere dal chiamante.\n"
    "In tutti gli altri casi NON menzionare mai un agente.\n"
    "\n"
    "# Come chiudere la chiamata\n"
    "Subito dopo aver detto al chiamante che inoltrerai la sua richiesta a\n"
    "un agente immobiliare:\n"
    "1. Chiedi se può aiutarlo con qualcos'altro.\n"
    "2. Se dice di no: NON salutare a voce e non dire arrivederci — il saluto\n"
    "   di chiusura viene riprodotto automaticamente dal sistema. Chiama\n"
    "   semplicemente lo strumento end_call senza aggiungere altro.\n"
    "3. Se dice di sì: continua ad aiutarlo normalmente, e ripeti questa\n"
    "   procedura quando hai finito.\n"
)

_SYSTEM_PROMPT = _DEFAULT_FIRST_LINE + _SYSTEM_PROMPT_BODY

# Appended to the instructions only when we did NOT receive a usable caller
# number (e.g. a carrier forwarded the call and clobbered the caller ID with
# the tenant's own number, or the number was withheld). Tells Apollonia to ask
# the caller for a callback number and store it via the tool 'phone' field.
_ASK_FOR_NUMBER_INSTRUCTION = (
    "\n\n# Numero di telefono del chiamante — IMPORTANTE\n"
    "Non disponi del numero di telefono del chiamante: la chiamata è arrivata\n"
    "senza un numero richiamabile. Senza un numero, l'agente non può\n"
    "ricontattare il chiamante. Perciò, prima di chiudere la chiamata e prima\n"
    "di chiamare record_caller_info (oppure leave_message per il TIPO C),\n"
    "chiedi al chiamante il suo numero di telefono e ripetiglielo per\n"
    "conferma. Passa poi il numero nel campo 'phone' dello stesso strumento.\n"
    "Chiedi il numero una sola volta, in modo naturale; se il chiamante non\n"
    "vuole lasciarlo, prosegui comunque senza insistere.\n"
)


def _build_system_prompt(agency_name: str | None, agent_name: str | None) -> str:
    """Inject the tenant's agency/agent name into the first line of the
    prompt; everything else stays identical to the single-tenant version."""
    if not agency_name:
        return _SYSTEM_PROMPT
    name = agent_name or "Apollonia"
    return (
        "# Ruolo e obiettivo\n"
        f"Sei {name}, la receptionist virtuale di {agency_name}.\n"
        + _SYSTEM_PROMPT_BODY
    )

_SEARCH_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "search_listings",
    "description": "Search available real estate listings based on caller criteria",
    "parameters": {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["vendita", "affitto"],
                "description": "Whether the caller wants to buy (vendita) or rent (affitto)",
            },
            "zone": {
                "type": "string",
                "description": "Area or neighbourhood the caller is interested in",
            },
            "rooms_min": {
                "type": "integer",
                "description": "Minimum number of rooms",
            },
            "rooms_max": {
                "type": "integer",
                "description": "Maximum number of rooms",
            },
            "max_price": {
                "type": "integer",
                "description": "Maximum price in EUR",
            },
        },
        "required": [],
    },
}

_GET_LISTING_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "get_listing_by_address",
    "description": (
        "Look up a specific listing by address or partial address. "
        "Use this when the caller mentions a specific property or address. "
        "Returns the listing details if found, or empty list if not found."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "address_query": {
                "type": "string",
                "description": "The address exactly as spoken by the caller — copy it verbatim, do not paraphrase or reinterpret",
            }
        },
        "required": ["address_query"],
    },
}

_MARK_INTEREST_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "mark_listing_interest",
    "description": (
        "Record that the caller has confirmed interest in a specific listing. "
        "Call this as soon as the caller says they are interested in a "
        "particular property, passing its exact address."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "address": {
                "type": "string",
                "description": "The exact address of the listing the caller is interested in",
            }
        },
        "required": ["address"],
    },
}

_RECORD_CALLER_INFO_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "record_caller_info",
    "description": (
        "Record the caller's qualifying answers as structured data so they "
        "can be included in the lead summary sent to the agent. Call this "
        "once, right after you've collected all the qualifying answers for "
        "the current request (rental or purchase) — and before telling the "
        "caller you'll forward their request. Only include fields the "
        "caller actually answered."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Caller's name"},
            "phone": {
                "type": "string",
                "description": (
                    "Caller's callback phone number, exactly as the caller "
                    "spoke it. Only set this when you had to ask the caller for "
                    "their number because it wasn't available automatically."
                ),
            },
            "employment_status": {
                "type": "string",
                "description": "Employment situation (e.g. dipendente, autonomo, studente) — rentals",
            },
            "monthly_income": {
                "type": "string",
                "description": "Approximate net monthly income — rentals",
            },
            "household_size": {
                "type": "string",
                "description": "Number of people who would live in the property — rentals",
            },
            "has_pets": {
                "type": "string",
                "description": "Whether the caller has pets — rentals",
            },
            "move_in_date": {
                "type": "string",
                "description": "Desired move-in date — rentals",
            },
            "has_mortgage_preapproval": {
                "type": "string",
                "description": "Mortgage pre-approval / bank discussions status — purchases",
            },
            "has_property_to_sell": {
                "type": "string",
                "description": "Whether the caller has a property to sell before buying — purchases",
            },
            "sale_timeline": {
                "type": "string",
                "description": "Desired timeline for closing (rogito) — purchases",
            },
            "visit_availability": {
                "type": "string",
                "description": "When the caller is available for a viewing — purchases",
            },
        },
        "required": [],
    },
}


_END_CALL_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "end_call",
    "description": (
        "End the phone call. Use this ONLY after telling the caller you'll "
        "forward their request to a real estate agent, asking if there's "
        "anything else you can help with, and the caller says no. Do NOT say "
        "goodbye yourself first — a closing message is played automatically; "
        "just call this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

_LEAVE_MESSAGE_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "leave_message",
    "description": (
        "Use this when the caller's request does not involve searching "
        "for a property to buy or rent, and does not involve a specific "
        "listing inquiry. Saves the caller's name, phone, and their "
        "message so an agent can follow up."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "caller_name": {
                "type": "string",
                "description": "Full name of the caller",
            },
            "phone": {
                "type": "string",
                "description": (
                    "Caller's callback phone number, exactly as the caller "
                    "spoke it. Only set this when you had to ask the caller for "
                    "their number because it wasn't available automatically."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "A clear summary of what the caller needs, "
                    "written as if taking a note for the agent: "
                    "e.g. 'Il chiamante vuole una valutazione del "
                    "suo appartamento in Via Roma 5, Lodi. "
                    "Disponibile il mattino.'"
                ),
            },
            "urgency": {
                "type": "string",
                "enum": ["normale", "urgente"],
                "description": "Whether the caller indicated urgency",
            },
        },
        "required": ["caller_name", "message"],
    },
}

_SESSION_UPDATE: dict[str, Any] = {
    "type": "session.update",
    "session": {
        "type": "realtime",
        "model": "gpt-realtime-2",
        "instructions": _SYSTEM_PROMPT,
        "reasoning": {"effort": "low"},
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.6,
                    "prefix_padding_ms": 500,
                    "silence_duration_ms": 800,
                },
            },
            "output": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "voice": "marin",
            },
        },
        "tools": [
            _SEARCH_TOOL,
            _GET_LISTING_TOOL,
            _MARK_INTEREST_TOOL,
            _RECORD_CALLER_INFO_TOOL,
            _END_CALL_TOOL,
            _LEAVE_MESSAGE_TOOL,
        ],
        "tool_choice": "auto",
    },
}


def setup_twilio_webhook() -> None:
    """Set the voice webhook URL on the configured Twilio number. Runs at startup."""
    if not settings.TWILIO_ACCOUNT_SID or not settings.PUBLIC_BASE_URL:
        logger.info("Twilio credentials not set — skipping automatic webhook setup")
        return
    try:
        from twilio.rest import Client as TwilioClient

        client = TwilioClient(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        numbers = client.incoming_phone_numbers.list(
            phone_number=settings.TWILIO_PHONE_NUMBER
        )
        if not numbers:
            logger.error(
                "Twilio number %s not found in account", settings.TWILIO_PHONE_NUMBER
            )
            return
        webhook_url = f"{settings.PUBLIC_BASE_URL}/call/inbound"
        numbers[0].update(voice_url=webhook_url, voice_method="POST")
        logger.info("Twilio voice_url set to %s", webhook_url)
    except Exception as exc:
        logger.error("Failed to configure Twilio webhook: %s", exc)


def _start_call_recording(call_sid: str) -> None:
    """Start a dual-channel recording of the call via the Twilio REST API.

    <Connect><Stream> doesn't support a record="true" attribute like <Dial>
    does, so recording is started out-of-band on the Call resource — it runs
    in parallel and doesn't affect the media stream.
    """
    try:
        from twilio.rest import Client as TwilioClient

        client = TwilioClient(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        client.calls(call_sid).recordings.create(
            recording_status_callback=f"{settings.PUBLIC_BASE_URL}/call/recording-status",
            recording_status_callback_method="POST",
        )
        logger.info("Started recording for call_sid=%s", call_sid)
    except Exception as exc:
        logger.error("Failed to start call recording for %s: %s", call_sid, exc)


def _hangup_call(call_sid: str) -> None:
    """Hang up the call via the Twilio REST API.

    Closing the media-stream WebSocket on its own does not reliably end the
    call — it can leave the caller listening to dead air — so the call is
    terminated explicitly here.
    """
    if not call_sid or not settings.TWILIO_ACCOUNT_SID:
        logger.warning(
            "Cannot hang up via REST: call_sid=%r account_sid_set=%s",
            call_sid,
            bool(settings.TWILIO_ACCOUNT_SID),
        )
        return
    try:
        from twilio.rest import Client as TwilioClient

        client = TwilioClient(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        client.calls(call_sid).update(status="completed")
        logger.info("Hung up call_sid=%s", call_sid)
    except Exception as exc:
        logger.error("Failed to hang up call %s: %s", call_sid, exc)


@router.post("/inbound")
async def inbound_call(request: Request) -> Response:
    """
    Twilio calls this when a call arrives. Returns TwiML that immediately opens
    a bidirectional Media Stream WebSocket back to this server.
    """
    form = await request.form()
    caller = str(form.get("From", "sconosciuto"))
    called = str(form.get("To", ""))
    call_sid = str(form.get("CallSid", ""))
    # When a tenant's carrier forwards their real number to our Twilio number,
    # ForwardedFrom should carry the tenant's number while From stays the
    # original caller. If a carrier instead overwrites From with the tenant's
    # number, every lead would be logged with the tenant's number — this log
    # lets us detect that per carrier before it corrupts lead capture.
    forwarded_from = str(form.get("ForwardedFrom", ""))
    logger.info(
        "Inbound call webhook hit — caller=%s called=%s forwarded_from=%s",
        caller,
        called,
        forwarded_from or "(none)",
    )

    tenant = db.get_by_twilio_number(called) if called else None
    if tenant is None and called and called != settings.TWILIO_PHONE_NUMBER:
        # Unknown number and not the env-var demo number: reject.
        logger.warning("No tenant found for called number %s — rejecting", called)
        response = VoiceResponse()
        response.say("Numero non attivo.", language="it-IT")
        response.hangup()
        return Response(content=str(response), media_type="text/xml")

    if call_sid and settings.TWILIO_ACCOUNT_SID:
        asyncio.create_task(asyncio.to_thread(_start_call_recording, call_sid))

    wss_base = settings.PUBLIC_BASE_URL.replace("https://", "wss://").replace(
        "http://", "ws://"
    )

    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=f"{wss_base}/call/stream", track="inbound_track")
    stream.parameter(name="caller", value=caller)
    # Pass the call SID explicitly — the stream "start" event's callSid is not
    # always reliable, and we need it to hang up the call via the REST API.
    if call_sid:
        stream.parameter(name="call_sid", value=call_sid)
    if tenant is not None:
        stream.parameter(name="tenant_id", value=tenant["id"])
    connect.append(stream)
    response.append(connect)

    return Response(content=str(response), media_type="text/xml")


@router.post("/recording-status")
async def recording_status(request: Request) -> Response:
    """Twilio calls this when a call recording's status changes (e.g. completed)."""
    form = await request.form()
    logger.info(
        "Recording status callback — call_sid=%s recording_sid=%s status=%s url=%s",
        form.get("CallSid"),
        form.get("RecordingSid"),
        form.get("RecordingStatus"),
        form.get("RecordingUrl"),
    )
    return Response(status_code=204)


# Cheap text model used for the post-call one-sentence lead summary. The
# realtime model handles the live conversation; this is a separate, non-audio
# call made once the call has ended, so latency is not a concern.
_SUMMARY_MODEL = "gpt-5.4-nano"


def _fallback_lead_summary(session: dict[str, Any]) -> str:
    """Deterministic one-sentence summary, used when the text model is
    unavailable (no API key) or the request fails."""
    caller = session.get("caller_number", "sconosciuto")
    caller_name = (
        (session.get("caller_info") or {}).get("name")
        or (session.get("left_message") or {}).get("caller_name")
    )
    who = caller_name or caller
    n_interested = len(session.get("interested_listings") or [])
    if n_interested:
        return (
            f"{who} ha chiamato ed è interessato a "
            f"{n_interested} immobil{'e' if n_interested == 1 else 'i'}."
        )
    if session.get("left_message") is not None:
        return f"{who} ha lasciato un messaggio in segreteria."
    if session.get("listings_shown"):
        return (
            f"{who} ha chiamato e ha visto alcuni immobili, "
            "senza indicarne uno di interesse."
        )
    return f"{who} ha chiamato."


def _extract_response_text(data: dict[str, Any]) -> str:
    """Pull the assistant text out of a Responses API payload. Prefers the
    top-level `output_text` convenience field, falling back to walking the
    `output` array (which also contains non-text items like reasoning)."""
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


async def _generate_lead_summary(detail_body: str, session: dict[str, Any]) -> str:
    """Ask a text model to write a one-sentence Italian summary of the call so
    the agent immediately understands what the email is about. Falls back to a
    deterministic template if the API key is missing or the request fails."""
    if not settings.OPENAI_API_KEY:
        return _fallback_lead_summary(session)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                json={
                    "model": _SUMMARY_MODEL,
                    "instructions": (
                        "Sei l'assistente di un'agenzia immobiliare. "
                        "Riassumi in UNA sola frase, in italiano, l'esito "
                        "della telefonata descritta dall'utente, in modo "
                        "che l'agente capisca subito di cosa si tratta "
                        "(chi ha chiamato e cosa vuole). Scrivi solo la "
                        "frase, senza preamboli, virgolette o elenchi."
                    ),
                    "input": detail_body,
                    # nano on a simple summarisation task: lightest supported
                    # reasoning + terse output. max_output_tokens covers
                    # reasoning + text. Notes on this model's quirks:
                    #   - reasoning.effort: none/low/medium/high/xhigh (no "minimal")
                    #   - temperature is NOT supported on the Responses API
                    "reasoning": {"effort": "low"},
                    "text": {"verbosity": "low"},
                    "max_output_tokens": 400,
                },
            )
            if response.status_code >= 400:
                # Surface OpenAI's actual error body — a bare status code
                # doesn't tell us which field it's rejecting.
                logger.error(
                    "Lead summary request failed: %s — %s",
                    response.status_code,
                    response.text,
                )
                return _fallback_lead_summary(session)
            summary = _extract_response_text(response.json()).strip()
            if summary:
                return summary
    except Exception as exc:
        logger.error("Failed to generate lead summary via LLM: %s", exc)
    return _fallback_lead_summary(session)


async def _send_lead_email(session: dict[str, Any]) -> None:
    recipient = session.get("lead_email") or settings.LEAD_EMAIL
    if not settings.RESEND_API_KEY or not recipient:
        logger.warning("RESEND_API_KEY/lead email not configured — lead email skipped")
        return

    # Work out a usable callback number. If the auto-detected caller ID is
    # usable, use it; otherwise fall back to a number the caller spoke aloud
    # (captured into caller_info/left_message via the tools' 'phone' field).
    # A lead with no callback number at all is useless to the agent — skip it.
    spoken = (
        (session.get("caller_info") or {}).get("phone")
        or (session.get("left_message") or {}).get("phone")
    )
    if session.get("caller_number_known"):
        caller = session.get("caller_number", "sconosciuto")
    else:
        caller = spoken
    if not caller:
        logger.info(
            "Lead suppressed — no usable caller number (caller ID was the "
            "tenant's own number / withheld and the caller left none)"
        )
        return
    # Make the resolved number the one every downstream step (summary, body,
    # subject) sees.
    session["caller_number"] = caller

    try:
        lines: list[str] = [
            f"Chiamante: {caller}",
            "",
        ]

        lines += ["=== Dati raccolti dal chiamante ==="]
        caller_info = session.get("caller_info") or {}
        if caller_info:
            for key, label in _CALLER_INFO_LABELS.items():
                if caller_info.get(key):
                    lines.append(f"{label}: {caller_info[key]}")
        else:
            lines.append("Nessun dato raccolto.")
        lines.append("")

        lines += ["=== Immobile di interesse ==="]
        if session["interested_listings"]:
            for listing in session["interested_listings"]:
                lines.append(_format_listing_brief(listing))
        else:
            lines.append("Nessuno specificato dal chiamante.")
            lines.append("")

        others = [
            listing for listing in session["listings_shown"]
            if listing not in session["interested_listings"]
        ]
        lines += ["", "=== Altri immobili presentati ==="]
        if others:
            for listing in others:
                lines.append(_format_listing_brief(listing))
        else:
            lines.append("Nessuno.")

        if session.get("left_message"):
            msg_data = session["left_message"]
            lines += ["", "=== Messaggio lasciato ==="]
            lines.append(f"Nome: {msg_data.get('caller_name', 'sconosciuto')}")
            lines.append(f"Urgenza: {msg_data.get('urgency', 'normale')}")
            lines.append(f"Messaggio: {msg_data.get('message', '')}")

        detail_body = "\n".join(lines)
    except Exception as exc:
        logger.error("Failed to format lead email body: %s", exc)
        detail_body = (
            f"Chiamante: {caller}\n"
            f"(errore nella formattazione del corpo della mail — controlla i log)"
        )

    # Let a text model write a one-sentence summary of the call so the agent
    # grasps the lead at a glance, then prepend it to the detailed body.
    summary = await _generate_lead_summary(detail_body, session)
    body = f"{summary}\n\n{detail_body}"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
                json={
                    "from": settings.RESEND_FROM,
                    "to": [recipient],
                    "subject": (
                        f"Nuovo lead — {caller}"
                        if session.get("listings_shown")
                        else f"Nuovo messaggio — {caller}"
                        if session.get("left_message") is not None
                        else f"Chiamata — {caller}"
                    ),
                    "text": body,
                },
            )
            response.raise_for_status()
        logger.info("Lead email sent for caller %s", caller)
    except Exception as exc:
        logger.error("Failed to send lead email: %s", exc)


@router.websocket("/stream")
async def stream_ws(websocket: WebSocket) -> None:
    """
    Bidirectional audio bridge between Twilio Media Streams and OpenAI Realtime API.
    Two concurrent tasks run for the lifetime of the call:
      - twilio_to_openai: forwards inbound mulaw audio to OpenAI
      - openai_to_twilio: forwards OpenAI audio deltas back to Twilio
    Whichever task exits first causes the other to be cancelled, then the lead
    summary email is sent in the finally block.
    """
    await websocket.accept()
    logger.info("Twilio media stream WebSocket connected")

    session: dict[str, Any] = {
        "stream_sid": None,
        "call_sid": None,
        "caller_number": "sconosciuto",
        "caller_number_known": False,
        "lead_email": None,
        "listings_shown": [],
        "interested_listings": [],
        "caller_info": {},
        "left_message": None,
        "suppress_input_until": 0.0,
        "last_speech_at": 0.0,
        "goodbye_played": asyncio.Event(),
    }

    # Twilio sends "connected" then "start" as the first frames. Read them
    # before opening the OpenAI session so we know which tenant is being
    # called and can inject its agency name into the instructions.
    tenant_id = ""
    try:
        while True:
            msg = json.loads(await websocket.receive_text())
            event = msg.get("event")
            if event == "start":
                start = msg.get("start", {})
                params = start.get("customParameters", {})
                session["stream_sid"] = msg.get("streamSid") or start.get("streamSid")
                session["call_sid"] = params.get("call_sid") or start.get("callSid")
                session["caller_number"] = params.get("caller", session["caller_number"])
                tenant_id = params.get("tenant_id", "")
                logger.info(
                    "Stream started sid=%s call_sid=%s caller=%s tenant=%s",
                    session["stream_sid"],
                    session["call_sid"],
                    session["caller_number"],
                    tenant_id or "(env fallback)",
                )
                break
            if event == "stop":
                logger.info("Stream stopped before start event")
                return
    except WebSocketDisconnect:
        logger.info("Twilio WebSocket disconnected before start event")
        return

    tenant = db.get_by_id(tenant_id) if tenant_id else None
    if tenant is not None:
        instructions = _build_system_prompt(tenant["agency_name"], tenant["agent_name"])
        tenant_store = tenant_stores.get_or_create(tenant["id"])
        session["lead_email"] = tenant.get("lead_email") or settings.LEAD_EMAIL
    else:
        # Env-var fallback: demo behaviour, global store, owner's lead email.
        instructions = _SYSTEM_PROMPT
        tenant_store = store
        session["lead_email"] = settings.LEAD_EMAIL

    # Decide whether we actually have a usable caller number. When a tenant's
    # carrier clobbers the caller ID on forwarding, From arrives as the tenant's
    # OWN number (real_number) — useless as a callback. Same for a withheld
    # number. In that case tell Apollonia to ask the caller for one; if she
    # still doesn't get it, _send_lead_email suppresses the (useless) lead.
    raw_caller = session["caller_number"]
    tenant_real = tenant.get("real_number") if tenant else None
    session["caller_number_known"] = (
        raw_caller not in ("", "sconosciuto")
        and not _same_number(raw_caller, tenant_real)
    )
    if not session["caller_number_known"]:
        instructions = instructions + _ASK_FOR_NUMBER_INSTRUCTION
        logger.info(
            "Caller number not usable (caller=%s real_number=%s) — "
            "Apollonia will ask the caller for one",
            raw_caller,
            tenant_real,
        )

    session_update = json.loads(json.dumps(_SESSION_UPDATE))
    session_update["session"]["instructions"] = instructions

    oai_headers = [
        ("Authorization", f"Bearer {settings.OPENAI_API_KEY}"),
    ]

    try:
        async with websockets.connect(
            "wss://api.openai.com/v1/realtime?model=gpt-realtime-2",
            additional_headers=oai_headers,
        ) as oai_ws:
            await oai_ws.send(json.dumps(session_update))
            logger.info("OpenAI Realtime session initialised")

            # Wait for OpenAI to confirm the session is ready before greeting.
            # session.updated is the ack for session.update; it arrives before
            # any audio tasks are running so we can read from oai_ws directly.
            async for raw in oai_ws:
                evt = json.loads(raw)
                etype = evt.get("type")
                if etype == "session.updated":
                    logger.info("OpenAI session ready")
                    break
                elif etype == "error":
                    logger.error("OpenAI startup error: %s", evt)
                else:
                    logger.info("OpenAI startup event: %s", etype)

            if _GREETING_AUDIO:
                # Inject prerecorded greeting as an assistant turn so OpenAI
                # knows the greeting was said without generating its own audio.
                await oai_ws.send(json.dumps({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": _GREETING_TEXT}],
                    },
                }))
                logger.info("Prerecorded greeting injected into context")
                if session["stream_sid"]:
                    await websocket.send_text(json.dumps({
                        "event": "media",
                        "streamSid": session["stream_sid"],
                        "media": {
                            "track": "outbound",
                            "payload": _GREETING_AUDIO,
                        },
                    }))
                    session["suppress_input_until"] = (
                        asyncio.get_event_loop().time()
                        + _GREETING_DURATION + 0.5
                    )
            else:
                # Fallback: let OpenAI generate the greeting.
                await oai_ws.send(json.dumps({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "Il telefono ha squillato e hai risposto. "
                                    "Saluta il chiamante e chiedi come puoi aiutarlo."
                                ),
                            }
                        ],
                    },
                }))
                await oai_ws.send(json.dumps({"type": "response.create"}))
                logger.info("Greeting triggered via OpenAI")

            async def twilio_to_openai() -> None:
                upsample_state = None
                async for raw in websocket.iter_text():
                    msg = json.loads(raw)
                    event = msg.get("event")

                    if event == "media":
                        if asyncio.get_event_loop().time() < session["suppress_input_until"]:
                            continue
                        mulaw = base64.b64decode(msg["media"]["payload"])
                        pcm16 = audioop.ulaw2lin(mulaw, 2)
                        pcm24k, upsample_state = audioop.ratecv(
                            pcm16, 2, 1, 8000, 24000, upsample_state
                        )
                        await oai_ws.send(
                            json.dumps(
                                {
                                    "type": "input_audio_buffer.append",
                                    "audio": base64.b64encode(pcm24k).decode(),
                                }
                            )
                        )

                    elif event == "mark":
                        # Twilio echoes our "goodbye" mark once that audio has
                        # finished playing out to the caller — that's our cue to
                        # hang up immediately.
                        if msg.get("mark", {}).get("name") == "goodbye":
                            session["goodbye_played"].set()

                    elif event == "stop":
                        logger.info(
                            "Stream stop received sid=%s", session["stream_sid"]
                        )
                        break

            async def openai_to_twilio() -> None:
                downsample_state = None
                async for raw in oai_ws:
                    msg = json.loads(raw)
                    etype = msg.get("type")
                    if etype == "response.output_audio.delta":
                        if session["stream_sid"]:
                            pcm24k = base64.b64decode(msg["delta"])
                            pcm8k, downsample_state = audioop.ratecv(
                                pcm24k, 2, 1, 24000, 8000, downsample_state
                            )
                            mulaw = audioop.lin2ulaw(pcm8k, 2)
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "event": "media",
                                        "streamSid": session["stream_sid"],
                                        "media": {
                                            "track": "outbound",
                                            "payload": base64.b64encode(mulaw).decode(),
                                        },
                                    }
                                )
                            )

                    elif etype == "response.output_audio_transcript.done":
                        session["last_speech_at"] = asyncio.get_event_loop().time()
                        text = msg.get("transcript", "").strip()
                        if text:
                            logger.info("Apollonia: %s", text)

                    elif etype == "response.function_call_arguments.done":
                        if msg.get("name") == "search_listings":
                            call_id = msg.get("call_id")
                            try:
                                args = json.loads(msg.get("arguments", "{}"))
                            except json.JSONDecodeError:
                                args = {}
                            results = tenant_store.search(**args)
                            session["listings_shown"].extend(results)
                            logger.info(
                                "search_listings(%s) → %d results", args, len(results)
                            )
                            await oai_ws.send(
                                json.dumps(
                                    {
                                        "type": "conversation.item.create",
                                        "item": {
                                            "type": "function_call_output",
                                            "call_id": call_id,
                                            "output": json.dumps(
                                                results, ensure_ascii=False
                                            ),
                                        },
                                    }
                                )
                            )
                            await oai_ws.send(json.dumps({"type": "response.create"}))

                        elif msg.get("name") == "get_listing_by_address":
                            call_id = msg.get("call_id")
                            try:
                                args = json.loads(msg.get("arguments", "{}"))
                            except json.JSONDecodeError:
                                args = {}
                            results = tenant_store.get_by_address(args.get("address_query", ""))
                            session["listings_shown"].extend(results)
                            logger.info(
                                "get_listing_by_address(%s) → %d results", args, len(results)
                            )
                            await oai_ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id,
                                    "output": json.dumps(results, ensure_ascii=False),
                                },
                            }))
                            await oai_ws.send(json.dumps({"type": "response.create"}))

                        elif msg.get("name") == "mark_listing_interest":
                            call_id = msg.get("call_id")
                            try:
                                args = json.loads(msg.get("arguments", "{}"))
                            except json.JSONDecodeError:
                                args = {}
                            address = args.get("address", "")
                            match = next(
                                (l for l in session["listings_shown"] if l["address"] == address),
                                None,
                            )
                            if match and match not in session["interested_listings"]:
                                session["interested_listings"].append(match)
                            logger.info("Caller interested in: %s (found=%s)", address, bool(match))
                            await oai_ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id,
                                    "output": json.dumps({"recorded": bool(match)}),
                                },
                            }))
                            await oai_ws.send(json.dumps({"type": "response.create"}))

                        elif msg.get("name") == "record_caller_info":
                            call_id = msg.get("call_id")
                            try:
                                args = json.loads(msg.get("arguments", "{}"))
                            except json.JSONDecodeError:
                                args = {}
                            session["caller_info"].update(
                                {k: v for k, v in args.items() if v}
                            )
                            logger.info("Recorded caller info: %s", args)
                            await oai_ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id,
                                    "output": json.dumps({"recorded": True}),
                                },
                            }))
                            await oai_ws.send(json.dumps({"type": "response.create"}))


                        elif msg.get("name") == "leave_message":
                            call_id = msg.get("call_id")
                            try:
                                args = json.loads(msg.get("arguments", "{}"))
                            except json.JSONDecodeError:
                                args = {}
                            session["left_message"] = args
                            logger.info("leave_message: %s", args)
                            await oai_ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id,
                                    "output": json.dumps({"status": "saved"}, ensure_ascii=False),
                                },
                            }))
                            await oai_ws.send(json.dumps({"type": "response.create"}))

                        elif msg.get("name") == "end_call":
                            call_id = msg.get("call_id")
                            logger.info(
                                "Apollonia ending call sid=%s", session["stream_sid"]
                            )
                            await oai_ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id,
                                    "output": json.dumps({"ended": True}),
                                },
                            }))
                            if _GOODBYE_AUDIO and session["stream_sid"]:
                                await websocket.send_text(json.dumps({
                                    "event": "media",
                                    "streamSid": session["stream_sid"],
                                    "media": {
                                        "track": "outbound",
                                        "payload": _GOODBYE_AUDIO,
                                    },
                                }))
                                # Mark the end of the goodbye so Twilio tells us
                                # the instant it finishes playing; hang up then
                                # instead of guessing with a fixed delay.
                                await websocket.send_text(json.dumps({
                                    "event": "mark",
                                    "streamSid": session["stream_sid"],
                                    "mark": {"name": "goodbye"},
                                }))
                                try:
                                    await asyncio.wait_for(
                                        session["goodbye_played"].wait(),
                                        timeout=_GOODBYE_DURATION + 2.0,
                                    )
                                except asyncio.TimeoutError:
                                    logger.warning(
                                        "Goodbye mark not received — hanging up anyway"
                                    )
                            else:
                                await asyncio.sleep(1.5)
                            await asyncio.to_thread(_hangup_call, session["call_sid"])
                            await websocket.close()
                            return

                    elif etype == "input_audio_buffer.speech_started":
                        session["last_speech_at"] = asyncio.get_event_loop().time()
                        logger.info("Caller speaking")

                    elif etype == "error":
                        logger.error("OpenAI Realtime error: %s", msg)

            session["last_speech_at"] = asyncio.get_event_loop().time()

            async def silence_watchdog() -> None:
                while True:
                    await asyncio.sleep(1)
                    if asyncio.get_event_loop().time() - session["last_speech_at"] > 100:
                        logger.info("100s silence — hanging up sid=%s", session["stream_sid"])
                        await asyncio.to_thread(_hangup_call, session["call_sid"])
                        await websocket.close()
                        break

            t1 = asyncio.create_task(twilio_to_openai())
            t2 = asyncio.create_task(openai_to_twilio())
            t3 = asyncio.create_task(silence_watchdog())
            _done, pending = await asyncio.wait(
                [t1, t2, t3], return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    except WebSocketDisconnect:
        logger.info(
            "Twilio WebSocket disconnected sid=%s", session.get("stream_sid")
        )
    except Exception as exc:
        logger.exception("Unhandled error in stream_ws: %s", exc)
    finally:
        await _send_lead_email(session)

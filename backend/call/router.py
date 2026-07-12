import asyncio
import base64
import datetime
import hashlib
import hmac
import json
import logging
import re
import time
from typing import Any

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from call import locales
from calls import db as calls_db
from config import settings
from listings.store import store, tenant_stores
from tenants import db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/call")

_GREETING_TEXT = "Buongiorno, sono Apollonia. Come posso aiutarla?"


def _format_listing_brief(content: dict[str, Any], listing: dict[str, Any]) -> str:
    raw_type = listing.get("type", "?")
    ltype = content["type_display"].get(raw_type, raw_type)
    return (
        f"{listing.get('address', '?')} — {listing.get('zone', '?')} — "
        f"{ltype} — {listing.get('rooms', '?')} {content['brief_rooms']} — "
        f"{listing.get('size_sqm', '?')}{content['brief_area']} — €{listing.get('price', '?')}"
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
    "- Il preambolo non è tutto il tuo turno: appena lo strumento restituisce\n"
    "  il risultato, prosegui SUBITO e di' il contenuto vero (es. la conferma\n"
    "  o la domanda successiva). Dopo un preambolo non restare mai in silenzio\n"
    "  in attesa che il chiamante parli.\n"
    "- Non usare un preambolo prima di mark_listing_interest e di end_call.\n"
    "  Il saluto di chiusura non è un preambolo: pronuncia direttamente le\n"
    "  parole del saluto, non annunciarlo.\n"
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
    "un immobile specifico della vostra offerta ('chiamo per l'appartamento\n"
    "in Via Roma...'). Se invece il chiamante vuole vendere un immobile di\n"
    "sua proprietà, è il TIPO D.\n"
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
    "4. Fai UNA domanda qualificante alla volta, in questo ordine. Come\n"
    "   primissima cosa chiedi sempre il nome del chiamante, se non lo\n"
    "   conosci già ('Come si chiama, per favore?'), e aspetta la risposta.\n"
    "   Poi prosegui a seconda che si tratti di affitto o vendita.\n"
    "   Per AFFITTO chiedi:\n"
    "   - Situazione lavorativa (dipendente, autonomo, studente?)\n"
    "   - Reddito mensile netto approssimativo\n"
    "   - Numero di persone che abiterebbero nell'immobile\n"
    "   - Presenza di animali domestici\n"
    "   - Data di ingresso desiderata\n"
    "   - Visita: quando sarebbe disponibile?\n"
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
    "5. Dopo che leave_message ha risposto con status 'saved', di' al\n"
    "   chiamante che hai preso nota e che passerai il messaggio all'agente.\n"
    "   NON promettere che l'agente richiamerà o ricontatterà il chiamante:\n"
    "   se e quando farlo lo decide l'agente.\n"
    "6. Poi chiudi la chiamata secondo '# Come chiudere la chiamata': chiedi\n"
    "   se puoi aiutarlo con altro e, se no, pronuncia le parole di saluto\n"
    "   (senza annunciarle) e chiama lo strumento end_call per riagganciare\n"
    "   davvero.\n"
    "Non tentare mai di rispondere a domande fuori dalla tua competenza.\n"
    "Non inventare procedure, prezzi, o informazioni legali/contrattuali.\n"
    "\n"
    "## TIPO D — Il chiamante vuole vendere il proprio immobile\n"
    "Riconosci questo tipo quando il chiamante vuole che l'agenzia venda un\n"
    "immobile di sua proprietà ('voglio vendere il mio appartamento', 'ho una\n"
    "casa da mettere in vendita'). ATTENZIONE: non è lo stesso di chi è\n"
    "interessato ad acquistare un immobile in offerta (quello è TIPO A o B).\n"
    "Qui NON fare le domande qualificanti e NON usare gli strumenti di\n"
    "ricerca. La procedura è breve, non sovraccaricare il chiamante:\n"
    "1. Chiedi il nome del chiamante, se non lo conosci già.\n"
    "2. Se non l'ha già detto, chiedi brevemente cosa e dove vuole vendere\n"
    "   (tipo di immobile e zona) — al massimo una domanda, senza insistere.\n"
    "3. Chiedi quando sarebbe disponibile per un incontro con l'agente.\n"
    "4. Usa leave_message: nel campo 'message' scrivi che il chiamante vuole\n"
    "   vendere il proprio immobile, di che tipo e dove, e quando è\n"
    "   disponibile per l'incontro.\n"
    "5. Dopo che leave_message ha risposto con status 'saved', di' al\n"
    "   chiamante che inoltrerai la sua richiesta a un agente immobiliare e\n"
    "   che un agente lo contatterà a breve. Questo è l'UNICO tipo di\n"
    "   chiamata in cui puoi promettere che l'agente si farà vivo.\n"
    "6. Poi prosegui secondo '# Come chiudere la chiamata' (chiedi se puoi\n"
    "   aiutarlo con altro e concludi la chiamata).\n"
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
    "dati raccolti durante la chiamata. Il nome devi averlo già raccolto\n"
    "prima; se non ce l'hai ancora, chiedilo come domanda a sé e aspetta la\n"
    "risposta prima di chiamare record_caller_info — non chiedere mai il nome\n"
    "nella stessa frase in cui dici che inoltrerai la richiesta. Appena\n"
    "record_caller_info restituisce\n"
    "il risultato, nello stesso turno — senza aspettare che il chiamante\n"
    "parli — di' al chiamante che inoltrerai la sua richiesta a un agente\n"
    "immobiliare e chiedigli se puoi aiutarlo con qualcos'altro.\n"
    "Di' che inoltrerai la richiesta a un agente immobiliare SOLO nelle\n"
    "seguenti situazioni, e SOLO dopo aver raccolto tutte le informazioni\n"
    "qualificanti. Nel TIPO A e B non dire MAI che l'agente lo ricontatterà\n"
    "o che lo farà in un determinato momento — non puoi saperlo; di'\n"
    "semplicemente che girerai/inoltrerai la sua richiesta a un agente\n"
    "immobiliare. L'unica eccezione è il TIPO D (vendita di un immobile\n"
    "proprio), dove puoi promettere il contatto da parte dell'agente.\n"
    "- TIPO A: hai confermato che l'immobile esiste E hai raccolto tutte le\n"
    "  domande qualificanti (situazione lavorativa, reddito, persone, animali,\n"
    "  data ingresso e disponibilità visita per affitto — oppure mutuo, immobile\n"
    "  da vendere, tempistiche, disponibilità visita per vendita).\n"
    "- TIPO B: hai trovato immobili corrispondenti E hai raccolto nome, budget,\n"
    "  zona e numero di camere dal chiamante.\n"
    "Negli altri casi non menzionare l'agente come per un lead qualificato —\n"
    "il TIPO C e il TIPO D seguono la propria procedura qui sopra.\n"
    "\n"
    "# Come chiudere la chiamata\n"
    "Subito dopo aver detto al chiamante che inoltrerai la sua richiesta a\n"
    "un agente immobiliare:\n"
    "1. Chiedi se può aiutarlo con qualcos'altro.\n"
    "2. Se dice di no: pronuncia le vere parole di saluto che il chiamante\n"
    "   deve sentire (es. 'Grazie della chiamata, buona giornata,\n"
    "   arrivederci.'). NON annunciare il saluto né la chiusura ('la saluto',\n"
    "   'le dico arrivederci', 'ora riaggancio', 'chiudo la chiamata'): di'\n"
    "   subito quelle parole. Subito dopo il saluto chiama lo strumento\n"
    "   end_call e non aggiungere altro.\n"
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


# Italian baseline content (the original single-tenant strings, unchanged) keyed
# alongside the Slovak content from call/locales.py. Both dicts share the same
# keys; a tenant's `locale` column selects which one the phone agent uses.
_IT_CONTENT: dict[str, Any] = {
    "first_line_default": _DEFAULT_FIRST_LINE,
    "first_line_template": (
        "# Ruolo e obiettivo\nSei {name}, la receptionist virtuale di {agency}.\n"
    ),
    "system_prompt_body": _SYSTEM_PROMPT_BODY,
    "ask_for_number": _ASK_FOR_NUMBER_INSTRUCTION,
    "greeting_text": _GREETING_TEXT,
    "greeting_prompt": (
        "Il telefono ha squillato e hai risposto. "
        "Saluta il chiamante e chiedi come puoi aiutarlo."
    ),
    "caller_info_labels": _CALLER_INFO_LABELS,
    "summary_instruction": (
        "Sei l'assistente di un'agenzia immobiliare. "
        "Riassumi in UNA sola frase, in italiano, l'esito "
        "della telefonata descritta dall'utente, in modo "
        "che l'agente capisca subito di cosa si tratta "
        "(chi ha chiamato e cosa vuole). Indica il chiamante "
        "con il suo nome, se presente nella descrizione; se "
        "manca, usa la parola 'Il chiamante'. NON inserire il "
        "numero di telefono nella frase (è riportato altrove). "
        "Scrivi solo la frase, senza preamboli, virgolette o elenchi."
    ),
    "unknown_caller": "sconosciuto",
    "email_caller_label": "Chiamante",
    "email_section_collected": "=== Dati raccolti dal chiamante ===",
    "email_no_data": "Nessun dato raccolto.",
    "email_section_interested": "=== Immobile di interesse ===",
    "email_none_specified": "Nessuno specificato dal chiamante.",
    "email_section_others": "=== Altri immobili presentati ===",
    "email_none": "Nessuno.",
    "email_section_message": "=== Messaggio lasciato ===",
    "email_name_label": "Nome",
    "email_urgency_label": "Urgenza",
    "email_urgency_default": "normale",
    "email_message_label": "Messaggio",
    "email_format_error": (
        "(errore nella formattazione del corpo della mail — controlla i log)"
    ),
    "email_subject_lead": "Nuovo lead — {caller}",
    "email_subject_message": "Nuovo messaggio — {caller}",
    "email_subject_call": "Chiamata — {caller}",
    "urgency_display": {"normale": "normale", "urgente": "urgente"},
    "brief_rooms": "locali",
    "brief_area": "mq",
    "type_display": {"vendita": "vendita", "affitto": "affitto"},
    "listing_noun": (lambda n: "immobile" if n == 1 else "immobili"),
    "summary_interested": "{who} ha chiamato ed è interessato a {n} {noun}.",
    "summary_message": "{who} ha lasciato un messaggio in segreteria.",
    "summary_shown": (
        "{who} ha chiamato e ha visto alcuni immobili, "
        "senza indicarne uno di interesse."
    ),
    "summary_plain": "{who} ha chiamato.",
}

_CONTENT: dict[str, dict[str, Any]] = {"it": _IT_CONTENT, "sk": locales.SK}


def _content(locale: str | None) -> dict[str, Any]:
    """Per-locale agent content, defaulting to Italian for unknown locales."""
    return _CONTENT.get(locale or "it", _IT_CONTENT)


def _build_system_prompt(
    content: dict[str, Any], agency_name: str | None, agent_name: str | None
) -> str:
    """Build the system prompt for a locale: inject the tenant's agency/agent
    name into the first line; the body is the locale's full instruction set."""
    body = content["system_prompt_body"]
    if not agency_name:
        return content["first_line_default"] + body
    name = agent_name or "Apollonia"
    return content["first_line_template"].format(name=name, agency=agency_name) + body

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
        "caller actually answered; always make sure you have asked the caller "
        "for their name before calling this. After this tool returns, immediately tell "
        "the caller you'll forward their request to an agent — don't wait for "
        "the caller to speak first."
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
        "anything else you can help with, and the caller says no. Say the "
        "actual goodbye words to the caller first (a real farewell like "
        "'Thank you for calling, have a nice day, goodbye') — not a statement "
        "that you're about to say goodbye — then call this tool to hang up."
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
        "model": "gpt-realtime-2.1",
        "instructions": _SYSTEM_PROMPT,
        "reasoning": {"effort": "low"},
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                # server VAD decides when the caller's turn ends and only then
                # does the model reply. threshold is how loud audio must be to
                # count as speech: too high and quiet/short utterances never
                # register, so she stays silent until the caller repeats. 0.5 is
                # the API default; we sit just under 0.6 to catch more real
                # speech while still ignoring line noise. silence_duration is how
                # long a pause ends the turn — shorter = snappier replies.
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 600,
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


# ── OpenAI Realtime SIP call control ─────────────────────────────────────────
# Inbound calls arrive over SIP: the Twilio number's SIP trunk routes the call
# to OpenAI's SIP connector (sip:<PROJECT_ID>@sip.api.openai.com;transport=tls),
# OpenAI terminates the media itself, and notifies us with a signed
# `realtime.call.incoming` webhook. We accept the call (configuring the realtime
# session), then attach a control WebSocket to drive tools + hangup. No audio
# ever flows through this server anymore.
_OPENAI_REALTIME_BASE = "https://api.openai.com/v1/realtime"

# Strong references to in-flight call tasks. A minutes-long call runs on a
# detached task; without a live reference the event loop can garbage-collect it
# mid-call and drop the line. Tasks remove themselves on completion.
_active_calls: set[asyncio.Task] = set()


def _verify_openai_webhook(secret: str, headers, raw_body: bytes) -> bool:
    """Verify an OpenAI webhook (Standard Webhooks spec, same scheme Resend/Svix
    use): HMAC-SHA256 over '{id}.{timestamp}.{body}', base64-compared
    constant-time against the v1 entries of the webhook-signature header."""
    msg_id = headers.get("webhook-id", "")
    timestamp = headers.get("webhook-timestamp", "")
    signatures = headers.get("webhook-signature", "")
    if not (msg_id and timestamp and signatures):
        return False
    # Drop stale deliveries (>5 min) to blunt replay attacks.
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except ValueError:
        return False
    key = secret.split("_", 1)[1] if secret.startswith("whsec_") else secret
    try:
        key_bytes = base64.b64decode(key)
    except Exception:
        return False
    signed = msg_id.encode() + b"." + timestamp.encode() + b"." + raw_body
    expected = base64.b64encode(
        hmac.new(key_bytes, signed, hashlib.sha256).digest()
    ).decode()
    for entry in signatures.split():
        _, _, sig = entry.partition(",")
        if sig and hmac.compare_digest(sig, expected):
            return True
    return False


def _sip_header(headers: list[dict[str, Any]], name: str) -> str:
    """First value of a SIP header from the webhook's sip_headers array."""
    for h in headers or []:
        if (h.get("name") or "").lower() == name.lower():
            return h.get("value") or ""
    return ""


def _extract_sip_number(header_value: str) -> str:
    """Pull a phone number out of a SIP From/To header value, which looks like
    '"Mario" <sip:+3902123@host;user=phone>;tag=..' or '<tel:+3902123>'. Returns
    E.164-ish (keeps a leading + when present) or '' for anonymous/withheld."""
    if not header_value:
        return ""
    m = re.search(r"(?:sip|tel):([^@;>\s]+)", header_value, re.IGNORECASE)
    user = m.group(1) if m else header_value
    if "anonymous" in user.lower():
        return ""
    digits = "".join(ch for ch in user if ch.isdigit())
    if not digits:
        return ""
    return ("+" if user.strip().startswith("+") else "") + digits


def _find_tenant_by_dialed(dialed: str) -> dict | None:
    """Route an inbound call to a tenant by the dialed DID. Tries an exact
    twilio_number match first, then a tolerant last-9-digit match so SIP
    formatting differences (+prefix, leading zeros) don't miss the tenant."""
    if not dialed:
        return None
    exact = db.get_by_twilio_number(dialed)
    if exact:
        return exact
    for cand in db.get_all_active():
        if _same_number(cand.get("twilio_number"), dialed):
            return cand
    return None


def _build_accept_config(instructions: str) -> dict[str, Any]:
    """Session config for POST /calls/{id}/accept. Reuses the phone agent's
    tuned VAD, voice, reasoning and tools, but drops the PCM format fields: over
    SIP, OpenAI negotiates the codec with the carrier and owns the media path."""
    cfg = json.loads(json.dumps(_SESSION_UPDATE["session"]))
    cfg["instructions"] = instructions
    cfg["audio"]["input"].pop("format", None)
    cfg["audio"]["output"].pop("format", None)
    return cfg


async def _accept_call(call_id: str, config: dict[str, Any]) -> bool:
    """Accept an incoming SIP call and configure its realtime session."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_OPENAI_REALTIME_BASE}/calls/{call_id}/accept",
                headers={
                    "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=config,
            )
            if resp.status_code >= 400:
                logger.error(
                    "Accept failed for call %s: %s — %s",
                    call_id, resp.status_code, resp.text,
                )
                return False
        logger.info("Accepted SIP call %s", call_id)
        return True
    except Exception as exc:
        logger.error("Accept error for call %s: %s", call_id, exc)
        return False


async def _reject_call(call_id: str, status_code: int = 603) -> None:
    """Reject an incoming SIP call (default 603 Decline)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"{_OPENAI_REALTIME_BASE}/calls/{call_id}/reject",
                headers={
                    "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"status_code": status_code},
            )
        logger.info("Rejected SIP call %s (%s)", call_id, status_code)
    except Exception as exc:
        logger.error("Reject error for call %s: %s", call_id, exc)


async def _hangup_call(call_id: str) -> None:
    """Hang up a live SIP call via the OpenAI REST API."""
    if not call_id:
        logger.warning("Cannot hang up: no call_id")
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"{_OPENAI_REALTIME_BASE}/calls/{call_id}/hangup",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            )
        logger.info("Hung up call %s", call_id)
    except Exception as exc:
        logger.error("Failed to hang up call %s: %s", call_id, exc)


@router.post("/incoming")
async def incoming_call(request: Request) -> Response:
    """OpenAI's `realtime.call.incoming` webhook: fires when a SIP call reaches
    our project's connector. We verify the signature, route to the tenant by the
    dialed number, accept the call with its configured session, and hand off to
    a detached control task. Returns 200 so OpenAI connects the accepted call."""
    raw = await request.body()

    secret = settings.OPENAI_WEBHOOK_SECRET
    if secret:
        if not _verify_openai_webhook(secret, request.headers, raw):
            logger.warning("Incoming call webhook: signature verification failed")
            raise HTTPException(status_code=401, detail="Invalid signature")
    else:
        logger.warning(
            "OPENAI_WEBHOOK_SECRET not set — accepting call webhook UNVERIFIED"
        )

    try:
        payload = json.loads(raw)
    except Exception:
        logger.warning("Incoming call webhook: body was not JSON")
        return Response(status_code=400)

    if payload.get("type") != "realtime.call.incoming":
        logger.info("Ignoring webhook type=%s", payload.get("type"))
        return Response(status_code=200)

    data = payload.get("data") or {}
    call_id = data.get("call_id")
    sip_headers = data.get("sip_headers") or []
    if not call_id:
        logger.warning("Incoming call webhook: no call_id")
        return Response(status_code=400)

    caller = _extract_sip_number(_sip_header(sip_headers, "From")) or "sconosciuto"
    dialed = _extract_sip_number(_sip_header(sip_headers, "To"))
    # When a carrier/Twilio forwards a line to the Apollonia number, the call's
    # To header carries the originally-dialed number, and the Apollonia (Twilio)
    # number that identifies the tenant lands in the Diversion header instead —
    # which is also the SIP analogue of Twilio's ForwardedFrom. So route on
    # either. Full headers are logged to diagnose per-carrier quirks.
    diversion = _extract_sip_number(_sip_header(sip_headers, "Diversion"))
    logger.info(
        "Inbound SIP call — call_id=%s caller=%s dialed=%s diversion=%s headers=%s",
        call_id, caller, dialed, diversion or "(none)", json.dumps(sip_headers),
    )

    tenant = _find_tenant_by_dialed(dialed) or _find_tenant_by_dialed(diversion)
    is_demo = _same_number(dialed, settings.TWILIO_PHONE_NUMBER) or _same_number(
        diversion, settings.TWILIO_PHONE_NUMBER
    )
    if tenant is None and not is_demo:
        # Unknown number and not the env-var demo number: decline the call.
        logger.warning(
            "No tenant for dialed=%s / diversion=%s — rejecting", dialed, diversion
        )
        await _reject_call(call_id, 404)
        return Response(status_code=200)

    locale = (tenant.get("locale") if tenant else None) or "it"
    content = _content(locale)
    if tenant is not None:
        instructions = _build_system_prompt(
            content, tenant["agency_name"], tenant["agent_name"]
        )
        tenant_store = tenant_stores.get_or_create(tenant["id"])
        lead_email = tenant.get("lead_email") or settings.LEAD_EMAIL
    else:
        # Env-var fallback: demo behaviour, global store, owner's lead email.
        instructions = _build_system_prompt(content, None, None)
        tenant_store = store
        lead_email = settings.LEAD_EMAIL

    # Decide whether we have a usable caller number. When a tenant's carrier
    # clobbers the caller ID on forwarding, From arrives as the tenant's OWN
    # number (real_number) — useless as a callback. Same for a withheld number.
    # In that case tell Apollonia to ask the caller for one; if she still
    # doesn't get it, _send_lead_email suppresses the (useless) lead.
    tenant_real = tenant.get("real_number") if tenant else None
    caller_number_known = (
        caller not in ("", "sconosciuto")
        and not _same_number(caller, tenant_real)
    )
    if not caller_number_known:
        instructions = instructions + content["ask_for_number"]
        logger.info(
            "Caller number not usable (caller=%s real_number=%s) — "
            "Apollonia will ask the caller for one",
            caller, tenant_real,
        )

    session: dict[str, Any] = {
        "call_id": call_id,
        # tenant_id scopes the persisted call/contact rows. None on the pure
        # env-var demo fallback (no tenant row): that path is not persisted.
        "tenant_id": tenant["id"] if tenant else None,
        "caller_number": caller,
        "caller_number_known": caller_number_known,
        "lead_email": lead_email,
        "locale": locale,
        "listings_shown": [],
        "interested_listings": [],
        "caller_info": {},
        "left_message": None,
        "last_speech_at": 0.0,
        # Wall-clock call start, stamped once the call is accepted (see
        # _run_call); used to compute duration_seconds when the call is persisted.
        "started_at": None,
    }

    # Accept + run the call on a detached task so this webhook returns 200
    # promptly; OpenAI keeps the call pending until the task accepts it.
    task = asyncio.create_task(
        _run_call(
            call_id, _build_accept_config(instructions), session, content, tenant_store
        )
    )
    _active_calls.add(task)
    task.add_done_callback(_active_calls.discard)
    return Response(status_code=200)


# Cheap text model used for the post-call one-sentence lead summary. The
# realtime model handles the live conversation; this is a separate, non-audio
# call made once the call has ended, so latency is not a concern.
_SUMMARY_MODEL = "gpt-5.4-nano"


def _fallback_lead_summary(content: dict[str, Any], session: dict[str, Any]) -> str:
    """Deterministic one-sentence summary, used when the text model is
    unavailable (no API key) or the request fails."""
    caller = session.get("caller_number", content["unknown_caller"])
    caller_name = (
        (session.get("caller_info") or {}).get("name")
        or (session.get("left_message") or {}).get("caller_name")
    )
    who = caller_name or caller
    n_interested = len(session.get("interested_listings") or [])
    if n_interested:
        noun = content["listing_noun"](n_interested)
        return content["summary_interested"].format(who=who, n=n_interested, noun=noun)
    if session.get("left_message") is not None:
        return content["summary_message"].format(who=who)
    if session.get("listings_shown"):
        return content["summary_shown"].format(who=who)
    return content["summary_plain"].format(who=who)


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


async def _generate_lead_summary(
    content: dict[str, Any], detail_body: str, session: dict[str, Any]
) -> str:
    """Ask a text model to write a one-sentence summary of the call, in the
    tenant's locale, so the agent immediately understands what the email is
    about. Falls back to a deterministic template if the API key is missing or
    the request fails."""
    if not settings.OPENAI_API_KEY:
        return _fallback_lead_summary(content, session)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                json={
                    "model": _SUMMARY_MODEL,
                    "instructions": content["summary_instruction"],
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
                return _fallback_lead_summary(content, session)
            summary = _extract_response_text(response.json()).strip()
            if summary:
                return summary
    except Exception as exc:
        logger.error("Failed to generate lead summary via LLM: %s", exc)
    return _fallback_lead_summary(content, session)


def _resolve_callback_number(session: dict[str, Any]) -> str | None:
    """The best number an agent could call back on: the auto-detected caller ID
    when it's usable, otherwise a number the caller spoke aloud (captured into
    caller_info/left_message via the tools' 'phone' field). None when neither
    exists. Pure and idempotent, so email and persistence agree on the number."""
    spoken = (
        (session.get("caller_info") or {}).get("phone")
        or (session.get("left_message") or {}).get("phone")
    )
    if session.get("caller_number_known"):
        return session.get("caller_number") or None
    return spoken or None


def _call_outcome(session: dict[str, Any]) -> str:
    """Classify the call for the dashboard, matching the lead-email subject
    logic: engaged with listings → lead, else left a message → message, else a
    plain call."""
    if session.get("listings_shown"):
        return "lead"
    if session.get("left_message") is not None:
        return "message"
    return "call"


def _persist_call(session: dict[str, Any]) -> None:
    """Write one call_sessions row (always, for the minutes metric) and, when the
    call produced someone to follow up on, one contacts row — both scoped to the
    tenant. Synchronous SQLite; call via asyncio.to_thread. Skips the env-var
    demo fallback (no tenant_id) and never raises into the call task."""
    tenant_id = session.get("tenant_id")
    if not tenant_id:
        return

    started = session.get("started_at")
    # End time is stamped at call teardown (see _run_call's finally), NOT here —
    # otherwise duration would also count the post-call summary + email HTTP
    # calls that run before this, inflating every call by up to tens of seconds.
    ended = session.get("ended_at") or datetime.datetime.now(datetime.timezone.utc)
    duration = int((ended - started).total_seconds()) if started else 0
    ended_iso = ended.isoformat()

    callback = _resolve_callback_number(session)
    content = _content(session.get("locale"))
    summary = session.get("summary") or _fallback_lead_summary(content, session)

    call_session_id = calls_db.add_call_session(
        tenant_id=tenant_id,
        call_id=session.get("call_id"),
        caller_number=callback or session.get("caller_number"),
        started_at=started.isoformat() if started else None,
        ended_at=ended_iso,
        duration_seconds=duration,
        locale=session.get("locale") or "it",
        outcome=_call_outcome(session),
        summary=summary,
    )

    # A contact is only worth surfacing if there's a way to act on it — a name
    # to recognise or a number to call back. Otherwise it's just a logged call.
    caller_info = session.get("caller_info") or {}
    left_message = session.get("left_message")
    interested = session.get("interested_listings") or []
    name = caller_info.get("name") or (left_message or {}).get("caller_name")
    if not (name or callback):
        return

    interest = "; ".join(
        l.get("address", "") for l in interested if l.get("address")
    )
    details = json.dumps(
        {
            "caller_info": caller_info,
            "interested_listings": interested,
            "left_message": left_message,
        },
        ensure_ascii=False,
    )
    calls_db.add_contact(
        tenant_id=tenant_id,
        call_session_id=call_session_id,
        name=name,
        phone=callback,
        interest=interest or None,
        summary=summary,
        details=details,
        created_at=ended_iso,
    )


async def _send_lead_email(session: dict[str, Any]) -> None:
    recipient = session.get("lead_email") or settings.LEAD_EMAIL
    content = _content(session.get("locale"))
    if not settings.RESEND_API_KEY or not recipient:
        logger.warning("RESEND_API_KEY/lead email not configured — lead email skipped")
        return

    # Work out a usable callback number (auto-detected caller ID, else a number
    # the caller spoke aloud). A lead with no callback number is useless — skip.
    caller = _resolve_callback_number(session)
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
            f"{content['email_caller_label']}: {caller}",
            "",
        ]

        lines += [content["email_section_collected"]]
        caller_info = session.get("caller_info") or {}
        if caller_info:
            for key, label in content["caller_info_labels"].items():
                if caller_info.get(key):
                    lines.append(f"{label}: {caller_info[key]}")
        else:
            lines.append(content["email_no_data"])
        lines.append("")

        lines += [content["email_section_interested"]]
        if session["interested_listings"]:
            for listing in session["interested_listings"]:
                lines.append(_format_listing_brief(content, listing))
        else:
            lines.append(content["email_none_specified"])
            lines.append("")

        others = [
            listing for listing in session["listings_shown"]
            if listing not in session["interested_listings"]
        ]
        lines += ["", content["email_section_others"]]
        if others:
            for listing in others:
                lines.append(_format_listing_brief(content, listing))
        else:
            lines.append(content["email_none"])

        if session.get("left_message"):
            msg_data = session["left_message"]
            urgency_tok = msg_data.get("urgency") or content["email_urgency_default"]
            urgency_disp = content["urgency_display"].get(urgency_tok, urgency_tok)
            lines += ["", content["email_section_message"]]
            lines.append(f"{content['email_name_label']}: {msg_data.get('caller_name', content['unknown_caller'])}")
            lines.append(f"{content['email_urgency_label']}: {urgency_disp}")
            lines.append(f"{content['email_message_label']}: {msg_data.get('message', '')}")

        detail_body = "\n".join(lines)
    except Exception as exc:
        logger.error("Failed to format lead email body: %s", exc)
        detail_body = (
            f"{content['email_caller_label']}: {caller}\n"
            f"{content['email_format_error']}"
        )

    # Let a text model write a one-sentence summary of the call so the agent
    # grasps the lead at a glance, then prepend it to the detailed body.
    summary = await _generate_lead_summary(content, detail_body, session)
    # Stash the summary so the persistence step can reuse it instead of paying
    # for a second summarisation LLM call.
    session["summary"] = summary
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
                        content["email_subject_lead"].format(caller=caller)
                        if session.get("listings_shown")
                        else content["email_subject_message"].format(caller=caller)
                        if session.get("left_message") is not None
                        else content["email_subject_call"].format(caller=caller)
                    ),
                    "text": body,
                },
            )
            response.raise_for_status()
        logger.info("Lead email sent for caller %s", caller)
    except Exception as exc:
        logger.error("Failed to send lead email: %s", exc)


async def _run_call(
    call_id: str,
    accept_config: dict[str, Any],
    session: dict[str, Any],
    content: dict[str, Any],
    tenant_store: Any,
) -> None:
    """Accept a pending SIP call, then drive it over its control WebSocket:
    trigger the greeting, answer tool calls, hang up when Apollonia ends the
    call or the line goes silent, then send the lead-summary email. OpenAI owns
    the media, so no audio flows through here — pure control + tool-calling."""
    if not await _accept_call(call_id, accept_config):
        return  # accept failed: nothing to run and no lead to report

    # The call is now connected: stamp the start so duration reflects the live
    # conversation, not the accept latency before it.
    session["started_at"] = datetime.datetime.now(datetime.timezone.utc)

    ws_url = f"wss://api.openai.com/v1/realtime?call_id={call_id}"
    oai_headers = [("Authorization", f"Bearer {settings.OPENAI_API_KEY}")]

    try:
        async with websockets.connect(
            ws_url, additional_headers=oai_headers
        ) as ws:
            logger.info("Control WebSocket attached to call %s", call_id)

            # The session was already configured by the accept call, so just
            # trigger the (model-generated) greeting in the tenant's locale.
            await ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": content["greeting_prompt"]}
                    ],
                },
            }))
            await ws.send(json.dumps({"type": "response.create"}))
            logger.info("Greeting triggered for call %s", call_id)

            session["last_speech_at"] = asyncio.get_event_loop().time()

            async def event_loop() -> None:
                try:
                    await _drive_events()
                except websockets.exceptions.ConnectionClosed:
                    # Normal end of call: the caller hung up (or OpenAI closed
                    # the SIP leg), so the control WebSocket dropped.
                    logger.info("Call %s ended (control WebSocket closed)", call_id)

            async def _drive_events() -> None:
                async for raw in ws:
                    msg = json.loads(raw)
                    etype = msg.get("type")

                    if etype == "response.output_audio_transcript.done":
                        session["last_speech_at"] = asyncio.get_event_loop().time()
                        text = msg.get("transcript", "").strip()
                        if text:
                            logger.info("Apollonia: %s", text)

                    elif etype == "response.function_call_arguments.done":
                        if msg.get("name") == "search_listings":
                            fc_id = msg.get("call_id")
                            try:
                                args = json.loads(msg.get("arguments", "{}"))
                            except json.JSONDecodeError:
                                args = {}
                            results = tenant_store.search(**args)
                            session["listings_shown"].extend(results)
                            logger.info(
                                "search_listings(%s) → %d results", args, len(results)
                            )
                            await ws.send(
                                json.dumps(
                                    {
                                        "type": "conversation.item.create",
                                        "item": {
                                            "type": "function_call_output",
                                            "call_id": fc_id,
                                            "output": json.dumps(
                                                results, ensure_ascii=False
                                            ),
                                        },
                                    }
                                )
                            )
                            await ws.send(json.dumps({"type": "response.create"}))

                        elif msg.get("name") == "get_listing_by_address":
                            fc_id = msg.get("call_id")
                            try:
                                args = json.loads(msg.get("arguments", "{}"))
                            except json.JSONDecodeError:
                                args = {}
                            results = tenant_store.get_by_address(args.get("address_query", ""))
                            session["listings_shown"].extend(results)
                            logger.info(
                                "get_listing_by_address(%s) → %d results", args, len(results)
                            )
                            await ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": fc_id,
                                    "output": json.dumps(results, ensure_ascii=False),
                                },
                            }))
                            await ws.send(json.dumps({"type": "response.create"}))

                        elif msg.get("name") == "mark_listing_interest":
                            fc_id = msg.get("call_id")
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
                            await ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": fc_id,
                                    "output": json.dumps({"recorded": bool(match)}),
                                },
                            }))
                            await ws.send(json.dumps({"type": "response.create"}))

                        elif msg.get("name") == "record_caller_info":
                            fc_id = msg.get("call_id")
                            try:
                                args = json.loads(msg.get("arguments", "{}"))
                            except json.JSONDecodeError:
                                args = {}
                            session["caller_info"].update(
                                {k: v for k, v in args.items() if v}
                            )
                            logger.info("Recorded caller info: %s", args)
                            await ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": fc_id,
                                    "output": json.dumps({"recorded": True}),
                                },
                            }))
                            await ws.send(json.dumps({"type": "response.create"}))

                        elif msg.get("name") == "leave_message":
                            fc_id = msg.get("call_id")
                            try:
                                args = json.loads(msg.get("arguments", "{}"))
                            except json.JSONDecodeError:
                                args = {}
                            session["left_message"] = args
                            logger.info("leave_message: %s", args)
                            await ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": fc_id,
                                    "output": json.dumps({"status": "saved"}, ensure_ascii=False),
                                },
                            }))
                            await ws.send(json.dumps({"type": "response.create"}))

                        elif msg.get("name") == "end_call":
                            fc_id = msg.get("call_id")
                            logger.info("Apollonia ending call %s", call_id)
                            await ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": fc_id,
                                    "output": json.dumps({"ended": True}),
                                },
                            }))
                            # The prompt has her say a short goodbye just before
                            # calling end_call; give that farewell audio a moment
                            # to play out over SIP before we tear the call down.
                            await asyncio.sleep(3.0)
                            await _hangup_call(call_id)
                            return

                    elif etype == "input_audio_buffer.speech_started":
                        session["last_speech_at"] = asyncio.get_event_loop().time()
                        logger.info("Caller speaking")

                    elif etype == "error":
                        logger.error("OpenAI Realtime error: %s", msg)

            async def silence_watchdog() -> None:
                while True:
                    await asyncio.sleep(1)
                    if asyncio.get_event_loop().time() - session["last_speech_at"] > 100:
                        logger.info("100s silence — hanging up call %s", call_id)
                        await _hangup_call(call_id)
                        break

            t1 = asyncio.create_task(event_loop())
            t2 = asyncio.create_task(silence_watchdog())
            done, pending = await asyncio.wait(
                [t1, t2], return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            # Await every task so no exception (incl. the cancelled watchdog or a
            # loop error) is left unretrieved and logged by asyncio.
            for task in (*done, *pending):
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    except Exception as exc:
        logger.exception("Unhandled error in _run_call for %s: %s", call_id, exc)
    finally:
        # Stamp the end at teardown, BEFORE the post-call email/summary pipeline,
        # so duration measures the call itself — not the seconds spent generating
        # the summary and sending the email afterwards.
        session["ended_at"] = datetime.datetime.now(datetime.timezone.utc)
        await _send_lead_email(session)
        # Persist after the email so the LLM summary it generated can be reused.
        # Guarded so a DB hiccup can't crash the call task or lose the email.
        try:
            await asyncio.to_thread(_persist_call, session)
        except Exception:
            logger.exception("Failed to persist call %s", call_id)

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
from listings.store import store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/call")

_GREETING_TEXT = "Buongiorno, sono Apollonia. Come posso aiutarla?"

_LANG_WORDS: dict[str, frozenset[str]] = {
    "italiano": frozenset({"sono", "per", "che", "con", "una", "del", "della", "nel", "anche", "come", "buongiorno", "ciao", "vorrei", "cerco", "affitto", "vendita", "appartamento"}),
    "inglese":  frozenset({"the", "and", "for", "with", "that", "this", "hello", "good", "morning", "looking", "apartment", "rent", "buy", "would", "like", "calling"}),
    "tedesco":  frozenset({"ich", "sie", "und", "die", "der", "das", "ist", "nicht", "hallo", "guten", "morgen", "suche", "miete", "wohnung"}),
    "francese": frozenset({"je", "vous", "les", "des", "est", "avec", "pour", "bonjour", "cherche", "louer", "acheter", "appartement"}),
}


def _detect_language(text: str) -> str:
    words = frozenset(text.lower().split())
    scores = {lang: len(words & vocab) for lang, vocab in _LANG_WORDS.items()}
    best, count = max(scores.items(), key=lambda x: x[1])
    return best if count > 0 else "altra"


def _format_listing_brief(listing: dict[str, Any]) -> str:
    return (
        f"{listing.get('address', '?')} — {listing.get('zone', '?')} — "
        f"{listing.get('type', '?')} — {listing.get('rooms', '?')} locali — "
        f"{listing.get('size_sqm', '?')}mq — €{listing.get('price', '?')}"
    )


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

_SYSTEM_PROMPT = (
    "Sei Apollonia, la receptionist virtuale di uno studio immobiliare.\n"
    "Rispondi sempre in italiano, con tono professionale ma cordiale.\n"
    "\n"
    "# Tipi di chiamata\n"
    "\n"
    "## TIPO A — Il chiamante chiede di un immobile specifico\n"
    "Riconosci questo tipo quando il chiamante menziona un indirizzo o\n"
    "un immobile specifico ('chiamo per l'appartamento in Via Roma...').\n"
    "Procedura:\n"
    "1. Usa get_listing_by_address per cercare quell'immobile.\n"
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
    "6. Se NON trovato: scusati e di' che inoltrerai la richiesta\n"
    "   a un agente immobiliare.\n"
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
    "# Regole generali\n"
    "- Rispondi nel modo più breve possibile. Una frase, mai più di due.\n"
    "- ATTENZIONE LINGUA: ascolta la primissima frase del chiamante. Se non è\n"
    "  in italiano, la TUA RISPOSTA SUCCESSIVA deve essere interamente nella\n"
    "  lingua del chiamante, dalla prima parola — senza dire prima nulla in\n"
    "  italiano. Continua in quella lingua per tutta la chiamata.\n"
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
    "# Quando dire che inoltrerai la richiesta a un agente\n"
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
    "2. Se dice di no: salutalo calorosamente, poi usa lo strumento\n"
    "   end_call per terminare la chiamata.\n"
    "3. Se dice di sì: continua ad aiutarlo normalmente, e ripeti questa\n"
    "   procedura quando hai finito.\n"
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
                "description": "The address or partial address mentioned by the caller",
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
        "anything else you can help with, the caller says no, and you've "
        "said goodbye."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
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
                "transcription": {"model": "whisper-1"},
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


@router.post("/inbound")
async def inbound_call(request: Request) -> Response:
    """
    Twilio calls this when a call arrives. Returns TwiML that immediately opens
    a bidirectional Media Stream WebSocket back to this server.
    """
    form = await request.form()
    caller = str(form.get("From", "sconosciuto"))
    logger.info("Inbound call webhook hit — caller=%s", caller)

    wss_base = settings.PUBLIC_BASE_URL.replace("https://", "wss://").replace(
        "http://", "ws://"
    )

    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=f"{wss_base}/call/stream", track="inbound_track")
    stream.parameter(name="caller", value=caller)
    connect.append(stream)
    response.append(connect)

    return Response(content=str(response), media_type="text/xml")


async def _send_lead_email(session: dict[str, Any]) -> None:
    if not settings.RESEND_API_KEY or not settings.LEAD_EMAIL:
        logger.warning("RESEND_API_KEY/LEAD_EMAIL not configured — lead email skipped")
        return

    caller = session.get("caller_number", "sconosciuto")

    try:
        lines: list[str] = [
            f"Chiamante: {caller}",
            f"Lingua: {session.get('caller_language', 'italiano')}",
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

        lines += ["=== Trascrizione ==="]
        if session["transcript"]:
            for turn in session["transcript"]:
                lines.append(f"{turn['role'].upper()}: {turn['text']}")
        else:
            lines.append("(nessuna trascrizione disponibile)")

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

        body = "\n".join(lines)
    except Exception as exc:
        logger.error("Failed to format lead email body: %s", exc)
        body = (
            f"Chiamante: {caller}\n"
            f"(errore nella formattazione del corpo della mail — controlla i log)"
        )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
                json={
                    "from": settings.RESEND_FROM,
                    "to": [settings.LEAD_EMAIL],
                    "subject": f"Nuovo lead — {caller}",
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
        "transcript": [],
        "listings_shown": [],
        "interested_listings": [],
        "caller_info": {},
        "caller_language": "italiano",
        "suppress_input_until": 0.0,
        "last_speech_at": 0.0,
    }

    oai_headers = [
        ("Authorization", f"Bearer {settings.OPENAI_API_KEY}"),
    ]

    try:
        async with websockets.connect(
            "wss://api.openai.com/v1/realtime?model=gpt-realtime-2",
            additional_headers=oai_headers,
        ) as oai_ws:
            await oai_ws.send(json.dumps(_SESSION_UPDATE))
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

                    if event == "start":
                        start = msg.get("start", {})
                        session["stream_sid"] = msg.get("streamSid") or start.get(
                            "streamSid"
                        )
                        session["call_sid"] = start.get("callSid")
                        params = start.get("customParameters", {})
                        session["caller_number"] = params.get(
                            "caller", session["caller_number"]
                        )
                        logger.info(
                            "Stream started sid=%s caller=%s",
                            session["stream_sid"],
                            session["caller_number"],
                        )
                        if _GREETING_AUDIO:
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

                    elif event == "media":
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
                            session["transcript"].append(
                                {"role": "assistant", "text": text}
                            )
                            logger.info("Apollonia: %s", text)

                    elif (
                        etype
                        == "conversation.item.input_audio_transcription.completed"
                    ):
                        text = msg.get("transcript", "").strip()
                        if text:
                            session["transcript"].append(
                                {"role": "user", "text": text}
                            )
                            detected = _detect_language(text)
                            if detected != "italiano":
                                session["caller_language"] = detected
                            logger.info("Caller said: %s", text)

                    elif etype == "response.function_call_arguments.done":
                        if msg.get("name") == "search_listings":
                            call_id = msg.get("call_id")
                            try:
                                args = json.loads(msg.get("arguments", "{}"))
                            except json.JSONDecodeError:
                                args = {}
                            results = store.search(**args)
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
                            results = store.get_by_address(args.get("address_query", ""))
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

                        elif msg.get("name") == "switch_language":
                            call_id = msg.get("call_id")
                            try:
                                args = json.loads(msg.get("arguments", "{}"))
                            except json.JSONDecodeError:
                                args = {}
                            code = args.get("language_code", "it")
                            name = args.get("language_name", "italiano")
                            session["caller_language"] = name
                            audio_cfg = json.loads(
                                json.dumps(_SESSION_UPDATE["session"]["audio"])
                            )
                            audio_cfg["input"]["transcription"]["language"] = code
                            await oai_ws.send(json.dumps({
                                "type": "session.update",
                                "session": {"type": "realtime", "audio": audio_cfg},
                            }))
                            logger.info(
                                "Switched transcription language to %s (%s)", code, name
                            )
                            await oai_ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id,
                                    "output": json.dumps({"switched": True}),
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
                                await asyncio.sleep(_GOODBYE_DURATION + 0.5)
                            else:
                                await asyncio.sleep(1.5)
                            await websocket.close()

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

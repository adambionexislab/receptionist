import asyncio
import audioop
import base64
import json
import logging
from email.message import EmailMessage
from typing import Any

import aiosmtplib
import websockets
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse

from config import settings
from listings.store import store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/call")

_SYSTEM_PROMPT = (
    "Sei Sara, la receptionist virtuale di uno studio immobiliare.\n"
    "Rispondi sempre in italiano, con tono professionale ma cordiale.\n"
    "\n"
    "# Tipi di chiamata\n"
    "\n"
    "## TIPO A — Il chiamante chiede di un immobile specifico\n"
    "Riconosci questo tipo quando il chiamante menziona un indirizzo o\n"
    "un immobile specifico ('chiamo per l'appartamento in Via Roma...').\n"
    "Procedura:\n"
    "1. Usa get_listing_by_address per cercare quell'immobile.\n"
    "2. Se trovato: conferma che è disponibile e descrivi brevemente.\n"
    "3. Poi fai UNA domanda qualificante alla volta, in questo ordine.\n"
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
    "4. Rispondi a qualsiasi domanda sul immobile usando i dati trovati.\n"
    "   Se non hai l'informazione, di' che chiederai all'agente.\n"
    "5. Se NON trovato: scusati, di' che verificherai e un agente\n"
    "   ricontatterà il chiamante.\n"
    "\n"
    "## TIPO B — Il chiamante cerca senza un immobile specifico\n"
    "Raccolta informazioni — fai UNA domanda alla volta:\n"
    "1. Acquisto (vendita) o affitto?\n"
    "2. Zona o città preferita?\n"
    "3. Numero di camere?\n"
    "4. Budget massimo?\n"
    "Poi usa search_listings con i parametri raccolti.\n"
    "Descrivi i risultati in modo naturale, come farebbe un agente umano.\n"
    "Se nessun risultato: chiedi se vuole provare criteri diversi.\n"
    "\n"
    "# Regole generali\n"
    "- Aspetta SEMPRE che il chiamante finisca di parlare prima di rispondere.\n"
    "- Non terminare mai la chiamata — aspetta che sia il chiamante a salutare.\n"
    "- Alla fine di ogni chiamata di' che un agente ricontatterà per i dettagli.\n"
    "- Non inventare mai dati non presenti nei risultati degli strumenti.\n"
    "- Non trasferire mai la chiamata.\n"
    "- Raccogli sempre il nome del chiamante se non lo conosce già.\n"
    "- Salva mentalmente le informazioni raccolte per il riepilogo finale.\n"
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
                    "silence_duration_ms": 1200,
                },
            },
            "output": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "voice": "alloy",
            },
        },
        "tools": [_SEARCH_TOOL, _GET_LISTING_TOOL],
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
    if not settings.SMTP_USER or not settings.LEAD_EMAIL:
        logger.warning("SMTP/LEAD_EMAIL not configured — lead email skipped")
        return

    caller = session.get("caller_number", "sconosciuto")
    lines: list[str] = [f"Chiamante: {caller}", ""]

    lines += ["=== Trascrizione ==="]
    if session["transcript"]:
        for turn in session["transcript"]:
            lines.append(f"{turn['role'].upper()}: {turn['text']}")
    else:
        lines.append("(nessuna trascrizione disponibile)")

    lines += ["", "=== Immobili mostrati ==="]
    if session["listings_shown"]:
        for listing in session["listings_shown"]:
            lines.append(json.dumps(listing, ensure_ascii=False))
    else:
        lines.append("Nessun immobile mostrato.")

    msg = EmailMessage()
    msg["Subject"] = f"Nuovo lead — {caller}"
    msg["From"] = settings.SMTP_USER
    msg["To"] = settings.LEAD_EMAIL
    msg.set_content("\n".join(lines))

    smtp_port = int(settings.SMTP_PORT or 587)
    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST or "smtp.gmail.com",
            port=smtp_port,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            use_tls=(smtp_port == 465),
            start_tls=(smtp_port != 465),
        )
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

    session: dict[str, Any] = {
        "stream_sid": None,
        "call_sid": None,
        "caller_number": "sconosciuto",
        "transcript": [],
        "listings_shown": [],
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
                if evt.get("type") == "session.updated":
                    break

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
            logger.info("Greeting triggered")

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

                    elif event == "media":
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

                    elif etype == "response.audio_transcript.done":
                        text = msg.get("transcript", "").strip()
                        if text:
                            session["transcript"].append(
                                {"role": "assistant", "text": text}
                            )

                    elif (
                        etype
                        == "conversation.item.input_audio_transcription.completed"
                    ):
                        text = msg.get("transcript", "").strip()
                        if text:
                            session["transcript"].append(
                                {"role": "user", "text": text}
                            )

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

                    elif etype == "error":
                        logger.error("OpenAI Realtime error: %s", msg)

            t1 = asyncio.create_task(twilio_to_openai())
            t2 = asyncio.create_task(openai_to_twilio())
            _done, pending = await asyncio.wait(
                [t1, t2], return_when=asyncio.FIRST_COMPLETED
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

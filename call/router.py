import asyncio
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
    "Il tuo obiettivo è capire cosa cerca il chiamante (acquisto o affitto,\n"
    "zona, numero di camere, budget) e cercare gli immobili disponibili\n"
    "usando lo strumento search_listings.\n"
    "Dopo aver trovato risultati, leggili in modo naturale — non elencare\n"
    "tutti i campi, descrivi l'immobile come farebbe un agente umano.\n"
    "Se non ci sono risultati, di' che verificherai con i colleghi e che\n"
    "qualcuno ricontatterà il chiamante.\n"
    "Alla fine della chiamata, saluta cordialmente e ringrazia il chiamante.\n"
    "Non inventare immobili che non esistono nel risultato della ricerca.\n"
    "Non trasferire mai la chiamata — sei l'unico punto di contatto."
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

_SESSION_UPDATE: dict[str, Any] = {
    "type": "session.update",
    "session": {
        "type": "realtime",
        "model": "gpt-realtime-2",
        "instructions": _SYSTEM_PROMPT,
        "voice": "alloy",
        "input_audio_format": "g711_ulaw",
        "output_audio_format": "g711_ulaw",
        "input_audio_transcription": {"model": "gpt-4o-mini-transcribe"},
        "turn_detection": {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 600,
        },
        "tools": [_SEARCH_TOOL],
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

            async def twilio_to_openai() -> None:
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
                        await oai_ws.send(
                            json.dumps(
                                {
                                    "type": "input_audio_buffer.append",
                                    "audio": msg["media"]["payload"],
                                }
                            )
                        )

                    elif event == "stop":
                        logger.info(
                            "Stream stop received sid=%s", session["stream_sid"]
                        )
                        break

            async def openai_to_twilio() -> None:
                async for raw in oai_ws:
                    msg = json.loads(raw)
                    etype = msg.get("type")

                    if etype == "response.audio.delta":
                        if session["stream_sid"]:
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "event": "media",
                                        "streamSid": session["stream_sid"],
                                        "media": {
                                            "track": "outbound",
                                            "payload": msg["delta"],
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

"""Ask questions across every meeting note — "come siamo messi con Halo
Reality?" — and get an answer grounded in what the rep actually recorded.

Deliberately NOT a retrieval/embedding setup. The whole corpus is one sales
rep's structured notes: a few hundred at most, a few hundred words each, so the
cheapest correct design is to put all of them in front of the model and let it
do the searching. An embedding index would add a second store to keep in sync
with every edit and every delete, to answer questions a single call already
answers over the complete truth.

What is sent is the STRUCTURED note, never the raw transcript: the transcript
is the rambling source the structure was already distilled from, and it would
multiply the context for nothing.

Fails loudly (ChatError) — a confident wrong answer about where a customer
stands is worse than no answer.
"""

import asyncio
import datetime
import logging
from typing import Any, Optional

import httpx

from config import settings
from salesnotes import content, db, schema

logger = logging.getLogger(__name__)

_RESPONSES_URL = "https://api.openai.com/v1/responses"

# How many notes can be pulled into one answer, and the character budget the
# corpus is trimmed to. Oldest notes are dropped first if the budget binds, and
# the model is told so rather than silently answering over a truncated history.
MAX_NOTES = 500
MAX_CORPUS_CHARS = 120_000

# How much conversation is carried back. The rep asks follow-ups ("and before
# that?"), so history matters, but this is a lookup tool and not a chat log.
MAX_HISTORY_MESSAGES = 12
MAX_QUESTION_CHARS = 2000

_LIST_LABELS = {
    "went_well": "Andato bene",
    "went_wrong": "Andato male",
    "objections": "Obiezioni",
    "next_steps": "Prossimi passi",
}

_INSTRUCTIONS = (
    "Sei l'assistente della commerciale di ApollonIA. Hai davanti TUTTE le note\n"
    "delle sue riunioni con clienti e potenziali clienti, gia strutturate.\n"
    "Rispondi alle sue domande basandoti solo su quelle note.\n"
    "\n"
    "Regole:\n"
    "- Usa SOLO cio che c'e nelle note. Non inventare fatti, nomi, date, cifre\n"
    "  o impegni che non ci sono.\n"
    "- Se le note non contengono la risposta, dillo chiaramente in una frase\n"
    "  invece di ipotizzare.\n"
    "- Il nome del cliente nella domanda puo essere scritto in modo diverso\n"
    "  dalle note (maiuscole, s.r.o., refusi): considera le corrispondenze\n"
    "  ragionevoli, ma se resti incerta su quale cliente si intenda, chiedi.\n"
    "- Quando la domanda riguarda un cliente, guarda TUTTE le sue riunioni e\n"
    "  raccontale in ordine di tempo: dove si era arrivati, cosa ha obiettato,\n"
    "  cosa resta da fare. Le note piu recenti pesano di piu.\n"
    "- Cita la data delle riunioni a cui ti riferisci (formato GG/MM/AAAA),\n"
    "  cosi che lei possa aprire la nota giusta.\n"
    "- I prossimi passi ancora aperti sono la cosa piu utile: mettili in fondo\n"
    "  quando ci sono.\n"
    "- Rispondi breve e concreto: niente preamboli, niente Markdown (niente #,\n"
    "  *, **), testo semplice. Elenchi con un trattino a inizio riga.\n"
    "- Scrivi in {language}, qualunque sia la lingua delle note.\n"
)


class ChatError(Exception):
    """The chat call failed. The dashboard shows it and lets her retry."""


def _fmt_date(iso: Optional[str]) -> str:
    if not iso:
        return "data ignota"
    try:
        text = iso if (iso.endswith("Z") or "+" in iso) else iso + "+00:00"
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%d/%m/%Y")
    except ValueError:
        return iso


def _render_note(note: dict[str, Any]) -> str:
    """One note as plain text. Empty fields are omitted rather than written out
    as 'null', so absence reads as absence instead of as a value."""
    draft = "" if note.get("status") == "saved" else " [bozza non salvata]"
    lines = [f"### Riunione del {_fmt_date(note.get('created_at'))}{draft}"]
    if note.get("title"):
        lines.append(f"Titolo: {note['title']}")
    lines.append(f"Cliente: {note.get('customer') or 'non indicato'}")
    if note.get("outcome"):
        lines.append(f"Esito: {note['outcome']}")
    if note.get("summary"):
        lines.append(f"Sintesi: {note['summary']}")
    for field in schema.LIST_FIELDS:
        items = note.get(field) or []
        if items:
            lines.append(_LIST_LABELS[field] + ":")
            lines.extend(f"- {item}" for item in items)
    return "\n".join(lines)


def build_corpus(notes: list[dict[str, Any]]) -> tuple[str, int, bool]:
    """Notes -> the text block sent to the model, oldest first so the model
    reads a customer's history in the order it happened.

    Returns (corpus, notes_included, truncated). `notes` arrives newest-first
    (see db.list_notes), which is also the order we fill the budget in: if
    something has to be dropped it is the oldest meeting, not this month's.
    """
    kept: list[str] = []
    used = 0
    truncated = False
    for note in notes:
        rendered = _render_note(note)
        if kept and used + len(rendered) > MAX_CORPUS_CHARS:
            truncated = True
            break
        kept.append(rendered)
        used += len(rendered)
    kept.reverse()
    return "\n\n".join(kept), len(kept), truncated


def _messages(
    corpus: str,
    count: int,
    truncated: bool,
    history: list[dict[str, str]],
    question: str,
) -> list[dict[str, str]]:
    """The corpus goes in as the first user turn rather than into the
    instructions, so a long follow-up conversation never re-sends it and the
    prompt prefix stays identical (and cacheable) across questions."""
    header = f"Note disponibili: {count}."
    if truncated:
        header += (
            " Sono le piu recenti: le riunioni piu vecchie non rientrano in"
            " questa finestra, quindi dillo se la domanda riguarda il passato"
            " remoto."
        )
    return [
        {"role": "user", "content": f"{header}\n\n{corpus}"},
        {"role": "assistant", "content": "Ho letto le note. Chiedi pure."},
        *history[-MAX_HISTORY_MESSAGES:],
        {"role": "user", "content": question},
    ]


def _extract_output_text(data: dict[str, Any]) -> str:
    """Same walk as salesnotes/extraction.py: prefer output_text, else collect
    the message parts."""
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


async def ask(question: str, history: list[dict[str, str]], language: str) -> dict[str, Any]:
    """Answer one question over every note. Returns the answer plus how many
    notes it was grounded in, which the dashboard shows underneath it."""
    if not settings.OPENAI_API_KEY:
        raise ChatError("OPENAI_API_KEY not configured")
    question = (question or "").strip()[:MAX_QUESTION_CHARS]
    if not question:
        raise ChatError("Question is empty")

    notes = await asyncio.to_thread(db.list_notes, MAX_NOTES)
    corpus, count, truncated = build_corpus(notes)
    # Nothing to ground an answer in. Answering anyway would mean answering
    # from the model's own head, which is exactly what this must never do.
    if not count:
        return {"answer": None, "notes_used": 0, "truncated": False, "empty": True}

    lang_name = content.LANGUAGES.get(language, content.LANGUAGES[content.DEFAULT_LANGUAGE])
    body = {
        "model": settings.NOTES_CHAT_MODEL,
        "instructions": _INSTRUCTIONS.format(language=lang_name),
        "input": _messages(corpus, count, truncated, history, question),
        "reasoning": {"effort": "medium"},
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                _RESPONSES_URL,
                headers={
                    "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            if resp.status_code >= 400:
                logger.error("Notes chat request failed: %s — %s", resp.status_code, resp.text)
                raise ChatError(f"OpenAI chat call rejected (status {resp.status_code})")
            data = resp.json()
    except httpx.HTTPError as exc:
        logger.error("Notes chat request error: %s", exc)
        raise ChatError(f"Chat request failed: {exc}") from exc

    answer = _extract_output_text(data).strip()
    if not answer:
        raise ChatError("Chat returned no output")
    return {"answer": answer, "notes_used": count, "truncated": truncated, "empty": False}

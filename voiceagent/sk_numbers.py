"""Spell out digits as Slovak words before text reaches ElevenLabs.

eleven_v4_turbo drifts into Czech when it reads Slovak digit strings itself
(Phase 0, elevenlabs-voice-roadmap.md). Words leave it nothing to guess. This
module becomes part of the agent (roadmap §4.2); it has no dependencies.

    spell_numbers("Vaše číslo je 0903 123 456.")
    -> "Vaše číslo je nula deväť nula tri, jedna dva tri, štyri päť šesť."

What it recognises, in this order:
  - phone numbers: +421 / 00421 / plus 421 / a leading 0, at least 9 digits.
    Read digit by digit, group by group, with a pause (comma) between groups,
    the way a Slovak speaker reads a number back.
  - times:  "o 14:30" -> "o štrnástej tridsať"; "14:30" -> "štrnásť tridsať"
  - decimals: "2,5" -> "dve celé päť"
  - amounts with a space as thousands separator: "189 000" -> one number
  - any other number: "12" -> "dvanásť"
plus "€" -> "eur", "m²"/"m2" -> "metrov štvorcových", "%" -> "percent".

Numbers are written with spaces between their parts ("sto osemdesiat deväť
tisíc"). Spoken, that is exactly the one-word written form.

For streaming, SlovakNumberStream holds back a number that may still be
growing (a delta can end in the middle of "189 0") until it is complete.
"""

import re

_DIGIT = ["nula", "jedna", "dva", "tri", "štyri", "päť", "šesť", "sedem", "osem", "deväť"]
_UNIT = ["", "jeden", "dva", "tri", "štyri", "päť", "šesť", "sedem", "osem", "deväť"]
_TEEN = ["desať", "jedenásť", "dvanásť", "trinásť", "štrnásť", "pätnásť",
         "šestnásť", "sedemnásť", "osemnásť", "devätnásť"]
_TENS = ["", "", "dvadsať", "tridsať", "štyridsať", "päťdesiat", "šesťdesiat",
         "sedemdesiat", "osemdesiat", "deväťdesiat"]
_HUNDREDS = ["", "sto", "dvesto", "tristo", "štyristo", "päťsto", "šesťsto",
             "sedemsto", "osemsto", "deväťsto"]
# "o 14:30" -> "o štrnástej": the hour after "o" is a feminine ordinal, locative.
_HOUR_LOC = ["nultej", "prvej", "druhej", "tretej", "štvrtej", "piatej", "šiestej",
             "siedmej", "ôsmej", "deviatej", "desiatej", "jedenástej", "dvanástej",
             "trinástej", "štrnástej", "pätnástej", "šestnástej", "sedemnástej",
             "osemnástej", "devätnástej", "dvadsiatej", "dvadsiatej prvej",
             "dvadsiatej druhej", "dvadsiatej tretej", "dvadsiatej štvrtej"]


def _below_thousand(n: int) -> list[str]:
    words = []
    if n >= 100:
        words.append(_HUNDREDS[n // 100])
        n %= 100
    if 10 <= n < 20:
        words.append(_TEEN[n - 10])
    else:
        if n >= 20:
            words.append(_TENS[n // 10])
            n %= 10
        if n:
            words.append(_UNIT[n])
    return words


def cardinal(n: int) -> str:
    """189000 -> 'sto osemdesiat deväť tisíc'. Masculine 'jeden'/'dva'."""
    if n == 0:
        return "nula"
    words: list[str] = []
    millions, rest = divmod(n, 1_000_000)
    if millions:
        if millions == 1:
            words.append("milión")
        else:
            noun = "milióny" if 2 <= millions <= 4 else "miliónov"
            words += cardinal(millions).split() + [noun]
    thousands, rest = divmod(rest, 1000)
    if thousands:
        if thousands == 1:
            words.append("tisíc")
        elif thousands == 2:
            words.append("dve tisíc")
        else:
            words += _below_thousand(thousands) + ["tisíc"]
    words += _below_thousand(rest)
    return " ".join(words)


def _digits(s: str) -> str:
    return " ".join(_DIGIT[int(d)] for d in s)


def _phone(raw: str) -> str:
    """'+421 948 155 064' -> 'plus štyri dva jedna, deväť štyri osem, ...'"""
    plus = raw.lstrip().startswith("+") or raw.lower().lstrip().startswith("plus")
    body = re.sub(r"^\s*(\+|plus\s*)", "", raw, flags=re.IGNORECASE)
    groups = body.split()
    if len(groups) == 1:  # no spacing given: split the way the number is read
        d = groups[0]
        if d.startswith("00421"):
            plus, d = True, d[2:]
        if d.startswith("421") and (plus or len(d) == 12):
            plus = True
            groups = ["421"] + [d[i:i + 3] for i in range(3, len(d), 3)]
        elif len(d) == 10 and d.startswith("0"):
            groups = [d[:4], d[4:7], d[7:]]
        else:
            groups = [d[i:i + 3] for i in range(0, len(d), 3)]
    spoken = ", ".join(_digits(g) for g in groups)
    return f"plus {spoken}" if plus else spoken


def _is_phone(raw: str) -> bool:
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 9:
        return False
    head = raw.lstrip().lower()
    return head.startswith(("+", "plus")) or digits.startswith(("0", "421"))


def _time(h: int, m: int, after_o: bool) -> str:
    hour = _HOUR_LOC[h] if after_o and h < len(_HOUR_LOC) else cardinal(h)
    if m == 0:
        return hour
    minute = f"nula {_DIGIT[m]}" if m < 10 else cardinal(m)
    return f"{hour} {minute}"


def _decimal(whole: int, frac: str) -> str:
    if whole == 1:
        head = "jedna celá"
    elif whole == 2:
        head = "dve celé"
    elif whole in (3, 4):
        head = f"{cardinal(whole)} celé"
    else:
        head = f"{cardinal(whole)} celých"
    return f"{head} {cardinal(int(frac))}"


_NUMBER_RE = re.compile(
    r"(?P<phone>(?:\+|\bplus\s+)?\d(?:[  ]?\d){8,})"
    r"|(?P<o>\bo\s+)?(?P<time>\b\d{1,2}:\d{2}\b)"
    r"|(?P<dec>\b\d+,\d+\b)"
    r"|(?P<num>\b\d{1,3}(?:[  ]\d{3})+\b|\b\d+\b)",
    re.IGNORECASE,
)
# A symbol glued to its number ("189€") gets a space; one already spaced keeps
# its own, so a symbol arriving alone in a streamed piece reads the same.
_UNITS = [
    (re.compile(r"(?<=\d)€"), " eur"),
    (re.compile(r"€"), "eur"),
    (re.compile(r"\bm(?:²|2)\b|m²"), "metrov štvorcových"),
    (re.compile(r"(?<=\d)%"), " percent"),
    (re.compile(r"%"), "percent"),
]


def _replace(m: re.Match) -> str:
    if m.group("phone"):
        raw = m.group("phone")
        if _is_phone(raw):
            return _phone(raw)
        # A long amount, not a phone number: fall through to the amount rules.
        return _NUMBER_RE.sub(_replace_amount, raw)
    if m.group("time"):
        h, mi = (int(x) for x in m.group("time").split(":"))
        prefix = m.group("o") or ""
        return prefix + _time(h, mi, after_o=bool(prefix))
    if m.group("dec"):
        whole, frac = m.group("dec").split(",")
        return _decimal(int(whole), frac)
    return _replace_amount(m)


def _replace_amount(m: re.Match) -> str:
    raw = m.group(0)
    return cardinal(int(re.sub(r"\D", "", raw)))


def spell_numbers(text: str) -> str:
    """All digits in `text` as Slovak words; everything else untouched."""
    for pattern, words in _UNITS:
        text = pattern.sub(words, text)
    return _NUMBER_RE.sub(_replace, text)


# A number that may still be growing at the end of a streamed chunk: digits,
# optionally a +/plus before them, and the separators a number can contain.
# "o " is part of it, because "o 14:30" reads differently from "14:30".
_TAIL_RE = re.compile(
    r"(?:(?:\bo\s+)?(?:\+|\bplus\s*)?\d[\d  :,.]*|\+|\bplus\s*|\bo\s*)$",
    re.IGNORECASE,
)
# A piece ending in one of these ends a word; anything else may be half a word
# ("m" of "m²").
_WORD_END = (" ", " ", "\n", ".", ",", "?", "!", ";", ":", "…")


class SlovakNumberStream:
    """spell_numbers for text that arrives in pieces.

    feed() returns what is safe to send now. Anything that could still be part
    of a number ("0903 12", "189 0", "o 14:") or of a word ("m" before "²")
    waits for the next piece. finish() returns the rest."""

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, piece: str) -> str:
        self._pending += piece
        text = self._pending
        tail = _TAIL_RE.search(text)
        cut = tail.start() if tail else len(text)
        if not text.endswith(_WORD_END):
            last_space = max(text.rfind(" "), text.rfind(" "), text.rfind("\n"))
            cut = min(cut, last_space + 1)
        ready, self._pending = text[:cut], text[cut:]
        return spell_numbers(ready)

    def finish(self) -> str:
        ready, self._pending = self._pending, ""
        return spell_numbers(ready)



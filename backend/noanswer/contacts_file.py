"""Read the name/number pairs out of an exported phone book.

Two formats, because those are what phones export:
- vCard (.vcf) — iPhone (iCloud → Contacts → Export vCard), Android's own
  Contacts app, Google Contacts' "vCard" export;
- CSV — Google Contacts' "Google CSV" and Outlook's export.

The parser is deliberately forgiving: an export is never edited by hand and
nobody can fix one on their phone, so anything unreadable is skipped rather
than failing the whole file. Nothing is stored here — the dashboard shows what
was found and the owner ticks which entries go on the list.
"""

import csv
import io
import quopri
import re

from noanswer import db

# One contact per number: a person with a mobile and a landline is two lines on
# the list, since either can be the one that rings.
Contact = dict[str, str]


def parse(text: str) -> list[Contact]:
    """Every distinct number in the file, with the contact's name. Order follows
    the file; a number appearing twice keeps its first name."""
    text = (text or "").lstrip("﻿")
    found = _parse_vcard(text) if "BEGIN:VCARD" in text.upper() else _parse_csv(text)
    seen: set[str] = set()
    out: list[Contact] = []
    for name, raw in found:
        number = db.normalize(raw)
        if not number:
            continue
        key = db.match_key(number)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name.strip(), "number": number})
    return out


# ── vCard ────────────────────────────────────────────────────────────────────
def _unfold(text: str) -> list[str]:
    """Join vCard's folded lines: a line starting with a space or tab continues
    the previous one. Quoted-printable soft breaks (a trailing '=') are joined
    the same way, which is how Android writes long non-ASCII names."""
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if lines and line[:1] in (" ", "\t"):
            lines[-1] += line[1:]
        elif lines and lines[-1].endswith("=") and "QUOTED-PRINTABLE" in lines[-1].upper():
            lines[-1] = lines[-1][:-1] + line
        else:
            lines.append(line)
    return lines


def _value(prop: str, value: str) -> str:
    """Decode a property value according to its parameters."""
    params = prop.upper()
    if "QUOTED-PRINTABLE" in params:
        charset = re.search(r"CHARSET=([\w-]+)", params)
        try:
            value = quopri.decodestring(value.encode("ascii", "ignore")).decode(
                charset.group(1) if charset else "utf-8", "replace"
            )
        except LookupError:
            value = quopri.decodestring(value.encode("ascii", "ignore")).decode("utf-8", "replace")
    return value.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").strip()


def _name_from_n(value: str) -> str:
    """N is 'Family;Given;Middle;Prefix;Suffix' — used only when FN is missing."""
    parts = [p.strip() for p in value.split(";")]
    family = parts[0] if parts else ""
    given = parts[1] if len(parts) > 1 else ""
    return " ".join(p for p in (given, family) if p)


def _parse_vcard(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    fn = n = ""
    tels: list[str] = []
    for line in _unfold(text):
        if ":" not in line:
            continue
        prop, value = line.split(":", 1)
        # "item1.TEL;type=CELL" → "TEL"
        name = prop.split(";", 1)[0].split(".")[-1].strip().upper()
        if name == "BEGIN":
            fn = n = ""
            tels = []
        elif name == "FN":
            fn = _value(prop, value)
        elif name == "N":
            n = _name_from_n(_value(prop, value))
        elif name == "TEL":
            # vCard 4 writes "tel:+39..." as the value.
            tels.append(re.sub(r"^tel:", "", value.strip(), flags=re.IGNORECASE))
        elif name == "END":
            label = fn or n
            out.extend((label, t) for t in tels)
            tels = []
    return out


# ── CSV ──────────────────────────────────────────────────────────────────────
# Headers that hold a number. Google: "Phone 1 - Value"; Outlook: "Mobile
# Phone", "Home Phone"; Italian and Slovak Outlook: "Cellulare", "Telefono…",
# "Mobil", "Telefón…". The label columns next to them ("Phone 1 - Type",
# "Phone 1 - Label") hold words, not numbers, and are skipped.
_PHONE_HEADER = re.compile(r"phone|telefon|telefón|cellulare|mobil", re.IGNORECASE)
_LABEL_HEADER = re.compile(r"type|label|tipo|typ\b", re.IGNORECASE)


def _parse_csv(text: str) -> list[tuple[str, str]]:
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    header = next(reader, None)
    if not header:
        return []
    cols = [h.strip() for h in header]
    lower = [c.lower() for c in cols]
    phone_cols = [
        i for i, c in enumerate(cols)
        if _PHONE_HEADER.search(c) and not _LABEL_HEADER.search(c)
    ]

    def col(*names: str) -> int:
        for name in names:
            if name in lower:
                return lower.index(name)
        return -1

    full = col("name", "display name", "nome", "meno", "full name")
    first = col("first name", "given name", "nome di battesimo", "krstné meno")
    middle = col("middle name", "additional name")
    last = col("last name", "family name", "cognome", "priezvisko")
    org = col("organization name", "organization 1 - name", "company", "società")

    out: list[tuple[str, str]] = []
    for row in reader:
        def cell(i: int) -> str:
            return row[i].strip() if 0 <= i < len(row) else ""

        name = cell(full) or " ".join(
            p for p in (cell(first), cell(middle), cell(last)) if p
        ) or cell(org)
        for i in phone_cols:
            # Google packs several numbers into one cell: "+39 333 ::: +39 02".
            for raw in cell(i).split(":::"):
                if raw.strip():
                    out.append((name, raw))
    return out

"""Slovak extraction instructions for acquisizione/extraction.py. Mirrors
acquisizione/content.py's Italian baseline — see that module's docstring for
why this is written in Slovak rather than English."""

_SK_INSTRUCTIONS = (
    "Ste asistent pre realitné kancelárie. Dostávate prepis stretnutia medzi\n"
    "realitným maklérom a predávajúcim, ktorý chce ponúknuť nehnuteľnosť na\n"
    "predaj alebo prenájom. Vašou úlohou je v jednej odpovedi zodpovedajúcej\n"
    "zadanej schéme vytvoriť:\n"
    "1. Štruktúrované polia nehnuteľnosti (listing_fields), použite LEN to,\n"
    "   čo bolo v prepise výslovne povedané.\n"
    "2. Opisný text inzerátu (listing_text), napísaný po slovensky,\n"
    "   pripravený na zverejnenie, na základe zozbieraných údajov.\n"
    "3. Poznámky zo stretnutia (notes): štruktúrovaný text po slovensky s\n"
    "   dôležitými faktami A otvorenými bodmi na vyriešenie.\n"
    "\n"
    "Základné pravidlá:\n"
    "- NIKDY nevymýšľajte právne alebo finančne významné hodnoty (plocha,\n"
    "  cena, energetická trieda, druh vlastníctva, katastrálne údaje). Ak to\n"
    "  nebolo jasne povedané, ponechajte pole null: to je očakávané\n"
    "  správanie, nie chyba.\n"
    "- Hodnoty uveďte presne tak, ako boli povedané; nezaokrúhľujte ani\n"
    "  neodhadujte.\n"
    "- listing_text a notes musia byť napísané celé po slovensky, aj keď\n"
    "  prepis obsahuje výrazy v inom jazyku.\n"
    "\n"
    "Poznámky zo stretnutia (pole 'notes')\n"
    "Napíšte čitateľný text, pripravený na prečítanie a ručnú úpravu\n"
    "maklérom. Použite PRESNE tieto tri sekcie, v tomto poradí a s týmito\n"
    "nadpismi:\n"
    "\n"
    "KĽÚČOVÉ BODY\n"
    "- Dôležité fakty zo stretnutia, ktoré NIE SÚ už v štruktúrovaných\n"
    "  poliach: dôvod predaja, časový rámec, situácia predávajúceho,\n"
    "  obmedzenia, vykonané alebo potrebné práce, zvláštnosti nehnuteľnosti,\n"
    "  očakávania ohľadom ceny, prípadné problémy.\n"
    "\n"
    "NA VYRIEŠENIE\n"
    "- Otvorené body a veci na vybavenie: doklady na zaobstaranie, overenia,\n"
    "  chýbajúce informácie, dané prísľuby ('pošlem pôdorys', 'skontrolujem\n"
    "  v katastri', 'zavolám').\n"
    "- NEUVÁDZAJTE, kto sa tým má zaoberať: napíšte len to, čo treba urobiť.\n"
    "  Nepíšte 'maklér musí...' ani 'predávajúci musí...'.\n"
    "- Ak niektorý bod blokuje zverejnenie inzerátu, pridajte na koniec\n"
    "  riadka '(blokuje zverejnenie)'.\n"
    "\n"
    "POVEDAL PREDÁVAJÚCI\n"
    "- Zopár krátkych a relevantných doslovných citácií v úvodzovkách, aby\n"
    "  maklér vedel overiť, odkiaľ informácie pochádzajú.\n"
    "\n"
    "Pravidlá pre poznámky:\n"
    "- Používajte pomlčky (-) pre zoznamy, jeden riadok na bod, krátke vety.\n"
    "- Žiadny Markdown (žiadne #, *, **): je to obyčajný text.\n"
    "- Neopakujte štruktúrované polia (m², cena, izby...), ak nepridávajú\n"
    "  kontext.\n"
    "- Ak sekcia nemá obsah, napíšte aj tak nadpis a pod ním riadok\n"
    "  '- žiadne'.\n"
    "- Vychádzajte IBA z prepisu: nepridávajte rady ani domnienky.\n"
    "\n"
    "Počítanie izieb (pole 'locali') — JEDINÁ výnimka z pravidla neodvodzovať:\n"
    "spočítať nie je vymyslieť.\n"
    "- Ak predávajúci výslovne uvedie celkový počet ('je to trojizbový byt'),\n"
    "  použite tento počet.\n"
    "- Ak však celkový počet neuvedie, ale počas stretnutia vymenuje\n"
    "  jednotlivé miestnosti ('je tam obývačka, potom spálňa a detská izba'),\n"
    "  SPOČÍTAJTE vymenované obytné miestnosti a uveďte číslo. Nenechávajte\n"
    "  null len preto, že celkový počet nebol vyslovený.\n"
    "- Čo počítať: obývačka, spálne, detské izby, pracovňa, jedáleň.\n"
    "- Čo NEPOČÍTAŤ: kuchyňa (na Slovensku sa kuchyňa do počtu izieb\n"
    "  nezapočítava — trojizbový byt = 3 izby + kuchyňa), kúpeľne a WC,\n"
    "  chodby a predsiene, komory a šatníky, špajza, balkóny, lodžie a\n"
    "  terasy, pivnica, garáž.\n"
    "- Každú miestnosť započítajte iba raz, aj keď je spomenutá viackrát\n"
    "  počas stretnutia.\n"
    "- Iba ak z vymenovaných miestností nie je jasné, koľko ich je, uveďte\n"
    "  null.\n"
)

SK: dict = {
    "extraction_instructions": _SK_INSTRUCTIONS,
    # ── meeting-summary email (see acquisizione/notify.py) ────────────────────
    # Product name, untranslated — see content.IT's note.
    "email_subject": "ApollonIA Meeting — {address}",
    "email_intro": "Zhrnutie ApollonIA Meeting s predávajúcim.",
    "email_section_missing": "=== Chýbajúce údaje (na doplnenie) ===",
    "email_none_missing": "Žiadne: všetky povinné údaje sú vyplnené.",
    "email_section_notes": "=== Poznámky zo stretnutia ===",
    "email_no_notes": "Žiadne poznámky.",
    "email_section_listing_text": "=== Opis inzerátu ===",
    "email_section_fields": "=== Údaje o nehnuteľnosti ===",
    "email_section_transcript": "=== Prepis stretnutia ===",
    "email_no_text": "(žiadny opis)",
    "email_yes": "áno",
    "email_no": "nie",
    "listing_type_values": {"vendita": "Predaj", "affitto": "Prenájom"},
    "field_labels": {
        "tipo_annuncio": "Typ inzerátu",
        "tipologia": "Typ nehnuteľnosti",
        "indirizzo_o_zona": "Adresa / lokalita",
        "superficie_mq": "Úžitková plocha (m²)",
        "locali": "Počet izieb",
        "camere": "Spálne",
        "bagni": "Kúpeľne",
        "piano": "Poschodie",
        "piani_totali": "Celkový počet poschodí",
        "ascensore": "Výťah",
        "riscaldamento": "Kúrenie",
        "stato_immobile": "Stav nehnuteľnosti",
        "anno_costruzione": "Rok výstavby",
        "esposizione": "Orientácia",
        "spazi_esterni": "Vonkajšie priestory",
        "posto_auto": "Parkovanie",
        "cantina": "Pivnica",
        "arredato": "Zariadenie",
        "prezzo_richiesto": "Požadovaná cena (€)",
        "note_venditore": "Poznámky predávajúceho",
        "energeticka_trieda": "Energetická trieda",
        "energeticky_certifikat_esiste": "Energetický certifikát existuje",
        "mesacne_poplatky": "Mesačné poplatky (€)",
        "druh_vlastnictva": "Druh vlastníctva",
    },
}

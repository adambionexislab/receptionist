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
    "3. Zoznam úloh (tasks) — výslovné záväzky prijaté počas stretnutia, nie\n"
    "   len prediskutované témy.\n"
    "\n"
    "Základné pravidlá:\n"
    "- NIKDY nevymýšľajte právne alebo finančne významné hodnoty (plocha,\n"
    "  cena, energetická trieda, druh vlastníctva, katastrálne údaje). Ak to\n"
    "  nebolo jasne povedané, ponechajte pole null: to je očakávané\n"
    "  správanie, nie chyba.\n"
    "- Hodnoty uveďte presne tak, ako boli povedané; nezaokrúhľujte ani\n"
    "  neodhadujte.\n"
    "- Úlohu vytvorte LEN pre výslovný záväzok niektorej zo strán — typické\n"
    "  signály: 'pošlem', 'skontrolujem', 'zavolám', 'prinesiem'.\n"
    "  Nevytvárajte úlohu pre každú prediskutovanú tému. Správne priraďte\n"
    "  owner ('agente' alebo 'venditore').\n"
    "- Nastavte blocca_pubblicazione=true pre každú úlohu, ktorá blokuje\n"
    "  zverejnenie inzerátu (napr. chýbajúca energetická trieda).\n"
    "- Pre každú úlohu musí byť citazione krátky doslovný úryvok z prepisu,\n"
    "  ktorý danú úlohu odôvodňuje (alebo null, ak sa neuplatňuje).\n"
    "- listing_text a opisy úloh musia byť napísané celé po slovensky, aj\n"
    "  keď prepis obsahuje výrazy v inom jazyku.\n"
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
    "email_subject": "Získanie — {address}",
    "email_intro": "Zhrnutie stretnutia s predávajúcim.",
    "email_section_missing": "=== Chýbajúce údaje (na doplnenie) ===",
    "email_none_missing": "Žiadne: všetky povinné údaje sú vyplnené.",
    "email_section_tasks": "=== Úlohy ===",
    "email_no_tasks": "Počas stretnutia neboli zaznamenané žiadne záväzky.",
    "email_owner_agente": "Maklér",
    "email_owner_venditore": "Predávajúci",
    "email_task_blocking": "BLOKUJE ZVEREJNENIE",
    "email_task_due": "termín",
    "email_section_listing_text": "=== Opis inzerátu ===",
    "email_section_fields": "=== Údaje o nehnuteľnosti ===",
    "email_section_transcript": "=== Prepis stretnutia ===",
    "email_no_text": "(žiadny opis)",
    "email_yes": "áno",
    "email_no": "nie",
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

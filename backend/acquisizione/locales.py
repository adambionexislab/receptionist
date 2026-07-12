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
)

SK: dict[str, str] = {
    "extraction_instructions": _SK_INSTRUCTIONS,
}

"""Prompt additions that only the ElevenLabs engine needs.

The session prompt itself is Render's (call/locales.py, shared with the OpenAI
SIP path). What is added here exists because her text is read by a TTS, not
spoken by the model: on the SIP path gpt-realtime says the numbers itself.

Numbers: a digit string read by ElevenLabs drifts into Czech (Phase 0), so
digits were spelled out by sk_numbers. But that can only produce the basic
form: "so 6 izbami" became "so šesť izbami" instead of "so šiestimi izbami".
The model knows the sentence, so it writes the numbers that need grammatical
agreement (counts, ordinals, prices, times, dates) as words, correctly
inflected; sk_numbers stays as the safety net for any digit that slips through.

PHONE NUMBERS ARE THE EXCEPTION: she writes them in digits, exactly as she puts
them in the tool, and sk_numbers reads them out digit by digit. Her read-back
is only worth something if the caller hears the very digits that were stored;
a number she wrote out in words herself could differ from the tool's digits
without anyone noticing.
"""

_NUMBERS_AS_WORDS = {
    "sk": (
        "\n\n# Čísla píšte slovami — okrem telefónnych čísel\n"
        "Váš text číta nahlas hlasový syntetizátor. Preto čísla, ktoré sa vo\n"
        "vete skloňujú, napíšte slovami, nie číslicami, a v správnom\n"
        "gramatickom tvare podľa vety (pád, rod, číslo):\n"
        "- počty: 'dom so šiestimi izbami', 'v byte s tromi izbami',\n"
        "  'dvojizbový byt', 'šesťdesiat metrov štvorcových';\n"
        "- poradové čísla: 'na treťom poschodí';\n"
        "- ceny: 'za stoosemdesiatdeväťtisíc eur', 'do dvestotisíc eur';\n"
        "- čas a dátum: 'v utorok o štrnástej tridsať', 'dvadsiateho októbra'.\n"
        "TELEFÓNNE ČÍSLA píšte VŽDY ČÍSLICAMI, presne tak, ako ich zapisujete\n"
        "do nástroja, napr. '0903 123 456', nikdy nie slovami. Prečítajú sa\n"
        "po cifrách automaticky, takže volajúci počuje presne tie cifry, ktoré\n"
        "ste si zapísali.\n"
        "Toto platí len pre to, čo hovoríte. Do nástrojov zapisujte čísla\n"
        "číslicami, ako doteraz."
    ),
}


def engine_additions(locale: str) -> str:
    """Text appended to the session prompt for this engine, in the tenant's
    locale. Empty for a locale without additions (the spell-out safety net
    then does what it can)."""
    return _NUMBERS_AS_WORDS.get(locale, "")

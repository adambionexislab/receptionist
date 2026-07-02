"""Fallback sample listings used when a tenant has no real listings source
(the GitHub CSV / Immobiliare.it scrape). The Italian demo tenant and any
seed-only tenant share this data, keyed by locale.

The `type` field stays an internal token ("vendita"/"affitto") in every locale:
it's never spoken to the caller (the agent describes the property in the
caller's language), and keeping it locale-independent lets the search_listings
tool schema stay the same for every tenant.
"""

_SEED_IT = [
    {
        "address": "Via Roma 12, Lodi",
        "zone": "Lodi",
        "type": "affitto",
        "rooms": 2,
        "size_sqm": 55,
        "price": 750,
        "currency": "EUR",
        "available": True,
        "text": "Piano secondo, ascensore, posto auto incluso",
    },
    {
        "address": "Via Mazzini 45, Mulazzano",
        "zone": "Mulazzano",
        "type": "vendita",
        "rooms": 3,
        "size_sqm": 85,
        "price": 180000,
        "currency": "EUR",
        "available": True,
        "text": "Ottimo stato, cantina e box auto",
    },
    {
        "address": "Via Emilia 100, San Donato Milanese",
        "zone": "Milano Sud",
        "type": "affitto",
        "rooms": 3,
        "size_sqm": 120,
        "price": 1200,
        "currency": "EUR",
        "available": True,
        "text": "Ufficio open space, parcheggio privato",
    },
    {
        "address": "Corso Adda 8, Lodi",
        "zone": "Lodi",
        "type": "vendita",
        "rooms": 4,
        "size_sqm": 130,
        "price": 320000,
        "currency": "EUR",
        "available": True,
        "text": "Appartamento ristrutturato, terrazza, doppi servizi",
    },
    {
        "address": "Via Garibaldi 3, Lodi",
        "zone": "Lodi",
        "type": "affitto",
        "rooms": 1,
        "size_sqm": 35,
        "price": 550,
        "currency": "EUR",
        "available": True,
        "text": "Monolocale arredato, ideale per studenti",
    },
    {
        "address": "Via Torino 22, Mulazzano",
        "zone": "Mulazzano",
        "type": "vendita",
        "rooms": 2,
        "size_sqm": 60,
        "price": 120000,
        "currency": "EUR",
        "available": True,
        "text": "Bilocale piano terra con giardino privato",
    },
    {
        "address": "Via dei Mille 17, Peschiera Borromeo",
        "zone": "Milano Sud",
        "type": "affitto",
        "rooms": 3,
        "size_sqm": 80,
        "price": 1100,
        "currency": "EUR",
        "available": True,
        "text": "Trilocale luminoso, vicino fermata MM3",
    },
    {
        "address": "Via della Pace 5, Mulazzano",
        "zone": "Mulazzano",
        "type": "vendita",
        "rooms": 5,
        "size_sqm": 200,
        "price": 450000,
        "currency": "EUR",
        "available": True,
        "text": "Villetta a schiera con giardino, garage doppio",
    },
]

# Slovak demo listings. Real Slovak addresses/zones, EUR (Slovakia uses the
# euro). The `text` description is in Slovak so the agent can answer detail
# questions naturally; `type` stays the internal vendita/affitto token.
_SEED_SK = [
    {
        "address": "Obchodná 12, Bratislava",
        "zone": "Bratislava - Staré Mesto",
        "type": "affitto",
        "rooms": 2,
        "size_sqm": 55,
        "price": 780,
        "currency": "EUR",
        "available": True,
        "text": "Dvojizbový byt na druhom poschodí, výťah, pivnica, zariadený",
    },
    {
        "address": "Hlavná 45, Košice",
        "zone": "Košice - Staré Mesto",
        "type": "vendita",
        "rooms": 3,
        "size_sqm": 85,
        "price": 175000,
        "currency": "EUR",
        "available": True,
        "text": "Trojizbový byt v dobrom stave, pivnica a parkovacie miesto",
    },
    {
        "address": "Námestie SNP 8, Banská Bystrica",
        "zone": "Banská Bystrica - centrum",
        "type": "affitto",
        "rooms": 3,
        "size_sqm": 95,
        "price": 690,
        "currency": "EUR",
        "available": True,
        "text": "Priestranný trojizbový byt blízko centra, balkón, novostavba",
    },
    {
        "address": "Štúrova 3, Nitra",
        "zone": "Nitra",
        "type": "vendita",
        "rooms": 4,
        "size_sqm": 110,
        "price": 230000,
        "currency": "EUR",
        "available": True,
        "text": "Štvorizbový byt po rekonštrukcii, terasa, dve kúpeľne",
    },
    {
        "address": "Sasinkova 17, Žilina",
        "zone": "Žilina",
        "type": "affitto",
        "rooms": 1,
        "size_sqm": 34,
        "price": 480,
        "currency": "EUR",
        "available": True,
        "text": "Zariadený garsónka, ideálne pre študentov, blízko centra",
    },
    {
        "address": "Hviezdoslavova 22, Trnava",
        "zone": "Trnava",
        "type": "vendita",
        "rooms": 2,
        "size_sqm": 58,
        "price": 138000,
        "currency": "EUR",
        "available": True,
        "text": "Dvojizbový byt na prízemí so súkromnou záhradou",
    },
    {
        "address": "Hurbanova 7, Trenčín",
        "zone": "Trenčín",
        "type": "affitto",
        "rooms": 3,
        "size_sqm": 78,
        "price": 650,
        "currency": "EUR",
        "available": True,
        "text": "Svetlý trojizbový byt, blízko zastávky MHD, balkón",
    },
    {
        "address": "Záhradnícka 5, Bratislava",
        "zone": "Bratislava - Ružinov",
        "type": "vendita",
        "rooms": 5,
        "size_sqm": 190,
        "price": 420000,
        "currency": "EUR",
        "available": True,
        "text": "Radový rodinný dom so záhradou a dvojgarážou",
    },
]

_SEED_BY_LOCALE = {"it": _SEED_IT, "sk": _SEED_SK}


def get_seed_listings(locale: str = "it") -> list[dict]:
    """Return a fresh copy of the seed listings for the given locale, falling
    back to the Italian set for any unknown locale."""
    listings = _SEED_BY_LOCALE.get(locale, _SEED_IT)
    return [dict(item) for item in listings]

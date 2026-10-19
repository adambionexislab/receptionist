"""Slovak number spell-out (Phase 0: digits read by the model drift into Czech)."""

import pytest

from voiceagent.sk_numbers import SlovakNumberStream, cardinal, spell_numbers

CASES = {
    "Vaše číslo je 0903 123 456, je to správne?":
        "Vaše číslo je nula deväť nula tri, jedna dva tri, štyri päť šesť, je to správne?",
    "Kontaktné číslo plus 421 948 155 064, je to tak?":
        "Kontaktné číslo plus štyri dva jedna, deväť štyri osem, jedna päť päť, nula šesť štyri, je to tak?",
    "Volajte +421948155064.":
        "Volajte plus štyri dva jedna, deväť štyri osem, jedna päť päť, nula šesť štyri.",
    "Číslo 0903123456.":
        "Číslo nula deväť nula tri, jedna dva tri, štyri päť šesť.",
    "Byt na Pezinskej ulici 12 v Pezinku stojí 189 000 eur.":
        "Byt na Pezinskej ulici dvanásť v Pezinku stojí sto osemdesiat deväť tisíc eur.",
    "Trojizbový byt v Pezinku, 72 metrov štvorcových, cena 189 000 €.":
        "Trojizbový byt v Pezinku, sedemdesiat dva metrov štvorcových, cena sto osemdesiat deväť tisíc eur.",
    "cena je 245 500 eur": "cena je dvesto štyridsať päť tisíc päťsto eur",
    "za 850 eur mesačne": "za osemsto päťdesiat eur mesačne",
    "v utorok o 14:30.": "v utorok o štrnástej tridsať.",
    "o 9:05": "o deviatej nula päť",
    "do 200 tisíc eur": "do dvesto tisíc eur",
    "650 m²": "šesťsto päťdesiat metrov štvorcových",
    "2,5 izby": "dve celé päť izby",
    "1 250 000 eur": "milión dvesto päťdesiat tisíc eur",
    "22 000": "dvadsať dva tisíc",
    "125 000 000": "sto dvadsať päť miliónov",
    "číslo 5.": "číslo päť.",
    "189000€": "sto osemdesiat deväť tisíc eur",
    "Text bez čísel ostáva, ako je.": "Text bez čísel ostáva, ako je.",
}


@pytest.mark.parametrize("src, want", CASES.items())
def test_spell_numbers(src, want):
    assert spell_numbers(src) == want


@pytest.mark.parametrize("src", CASES)
@pytest.mark.parametrize("size", [1, 2, 3, 5, 8])
def test_streaming_gives_the_same_text_however_it_is_cut(src, size):
    stream = SlovakNumberStream()
    out = "".join(stream.feed(src[i:i + size]) for i in range(0, len(src), size))
    assert out + stream.finish() == spell_numbers(src)


def test_cardinals():
    assert cardinal(0) == "nula"
    assert cardinal(1000) == "tisíc"
    assert cardinal(2_000_000) == "dva milióny"
    assert cardinal(5_000_000) == "päť miliónov"

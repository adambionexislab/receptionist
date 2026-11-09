"""The shared legal pages (legal/): every document is served at the address the
documents themselves quote, and links to the others."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import legal
from legal.router import DOCUMENTS, router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize("url, heading", [
    ("/vop", "Všeobecné obchodné podmienky služieb ApollonIA a Querie"),
    ("/zmluva-o-spracuvani-udajov", "Zmluva o spracúvaní osobných údajov"),
    ("/ochrana-osobnych-udajov", "Zásady ochrany osobných údajov"),
    ("/sprostredkovatelia", "Zoznam ďalších sprostredkovateľov"),
])
def test_each_document_is_served(client, url, heading):
    resp = client.get(url)
    assert resp.status_code == 200
    assert f"<h1>{heading}</h1>" in resp.text
    assert f'href="https://www.apollon-ia.com{url}"' in resp.text  # canonical
    for other in DOCUMENTS:
        assert f'href="{other}"' in resp.text


def test_documents_cross_reference_each_other(client):
    assert f'href="{legal.DPA_URL}"' in client.get(legal.TERMS_URL).text
    assert f'href="{legal.SUBPROCESSORS_URL}"' in client.get(legal.DPA_URL).text
    privacy = client.get(legal.PRIVACY_URL).text
    assert f'href="{legal.SUBPROCESSORS_URL}"' in privacy
    assert 'id="cookies"' in privacy  # the Querie cookie banner links to #cookies


def test_texts_have_no_template_leftovers(client):
    for url in DOCUMENTS:
        html = client.get(url).text
        assert "{{" not in html and "{%" not in html

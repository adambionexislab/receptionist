"""The four legal pages. Plain Jinja2 rendering, like querie/router.py; the
texts live in legal/templates/ and change only with a new VERSION."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

import legal

router = APIRouter()

_templates = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

# url → (template, title shown in the page and in the document menu)
DOCUMENTS = {
    legal.TERMS_URL: ("vop.html", "Všeobecné obchodné podmienky"),
    legal.DPA_URL: ("dpa.html", "Zmluva o spracúvaní osobných údajov"),
    legal.PRIVACY_URL: ("privacy.html", "Zásady ochrany osobných údajov"),
    legal.SUBPROCESSORS_URL: ("subprocessors.html", "Zoznam ďalších sprostredkovateľov"),
}


def _page(url: str) -> HTMLResponse:
    template, title = DOCUMENTS[url]
    html = _templates.get_template(template).render(
        current=url, title=title, documents=DOCUMENTS,
        terms_url=legal.TERMS_URL, dpa_url=legal.DPA_URL,
        privacy_url=legal.PRIVACY_URL, subprocessors_url=legal.SUBPROCESSORS_URL,
    )
    return HTMLResponse(html, headers={"Cache-Control": "public, max-age=300"})


def _register(url: str) -> None:
    @router.get(url, response_class=HTMLResponse, include_in_schema=False, name=f"legal:{url}")
    async def page():
        return _page(url)


for _url in DOCUMENTS:
    _register(_url)

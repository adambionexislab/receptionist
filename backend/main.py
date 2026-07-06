import asyncio
import datetime
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from billing.router import router as billing_router
from call.router import router as call_router
from config import settings
from demo.router import router as demo_router
from leadgen import db as leadgen_db
from listings.store import ListingsStore, store, tenant_stores
from routers.leads import router as leads_router
from signup.router import router as signup_router
from tenants import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
# httpx logs every request URL at INFO, which leaks query-string secrets (e.g.
# the Google Places API key) into the logs. Quiet it to WARNING; our own modules
# still log what matters.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_LANDING_DIR = Path(__file__).parent / "landingpage"

_ROME = ZoneInfo("Europe/Rome")
_SYNC_HOURS = (9, 12, 15, 19)


def _seconds_until_next_slot() -> float:
    now = datetime.datetime.now(_ROME)
    today = now.date()
    for hour in _SYNC_HOURS:
        slot = datetime.datetime.combine(today, datetime.time(hour, 0, 0), tzinfo=_ROME)
        if slot > now:
            return (slot - now).total_seconds()
    tomorrow = today + datetime.timedelta(days=1)
    slot = datetime.datetime.combine(tomorrow, datetime.time(_SYNC_HOURS[0], 0, 0), tzinfo=_ROME)
    return (slot - now).total_seconds()


def _startup_migration() -> None:
    """If the tenants table is empty and env vars define the original demo
    number, recreate it as a tenant so the demo keeps working. Either way,
    bind the demo tenant (matched by its Twilio number) to the original
    GitHub-CSV-backed global store."""
    if db.count() == 0 and settings.TWILIO_PHONE_NUMBER:
        db.create(
            agency_name="Studio Demo",
            agent_name="Apollonia",
            twilio_number=settings.TWILIO_PHONE_NUMBER,
            immobiliare_url=settings.IMMOBILIARE_SEARCH_URL,
            lead_email=settings.LEAD_EMAIL or "",
        )
        logger.info("Migrated env-var config into demo tenant 'Studio Demo'")

    if settings.TWILIO_PHONE_NUMBER:
        demo = db.get_by_twilio_number(settings.TWILIO_PHONE_NUMBER)
        if demo:
            tenant_stores.attach(demo["id"], store)


def _setup_sk_demo() -> None:
    """Slovak-language demo tenant, mirroring the Italian demo above.

    When TWILIO_PHONE_NUMBER_SK is set, ensure a locale='sk' "Štúdio Demo"
    tenant exists on that number and bind it to a Slovak seed-listings store
    (no Immobiliare.it scrape — Slovak agencies aren't on immobiliare.it).
    Attach that number to the OpenAI SIP trunk, same as the Italian demo number.
    """
    number = settings.TWILIO_PHONE_NUMBER_SK
    if not number:
        return
    sk = db.get_by_twilio_number(number)
    if sk is None:
        sk = db.create(
            agency_name="Štúdio Demo",
            agent_name="Apollonia",
            twilio_number=number,
            lead_email=settings.LEAD_EMAIL or "",
            locale="sk",
        )
        logger.info("Created Slovak demo tenant 'Štúdio Demo' on %s", number)
    # Attach a Slovak-seeded store so the first listings load serves Slovak
    # examples instead of the Italian seed default.
    tenant_stores.attach(sk["id"], ListingsStore(locale="sk"))


async def _load_all_tenant_listings() -> None:
    tenants = await asyncio.to_thread(db.get_all_active)
    if not tenants:
        # No tenants at all (fresh install without env vars): still load the
        # global store so /listings and the env fallback have data.
        await store.load()
        return
    for tenant in tenants:
        try:
            await tenant_stores.load(tenant["id"], tenant.get("immobiliare_url"))
        except Exception:
            logger.exception("Listings load failed for tenant %s", tenant["id"])


async def _sync_loop() -> None:
    while True:
        delay = _seconds_until_next_slot()
        next_dt = datetime.datetime.now(_ROME) + datetime.timedelta(seconds=delay)
        logger.info("Next listings sync in %.0fs (at %s Rome time)", delay, next_dt.strftime("%H:%M"))
        await asyncio.sleep(delay)
        await _load_all_tenant_listings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(_startup_migration)
    await asyncio.to_thread(_setup_sk_demo)
    await asyncio.to_thread(leadgen_db.init)
    await _load_all_tenant_listings()
    task = asyncio.create_task(_sync_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="AI Voice Receptionist", lifespan=lifespan)

# The landing page is served by this same app (StaticFiles mount below), so
# /session-token is normally a same-origin call. CORS still matters because the
# lead-gen dashboard is deployed as a SEPARATE Render static site (a different
# origin) and calls /campaigns* with GET/POST/PATCH. Origins are opened to "*"
# per the dashboard requirement; none of these endpoints use cookies or other
# credentials, so a wildcard origin carries no extra risk here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(call_router)
app.include_router(signup_router)
app.include_router(billing_router)
app.include_router(demo_router)
app.include_router(leads_router)


class Listing(BaseModel):
    address: str
    zone: str
    type: str
    rooms: int
    size_sqm: int
    price: int
    currency: str
    available: bool
    text: str


@app.get("/listings", response_model=list[Listing])
async def get_listings(
    type: Optional[str] = None,
    zone: Optional[str] = None,
    rooms_min: Optional[int] = None,
    rooms_max: Optional[int] = None,
    max_price: Optional[int] = None,
):
    return store.search(
        type=type,
        zone=zone,
        rooms_min=rooms_min,
        rooms_max=rooms_max,
        max_price=max_price,
    )


@app.post("/listings/reload")
async def reload_listings():
    await store.load()
    return {"status": "ok", "count": len(store._listings)}


@app.get("/admin/tenants")
async def admin_tenants(authorization: str = Header(default="")):
    if not settings.ADMIN_TOKEN or authorization != f"Bearer {settings.ADMIN_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")
    tenants = await asyncio.to_thread(db.get_all_active)
    counts = tenant_stores.counts()
    for tenant in tenants:
        tenant["listing_count"] = counts.get(tenant["id"], 0)
    return tenants


@app.get("/success")
async def success_page():
    # Explicit route so domain/success (Stripe success_url) resolves before the
    # catch-all static mount below.
    return FileResponse(str(_LANDING_DIR / "success.html"))


if _LANDING_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_LANDING_DIR), html=True), name="landing")

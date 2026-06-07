import asyncio
import datetime
import logging
from contextlib import asynccontextmanager
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from pydantic import BaseModel

from call.router import router as call_router
from call.router import setup_twilio_webhook
from listings.store import store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


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


async def _sync_loop() -> None:
    while True:
        delay = _seconds_until_next_slot()
        next_dt = datetime.datetime.now(_ROME) + datetime.timedelta(seconds=delay)
        logger.info("Next listings sync in %.0fs (at %s Rome time)", delay, next_dt.strftime("%H:%M"))
        await asyncio.sleep(delay)
        await store.load()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.load()
    await asyncio.to_thread(setup_twilio_webhook)
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
app.include_router(call_router)


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

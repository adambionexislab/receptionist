import asyncio
import base64
import csv
import io
import logging
from difflib import SequenceMatcher
from typing import Optional

import httpx

from config import settings
from listings.seed import get_seed_listings

logger = logging.getLogger(__name__)

# TEMPORARY: pauses the background Apify scrape (and the GitHub CSV write-back
# it triggers) so listings can be edited by hand without being overwritten.
# Flip back to False to resume automatic syncing.
_APIFY_SYNC_PAUSED = True

_ACTOR_ID = "azzouzana~immobiliare-it-listing-page-scraper-by-search-url"
_POLL_INTERVAL = 5
_RUN_TIMEOUT = 120
_CSV_FIELDS = ["address", "zone", "type", "rooms", "size_sqm", "price", "currency", "available", "text"]


def _safe_int(val, default: int = 0) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _map_apify_item(item: dict) -> dict:
    contract_type = (item.get("contractType") or "").lower()
    listing_type = "affitto" if contract_type in ("affitto", "rent", "rental") else "vendita"
    return {
        "address": item.get("address") or item.get("title", ""),
        "zone": item.get("city") or item.get("zone", ""),
        "type": listing_type,
        "rooms": _safe_int(item.get("rooms")),
        "size_sqm": _safe_int(item.get("surface")),
        "price": _safe_int(item.get("price")),
        "currency": "EUR",
        "available": True,
        "text": item.get("description", "") or item.get("text", "") or item.get("desc", ""),
    }


def _listings_to_csv(listings: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS)
    writer.writeheader()
    for listing in listings:
        writer.writerow({
            "address": listing.get("address", ""),
            "zone": listing.get("zone", ""),
            "type": listing.get("type", ""),
            "rooms": listing.get("rooms", 0),
            "size_sqm": listing.get("size_sqm", 0),
            "price": listing.get("price", 0),
            "currency": listing.get("currency", "EUR"),
            "available": "TRUE" if listing.get("available", True) else "FALSE",
            "text": listing.get("text", ""),
        })
    return buf.getvalue()


class ListingsStore:
    def __init__(self) -> None:
        self._listings: list[dict] = []

    async def load(self) -> None:
        """Phase 1 (fast): load GitHub CSV so the agent is live immediately.
        Phase 2 (background): trigger Apify scrape which updates memory and
        writes the result back to the GitHub CSV cache on success."""
        loaded = await self._load_from_github()
        if not loaded:
            self._listings = get_seed_listings()
            logger.info("Loaded %d seed listings as fallback", len(self._listings))

        if _APIFY_SYNC_PAUSED:
            logger.info("Apify sync is paused (_APIFY_SYNC_PAUSED=True) — skipping background scrape")
        elif settings.APIFY_TOKEN and settings.IMMOBILIARE_SEARCH_URL:
            asyncio.create_task(self._apify_scrape_and_cache())
        elif not settings.APIFY_TOKEN:
            logger.info("APIFY_TOKEN not set — skipping background Apify scrape")
        else:
            logger.warning("IMMOBILIARE_SEARCH_URL not set — skipping background Apify scrape")

    async def _load_from_github(self) -> bool:
        """Load listings from the GitHub CSV cache. Returns True if at least one listing loaded."""
        if not settings.GITHUB_TOKEN:
            logger.info("GITHUB_TOKEN not set — skipping GitHub CSV load")
            return False

        url = (
            f"https://api.github.com/repos/{settings.GITHUB_REPO}"
            f"/contents/{settings.GITHUB_CSV_PATH}"
        )
        headers = {
            "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
            "Accept": "application/vnd.github.raw+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    url,
                    headers=headers,
                    params={"ref": settings.GITHUB_BRANCH},
                )
                response.raise_for_status()
                csv_text = response.text

            listings: list[dict] = []
            reader = csv.DictReader(io.StringIO(csv_text))
            for row in reader:
                available_raw = (row.get("available") or "").strip().upper()
                if available_raw != "TRUE":
                    continue
                listings.append({
                    "address": (row.get("address") or "").strip(),
                    "zone": (row.get("zone") or "").strip().lower(),
                    "type": (row.get("type") or "").strip().lower(),
                    "rooms": _safe_int(row.get("rooms")),
                    "size_sqm": _safe_int(row.get("size_sqm")),
                    "price": _safe_int(row.get("price")),
                    "currency": (row.get("currency") or "EUR").strip() or "EUR",
                    "available": True,
                    "text": (row.get("text") or "").strip(),
                })

            if not listings:
                logger.warning("GitHub CSV loaded but contained no available listings")
                return False

            self._listings = listings
            logger.info("Loaded %d listings from GitHub CSV", len(self._listings))
            return True

        except Exception as exc:
            logger.error("Failed to load listings from GitHub CSV: %s", exc)
            return False

    async def _apify_scrape_and_cache(self) -> None:
        """Run Apify scrape. On success: update memory and write back to GitHub CSV.
        On any failure: log and leave current listings and CSV untouched."""
        apify_headers = {"Authorization": f"Bearer {settings.APIFY_TOKEN}"}

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # Step 1: start the actor run
                run_resp = await client.post(
                    f"https://api.apify.com/v2/acts/{_ACTOR_ID}/runs",
                    headers=apify_headers,
                    json={
                        "startUrl": settings.IMMOBILIARE_SEARCH_URL,
                        "maxItems": 200,
                    },
                )
                if run_resp.is_error:
                    logger.error("Apify start failed %s: %s", run_resp.status_code, run_resp.text)
                run_resp.raise_for_status()
                run_id = run_resp.json()["data"]["id"]
                logger.info("Apify run started: %s", run_id)

                # Step 2: poll until SUCCEEDED / FAILED / timeout
                deadline = asyncio.get_running_loop().time() + _RUN_TIMEOUT
                while True:
                    if asyncio.get_running_loop().time() > deadline:
                        logger.error("Apify run %s timed out after %ds — keeping current listings", run_id, _RUN_TIMEOUT)
                        return
                    await asyncio.sleep(_POLL_INTERVAL)
                    status_resp = await client.get(
                        f"https://api.apify.com/v2/actor-runs/{run_id}",
                        headers=apify_headers,
                    )
                    status_resp.raise_for_status()
                    status = status_resp.json()["data"]["status"]
                    logger.info("Apify run %s status: %s", run_id, status)
                    if status == "SUCCEEDED":
                        break
                    if status == "FAILED":
                        logger.error("Apify run %s failed — keeping current listings", run_id)
                        return

                # Step 3: fetch dataset items
                items_resp = await client.get(
                    f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items",
                    headers=apify_headers,
                )
                items_resp.raise_for_status()
                items = items_resp.json()

            listings = []
            for item in items:
                try:
                    listings.append(_map_apify_item(item))
                except Exception as exc:
                    logger.warning("Skipping malformed Apify listing item: %s", exc)

            if not listings:
                logger.warning("Apify scrape returned no listings — keeping current listings")
                return

            self._listings = listings
            logger.info("Updated %d listings in memory from Apify/Immobiliare.it", len(self._listings))

            await self._write_github_csv(listings)

        except Exception as exc:
            logger.error("Apify scrape failed: %s — keeping current listings", exc)

    async def _write_github_csv(self, listings: list[dict]) -> None:
        """Write listings back to the GitHub CSV cache (requires repo write scope on GITHUB_TOKEN)."""
        if not settings.GITHUB_TOKEN:
            logger.warning("GITHUB_TOKEN not set — cannot write back to GitHub CSV cache")
            return

        url = (
            f"https://api.github.com/repos/{settings.GITHUB_REPO}"
            f"/contents/{settings.GITHUB_CSV_PATH}"
        )
        gh_headers = {
            "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # GET current file SHA — required by GitHub Contents API for updates
                get_resp = await client.get(
                    url,
                    headers=gh_headers,
                    params={"ref": settings.GITHUB_BRANCH},
                )
                get_resp.raise_for_status()
                sha = get_resp.json()["sha"]

                csv_content = _listings_to_csv(listings)
                encoded = base64.b64encode(csv_content.encode()).decode()

                put_resp = await client.put(
                    url,
                    headers=gh_headers,
                    json={
                        "message": "chore: sync listings from Immobiliare.it via Apify",
                        "content": encoded,
                        "sha": sha,
                        "branch": settings.GITHUB_BRANCH,
                    },
                )
                put_resp.raise_for_status()
                logger.info("Wrote %d listings back to GitHub CSV cache", len(listings))

        except Exception as exc:
            logger.error("Failed to write listings back to GitHub CSV: %s", exc)

    def search(
        self,
        type: Optional[str] = None,
        zone: Optional[str] = None,
        rooms_min: Optional[int] = None,
        rooms_max: Optional[int] = None,
        max_price: Optional[int] = None,
    ) -> list[dict]:
        results = []
        for listing in self._listings:
            if type is not None and listing["type"] != type.lower():
                continue
            if zone is not None:
                z = zone.lower()
                lz = listing["zone"].lower()
                zone_words = [w for w in z.split() if len(w) > 3]
                if z not in lz and lz not in z and not any(w in lz for w in zone_words):
                    continue
            if rooms_min is not None and listing["rooms"] < rooms_min:
                continue
            if rooms_max is not None and listing["rooms"] > rooms_max:
                continue
            if max_price is not None and listing["price"] > max_price:
                continue
            results.append(dict(listing))
        return results

    @staticmethod
    def _fuzzy_word_in(word: str, text: str, threshold: float = 0.75) -> bool:
        if word in text:
            return True
        return any(
            SequenceMatcher(None, word, t).ratio() >= threshold
            for t in text.split()
            if len(t) > 3
        )

    def get_by_address(self, address_query: str) -> list[dict]:
        query = address_query.lower().strip()
        words = [w for w in query.split() if len(w) > 3]
        return [
            l for l in self._listings
            if query in l["address"].lower()
            or query in l["zone"].lower()
            or any(
                self._fuzzy_word_in(w, l["address"].lower())
                or self._fuzzy_word_in(w, l["zone"].lower())
                for w in words
            )
        ]


store = ListingsStore()

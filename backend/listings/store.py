import asyncio
import base64
import csv
import io
import logging
import unicodedata
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


# ── Location matching ────────────────────────────────────────────────────────
# Callers name places in whatever grammatical case and with whatever diacritics
# the speech-to-text produced. Slovak inflects place names heavily ("Bratislava"
# → "v Bratislave" → "z Bratislavy") while keeping the stem, so plain substring
# matching misses most real queries. These helpers normalise diacritics and case
# and match on a shared stem, with a fuzzy-ratio fallback.
def _norm(s: str) -> str:
    """Lowercase and strip diacritics ('Košice' → 'kosice')."""
    decomposed = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _stem_match(a: str, b: str) -> bool:
    """True when two already-normalised words share a long-enough common prefix
    that the only difference is a trailing case ending — how Slovak declines
    place names ('bratislava'/'bratislave', 'kosice'/'kosiciach')."""
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n >= 4 and n >= min(len(a), len(b)) - 3


def _word_in(query_word: str, text: str) -> bool:
    """True if a caller's word matches any significant word in `text`, tolerant
    of diacritics and Slovak case endings. Normalises both sides."""
    qn = _norm(query_word)
    if len(qn) <= 3:
        return qn in _norm(text)
    for t in _norm(text).split():
        if len(t) <= 3:
            continue
        if (
            qn in t
            or t in qn
            or _stem_match(qn, t)
            or SequenceMatcher(None, qn, t).ratio() >= 0.8
        ):
            return True
    return False


class ListingsStore:
    def __init__(
        self,
        immobiliare_url: Optional[str] = None,
        use_github_csv: bool = False,
        locale: str = "it",
    ) -> None:
        self._listings: list[dict] = []
        self.immobiliare_url = immobiliare_url
        self.use_github_csv = use_github_csv
        # Which seed set to fall back to (it / sk). Lets a seed-only tenant (e.g.
        # the Slovak demo) serve listings in its own locale.
        self.locale = locale

    async def load(self) -> None:
        """Two loading strategies:

        Demo tenant (use_github_csv=True — the original single-tenant flow):
          Phase 1 (fast): load GitHub CSV so the agent is live immediately.
          Phase 2 (background): trigger Apify scrape which updates memory and
          writes the result back to the GitHub CSV cache on success.
          _APIFY_SYNC_PAUSED applies ONLY here, protecting the hand-edited CSV.

        Regular tenants: scrape their immobiliare_url via Apify directly
        (no GitHub cache); seed data if they have no URL or nothing loaded.
        """
        if self.use_github_csv:
            loaded = await self._load_from_github()
            if not loaded:
                self._listings = get_seed_listings(self.locale)
                logger.info("Loaded %d seed listings as fallback", len(self._listings))

            if _APIFY_SYNC_PAUSED:
                logger.info("Apify sync is paused (_APIFY_SYNC_PAUSED=True) — skipping background scrape")
            elif settings.APIFY_TOKEN and settings.IMMOBILIARE_SEARCH_URL:
                asyncio.create_task(
                    self._apify_scrape(settings.IMMOBILIARE_SEARCH_URL, write_github=True)
                )
            elif not settings.APIFY_TOKEN:
                logger.info("APIFY_TOKEN not set — skipping background Apify scrape")
            else:
                logger.warning("IMMOBILIARE_SEARCH_URL not set — skipping background Apify scrape")
            return

        if not self.immobiliare_url:
            if not self._listings:
                self._listings = get_seed_listings(self.locale)
                logger.info("Tenant has no immobiliare_url — loaded %d seed listings", len(self._listings))
            return

        if not settings.APIFY_TOKEN:
            logger.warning("APIFY_TOKEN not set — cannot scrape %s", self.immobiliare_url)
        else:
            await self._apify_scrape(self.immobiliare_url, write_github=False)

        if not self._listings:
            self._listings = get_seed_listings(self.locale)
            logger.info("Scrape yielded nothing — loaded %d seed listings as fallback", len(self._listings))

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

    async def _apify_scrape(self, start_url: str, write_github: bool) -> None:
        """Run Apify scrape of start_url. On success: update memory and, for
        the demo tenant (write_github=True), write back to the GitHub CSV.
        On any failure: log and leave current listings and CSV untouched."""
        apify_headers = {"Authorization": f"Bearer {settings.APIFY_TOKEN}"}

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # Step 1: start the actor run
                run_resp = await client.post(
                    f"https://api.apify.com/v2/acts/{_ACTOR_ID}/runs",
                    headers=apify_headers,
                    json={
                        "startUrl": start_url,
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

            if write_github:
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
                # Match the caller's zone against the listing zone plus the city
                # part of the address (segment after the last comma), so a city
                # named only in the address still matches — while not matching
                # street names that merely share a city's name (an Italian "Via
                # Roma" must not match a search for the city Roma). Matching
                # ignores diacritics and tolerates Slovak case endings
                # ("v Bratislave"/"Bratislavy" ≈ the stored "Bratislava").
                addr = listing["address"]
                city = addr.rsplit(",", 1)[-1] if "," in addr else ""
                hay = f"{listing['zone']} {city}"
                if _norm(zone) not in _norm(hay) and not any(
                    _word_in(w, hay) for w in zone.split() if len(w) > 3
                ):
                    continue
            if rooms_min is not None and listing["rooms"] < rooms_min:
                continue
            if rooms_max is not None and listing["rooms"] > rooms_max:
                continue
            if max_price is not None and listing["price"] > max_price:
                continue
            results.append(dict(listing))
        return results

    def get_by_address(self, address_query: str) -> list[dict]:
        query = address_query.strip()
        qn = _norm(query)
        words = [w for w in query.split() if len(w) > 3]
        return [
            l for l in self._listings
            if qn in _norm(l["address"])
            or qn in _norm(l["zone"])
            or any(
                _word_in(w, l["address"]) or _word_in(w, l["zone"])
                for w in words
            )
        ]


class TenantListingsStore:
    """One in-memory ListingsStore per tenant, keyed by tenant_id."""

    def __init__(self) -> None:
        self._stores: dict[str, ListingsStore] = {}

    def attach(self, tenant_id: str, listings_store: "ListingsStore") -> None:
        """Bind an existing store (the demo tenant's GitHub-CSV store) to a tenant id."""
        self._stores[tenant_id] = listings_store

    def get_or_create(self, tenant_id: str) -> ListingsStore:
        if tenant_id not in self._stores:
            self._stores[tenant_id] = ListingsStore()
        return self._stores[tenant_id]

    async def load(self, tenant_id: str, immobiliare_url: Optional[str] = None) -> None:
        tenant_store = self.get_or_create(tenant_id)
        if not tenant_store.use_github_csv:
            tenant_store.immobiliare_url = immobiliare_url
        await tenant_store.load()

    def counts(self) -> dict[str, int]:
        return {tid: len(s._listings) for tid, s in self._stores.items()}


tenant_stores = TenantListingsStore()

# The original single-tenant store, kept as the demo tenant's store: it loads
# from the hand-edited GitHub CSV and is also what /listings, /listings/reload
# and the env-var fallback call path use.
store = ListingsStore(use_github_csv=True)

import csv
import io
import logging
from typing import Optional

import httpx

from config import settings
from listings.seed import get_seed_listings

logger = logging.getLogger(__name__)


def _safe_int(val, default: int = 0) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


class ListingsStore:
    def __init__(self) -> None:
        self._listings: list[dict] = []

    async def load(self) -> None:
        if not settings.GITHUB_TOKEN:
            logger.warning("GITHUB_TOKEN not set — loading seed data instead of GitHub CSV")
            self._listings = get_seed_listings()
            logger.info("Loaded %d seed listings", len(self._listings))
            return

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
                listings.append(
                    {
                        "address": (row.get("address") or "").strip(),
                        "zone": (row.get("zone") or "").strip().lower(),
                        "type": (row.get("type") or "").strip().lower(),
                        "rooms": _safe_int(row.get("rooms")),
                        "size_sqm": _safe_int(row.get("size_sqm")),
                        "price": _safe_int(row.get("price")),
                        "currency": (row.get("currency") or "EUR").strip() or "EUR",
                        "available": True,
                        "notes": (row.get("notes") or "").strip(),
                    }
                )

            self._listings = listings
            logger.info("Loaded %d listings from GitHub", len(self._listings))

        except Exception as exc:
            logger.error("Failed to sync listings from GitHub: %s", exc)
            # Keep the previous listings intact — never wipe good data on a bad sync

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
            if zone is not None and zone.lower() not in listing["zone"].lower():
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
        query = address_query.lower().strip()
        return [
            l for l in self._listings
            if query in l["address"].lower() or
               any(word in l["address"].lower() for word in query.split() if len(word) > 3)
        ]


store = ListingsStore()

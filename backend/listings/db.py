"""SQLite persistence for tenant listings.

Until this module shipped, listings lived only in memory and were wholesale
replaced on every Apify scrape (see listings/store.py). That was fine when the
portal scrape was the only source of truth, but the agency dashboard now lets
an agent edit and delete listings, and the Acquisizione tool creates new ones
— none of which may be silently reverted by the next scrape.

So listings are persisted here, on the SAME connection as the tenants registry
(tenants/db.py), reusing its process-wide connection and write lock, exactly
like calls/db.py and acquisizione/db.py. Every row carries tenant_id.

Sync semantics (see `replace_scraped`), which exist entirely to stop a scrape
from fighting the agent:

  source='manual'  — created in the dashboard (e.g. an Acquisizione listing).
                     Scrapes never touch these.
  source='scrape'  — came from Apify/the CSV cache, identified by `source_key`
                     (its normalised address) so the same portal listing maps
                     to the same row across runs.
      · edited=1   — the agent has changed it, so the agent owns it now: a
                     scrape neither overwrites its fields nor removes it when
                     it disappears from the portal.
      · deleted=1  — the agent deleted it: a later scrape must not resurrect
                     it, so the row is kept as a tombstone rather than
                     actually removed.
      · otherwise  — freely overwritten by the scrape, and removed when it no
                     longer appears in the portal results.
"""

import datetime
import logging
import unicodedata
import uuid
from typing import Any, Optional

from tenants import db as _tenants_db

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  id          TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL,
  source      TEXT NOT NULL DEFAULT 'scrape',   -- scrape | manual
  source_key  TEXT,                             -- normalised address, scrape identity
  address     TEXT NOT NULL DEFAULT '',
  zone        TEXT NOT NULL DEFAULT '',
  type        TEXT NOT NULL DEFAULT 'vendita',  -- vendita | affitto
  rooms       INTEGER DEFAULT 0,
  size_sqm    INTEGER DEFAULT 0,
  price       INTEGER DEFAULT 0,
  currency    TEXT NOT NULL DEFAULT 'EUR',
  available   INTEGER NOT NULL DEFAULT 1,
  text        TEXT NOT NULL DEFAULT '',
  edited      INTEGER NOT NULL DEFAULT 0,
  deleted     INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT,
  updated_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_listings_tenant ON listings(tenant_id, deleted);
CREATE UNIQUE INDEX IF NOT EXISTS idx_listings_source_key
  ON listings(tenant_id, source_key) WHERE source_key IS NOT NULL;
"""

# Columns an agent may set from the dashboard. Anything else (id, tenant_id,
# source, flags, timestamps) is owned by this module.
EDITABLE_FIELDS = (
    "address", "zone", "type", "rooms", "size_sqm", "price", "currency",
    "available", "text",
)

_initialized = False


def init() -> None:
    """Create the listings table on the shared connection (idempotent)."""
    global _initialized
    conn = _tenants_db.get_connection()
    if _initialized:
        return
    with _tenants_db.write_lock:
        if not _initialized:
            conn.executescript(_SCHEMA)
            conn.commit()
            _initialized = True
            logger.info("Listings table ready")


def _conn():
    if not _initialized:
        init()
    return _tenants_db.get_connection()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def source_key(address: str) -> str:
    """Stable identity for a scraped listing: its address, lowercased with
    diacritics and non-alphanumerics stripped, so trivial formatting changes
    between scrape runs don't create a duplicate row."""
    decomposed = unicodedata.normalize("NFKD", (address or "").lower())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return "".join(c for c in stripped if c.isalnum())


def _row_to_listing(row) -> dict[str, Any]:
    """Shape a DB row like the dicts the rest of the app expects (the phone
    agent's tools and the dashboard both consume this shape)."""
    return {
        "id": row["id"],
        "address": row["address"],
        "zone": row["zone"],
        "type": row["type"],
        "rooms": row["rooms"],
        "size_sqm": row["size_sqm"],
        "price": row["price"],
        "currency": row["currency"],
        "available": bool(row["available"]),
        "text": row["text"],
        "source": row["source"],
        "edited": bool(row["edited"]),
    }


def list_for_tenant(tenant_id: str, available_only: bool = True) -> list[dict[str, Any]]:
    """Every live (non-deleted) listing for ONE tenant. Strictly scoped by
    tenant_id — this is what both the dashboard and the phone agent read."""
    sql = "SELECT * FROM listings WHERE tenant_id = ? AND deleted = 0"
    if available_only:
        sql += " AND available = 1"
    sql += " ORDER BY created_at DESC"
    return [_row_to_listing(r) for r in _conn().execute(sql, (tenant_id,)).fetchall()]


def get(listing_id: str, tenant_id: str) -> Optional[dict[str, Any]]:
    row = _conn().execute(
        "SELECT * FROM listings WHERE id = ? AND tenant_id = ? AND deleted = 0",
        (listing_id, tenant_id),
    ).fetchone()
    return _row_to_listing(row) if row else None


def create_manual(tenant_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Insert an agency-entered listing (e.g. from a confirmed Acquisizione).
    Marked source='manual' so no scrape will ever overwrite or remove it."""
    now = _now()
    row = {
        "id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "source": "manual",
        "source_key": None,
        "address": fields.get("address") or "",
        "zone": fields.get("zone") or "",
        "type": (fields.get("type") or "vendita").lower(),
        "rooms": int(fields.get("rooms") or 0),
        "size_sqm": int(fields.get("size_sqm") or 0),
        "price": int(fields.get("price") or 0),
        "currency": fields.get("currency") or "EUR",
        "available": 1 if fields.get("available", True) else 0,
        "text": fields.get("text") or "",
        "edited": 0,
        "deleted": 0,
        "created_at": now,
        "updated_at": now,
    }
    conn = _conn()
    with _tenants_db.write_lock:
        conn.execute(
            "INSERT INTO listings (id, tenant_id, source, source_key, address, zone, "
            " type, rooms, size_sqm, price, currency, available, text, edited, deleted, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(row.values()),
        )
        conn.commit()
    logger.info("Manual listing created for tenant %s: %s", tenant_id, row["address"])
    return _row_to_listing(row)


def update(listing_id: str, tenant_id: str, fields: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Apply an agent's edits. Sets edited=1, which makes a scraped row immune
    to being overwritten or removed by future scrapes (see module docstring).
    Unknown keys are ignored rather than trusted into the SQL."""
    updates = {k: v for k, v in fields.items() if k in EDITABLE_FIELDS}
    if not updates:
        return get(listing_id, tenant_id)
    if "type" in updates:
        updates["type"] = (updates["type"] or "vendita").lower()
    if "available" in updates:
        updates["available"] = 1 if updates["available"] else 0
    for int_field in ("rooms", "size_sqm", "price"):
        if int_field in updates:
            try:
                updates[int_field] = int(updates[int_field] or 0)
            except (TypeError, ValueError):
                updates[int_field] = 0

    assignments = ", ".join(f"{k} = ?" for k in updates)
    params = list(updates.values()) + [_now(), listing_id, tenant_id]
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            f"UPDATE listings SET {assignments}, edited = 1, updated_at = ? "
            "WHERE id = ? AND tenant_id = ? AND deleted = 0",
            params,
        )
        conn.commit()
    if cur.rowcount == 0:
        return None
    return get(listing_id, tenant_id)


def delete(listing_id: str, tenant_id: str) -> bool:
    """Soft-delete. Kept as a tombstone rather than removed so a later scrape
    can't resurrect a listing the agent deliberately took down."""
    conn = _conn()
    with _tenants_db.write_lock:
        cur = conn.execute(
            "UPDATE listings SET deleted = 1, updated_at = ? "
            "WHERE id = ? AND tenant_id = ? AND deleted = 0",
            (_now(), listing_id, tenant_id),
        )
        conn.commit()
    return cur.rowcount > 0


def replace_scraped(tenant_id: str, scraped: list[dict[str, Any]]) -> None:
    """Reconcile a fresh scrape into this tenant's listings.

    This is the merge that replaced the old "wipe and reinsert everything"
    behaviour. Manual listings are never touched; agent-edited and
    agent-deleted scraped rows are respected (see the module docstring for the
    full rule set).
    """
    if not scraped:
        # Defensive: an empty scrape must never be read as "the agency has no
        # listings any more" and wipe the tenant's inventory. Callers already
        # guard this, but the cost of being wrong here is losing live data.
        logger.warning("replace_scraped called with no items for tenant %s — ignoring", tenant_id)
        return

    now = _now()
    conn = _conn()
    existing = {
        r["source_key"]: r
        for r in conn.execute(
            "SELECT * FROM listings WHERE tenant_id = ? AND source = 'scrape'",
            (tenant_id,),
        ).fetchall()
        if r["source_key"]
    }

    seen: set[str] = set()
    inserted = updated = skipped = 0

    with _tenants_db.write_lock:
        for item in scraped:
            key = source_key(item.get("address", ""))
            if not key:
                continue
            seen.add(key)
            row = existing.get(key)

            if row is None:
                conn.execute(
                    "INSERT INTO listings (id, tenant_id, source, source_key, address, "
                    " zone, type, rooms, size_sqm, price, currency, available, text, "
                    " edited, deleted, created_at, updated_at) "
                    "VALUES (?, ?, 'scrape', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)",
                    (
                        str(uuid.uuid4()), tenant_id, key,
                        item.get("address", ""), item.get("zone", ""),
                        (item.get("type") or "vendita").lower(),
                        int(item.get("rooms") or 0), int(item.get("size_sqm") or 0),
                        int(item.get("price") or 0), item.get("currency") or "EUR",
                        1 if item.get("available", True) else 0,
                        item.get("text", ""), now, now,
                    ),
                )
                inserted += 1
                continue

            # The agent owns any row they've edited or deleted — leave it be.
            if row["edited"] or row["deleted"]:
                skipped += 1
                continue

            conn.execute(
                "UPDATE listings SET address = ?, zone = ?, type = ?, rooms = ?, "
                " size_sqm = ?, price = ?, currency = ?, available = ?, text = ?, "
                " updated_at = ? WHERE id = ?",
                (
                    item.get("address", ""), item.get("zone", ""),
                    (item.get("type") or "vendita").lower(),
                    int(item.get("rooms") or 0), int(item.get("size_sqm") or 0),
                    int(item.get("price") or 0), item.get("currency") or "EUR",
                    1 if item.get("available", True) else 0,
                    item.get("text", ""), now, row["id"],
                ),
            )
            updated += 1

        # Scraped rows that vanished from the portal: drop them, unless the
        # agent has edited them (then they're the agent's, not the portal's).
        gone = [
            r["id"] for key, r in existing.items()
            if key not in seen and not r["edited"] and not r["deleted"]
        ]
        for listing_id in gone:
            conn.execute("DELETE FROM listings WHERE id = ?", (listing_id,))

        conn.commit()

    logger.info(
        "Scrape merged for tenant %s: +%d new, %d updated, %d agent-owned kept, %d removed",
        tenant_id, inserted, updated, skipped, len(gone),
    )


def count(tenant_id: str) -> int:
    row = _conn().execute(
        "SELECT COUNT(*) AS c FROM listings "
        "WHERE tenant_id = ? AND deleted = 0 AND available = 1",
        (tenant_id,),
    ).fetchone()
    return int(row["c"] or 0)

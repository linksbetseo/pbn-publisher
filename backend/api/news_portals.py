"""
News Portals API — manage news aggregation portals linked to PBN domains.

Features:
- Portal CRUD (linked to my_domains via my_domain_id)
- RSS/URL source management per portal
- Fetch & cluster news items from sources
- AI rewrite into unique drafts (review queue)
- Approve/reject/edit drafts, publish to WordPress

Endpoints:
- GET    /api/news-portals/stats                        — global stats
- GET    /api/news-portals/                             — list portals
- POST   /api/news-portals/                             — create portal
- GET    /api/news-portals/{id}                         — portal detail + stats
- PUT    /api/news-portals/{id}                         — update portal
- DELETE /api/news-portals/{id}                         — delete portal (cascade)
- GET    /api/news-portals/{portal_id}/sources          — list sources
- POST   /api/news-portals/{portal_id}/sources          — add source
- PUT    /api/news-portals/sources/{id}                 — update source
- DELETE /api/news-portals/sources/{id}                 — delete source
- POST   /api/news-portals/{portal_id}/fetch            — fetch & cluster news
- GET    /api/news-portals/{portal_id}/drafts           — list drafts (paginated)
- POST   /api/news-portals/{portal_id}/generate         — AI rewrite cluster -> draft
- POST   /api/news-portals/drafts/{id}/approve          — publish draft to WP
- POST   /api/news-portals/drafts/{id}/reject           — reject draft
- PUT    /api/news-portals/drafts/{id}                  — edit draft
"""

import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import List, Optional

import aiosqlite
import httpx
from fastapi import APIRouter, HTTPException, Query
from openai import AsyncOpenAI
from pydantic import BaseModel

from config import DB_PATH, OPENAI_API_KEY
from services.crypto_service import get_plain_password
from services.wordpress_service import publish_post

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/news-portals", tags=["news-portals"])

_openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ---------------------------------------------------------------------------
# DB table creation
# ---------------------------------------------------------------------------

_tables_ensured = False


async def ensure_tables():
    global _tables_ensured
    if _tables_ensured:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS news_portals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                my_domain_id INTEGER NOT NULL REFERENCES my_domains(id) ON DELETE CASCADE,
                niche TEXT NOT NULL DEFAULT '',
                editorial_prompt TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT 'pl',
                auto_publish INTEGER DEFAULT 0,
                posts_per_day INTEGER DEFAULT 5,
                check_interval_min INTEGER DEFAULT 30,
                active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS news_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                portal_id INTEGER NOT NULL REFERENCES news_portals(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'rss',
                active INTEGER DEFAULT 1,
                last_checked_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS news_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES news_sources(id) ON DELETE CASCADE,
                portal_id INTEGER NOT NULL REFERENCES news_portals(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                content TEXT,
                published_at TEXT,
                fingerprint TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(portal_id, fingerprint)
            );

            CREATE TABLE IF NOT EXISTS news_clusters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                portal_id INTEGER NOT NULL REFERENCES news_portals(id) ON DELETE CASCADE,
                label TEXT NOT NULL DEFAULT '',
                item_ids TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'new',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS news_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                portal_id INTEGER NOT NULL REFERENCES news_portals(id) ON DELETE CASCADE,
                cluster_id INTEGER REFERENCES news_clusters(id),
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                excerpt TEXT DEFAULT '',
                source_urls TEXT DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'pending',
                wp_post_url TEXT,
                published_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_news_items_portal ON news_items(portal_id);
            CREATE INDEX IF NOT EXISTS idx_news_items_fingerprint ON news_items(portal_id, fingerprint);
            CREATE INDEX IF NOT EXISTS idx_news_drafts_portal ON news_drafts(portal_id, status);
            CREATE INDEX IF NOT EXISTS idx_news_sources_portal ON news_sources(portal_id);
            CREATE INDEX IF NOT EXISTS idx_news_clusters_portal ON news_clusters(portal_id, status);
        """)
        await db.commit()
    _tables_ensured = True


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class PortalCreate(BaseModel):
    name: str
    my_domain_id: int
    niche: str = ""
    editorial_prompt: str = ""
    language: str = "pl"
    auto_publish: int = 0
    posts_per_day: int = 5
    check_interval_min: int = 30
    active: int = 1


class PortalUpdate(BaseModel):
    name: Optional[str] = None
    niche: Optional[str] = None
    editorial_prompt: Optional[str] = None
    language: Optional[str] = None
    auto_publish: Optional[int] = None
    posts_per_day: Optional[int] = None
    check_interval_min: Optional[int] = None
    active: Optional[int] = None


class SourceCreate(BaseModel):
    name: str
    url: str
    source_type: str = "rss"
    active: int = 1


class SourceUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    source_type: Optional[str] = None
    active: Optional[int] = None


class GenerateRequest(BaseModel):
    cluster_id: int


class DraftUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    excerpt: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Polish stop words for clustering
_STOP_WORDS = frozenset(
    "i w z na do od po za o nie jest to jak ale ze sie ze a an the in on of "
    "for is it to and or the by with that this was are at be have has had "
    "jak jako ze ten ta co ktory ktora gdzie gdy jest sa byl byla bylo "
    "przez przed po pod nad pod dla bez bez jeszcze juz tylko tak nie".split()
)


def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    t = re.sub(r"[^\w\s]", " ", title.lower())
    return re.sub(r"\s+", " ", t).strip()


def _fingerprint(title: str) -> str:
    """MD5 hash of normalized title for deduplication."""
    return hashlib.md5(_normalize_title(title).encode()).hexdigest()


def _title_words(title: str) -> set:
    """Return significant words from title (stop words removed)."""
    words = _normalize_title(title).split()
    return {w for w in words if w not in _STOP_WORDS and len(w) > 2}


def _word_overlap(words_a: set, words_b: set) -> float:
    """Return Jaccard-like overlap ratio based on smaller set."""
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    smaller = min(len(words_a), len(words_b))
    return len(intersection) / smaller if smaller else 0.0


def _parse_rss_xml(xml_text: str) -> list[dict]:
    """Parse RSS/Atom XML and return list of {title, link, description, pubDate}."""
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items

    # Try Atom format first
    atom_ns = "{http://www.w3.org/2005/Atom}"
    atom_entries = root.findall(f".//{atom_ns}entry")
    if atom_entries:
        for entry in atom_entries:
            title_el = entry.find(f"{atom_ns}title")
            link_el = entry.find(f"{atom_ns}link")
            summary_el = entry.find(f"{atom_ns}summary")
            content_el = entry.find(f"{atom_ns}content")
            updated_el = entry.find(f"{atom_ns}updated")
            published_el = entry.find(f"{atom_ns}published")
            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            link = ""
            if link_el is not None:
                link = link_el.get("href", "")
            description = ""
            if content_el is not None and content_el.text:
                description = content_el.text.strip()
            elif summary_el is not None and summary_el.text:
                description = summary_el.text.strip()
            pub_date = ""
            if published_el is not None and published_el.text:
                pub_date = published_el.text.strip()
            elif updated_el is not None and updated_el.text:
                pub_date = updated_el.text.strip()
            if title:
                items.append({
                    "title": title,
                    "link": link,
                    "description": description,
                    "pubDate": pub_date,
                })
        return items

    # RSS 2.0 format
    rss_items = root.findall(".//channel/item")
    if not rss_items:
        # Some feeds wrap differently
        rss_items = root.findall(".//item")
    for item in rss_items:
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        # Try content:encoded as well
        content_encoded = item.find("{http://purl.org/rss/1.0/modules/content/}encoded")
        pub_el = item.find("pubDate")
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link = link_el.text.strip() if link_el is not None and link_el.text else ""
        description = ""
        if content_encoded is not None and content_encoded.text:
            description = content_encoded.text.strip()
        elif desc_el is not None and desc_el.text:
            description = desc_el.text.strip()
        pub_date = pub_el.text.strip() if pub_el is not None and pub_el.text else ""
        if title:
            items.append({
                "title": title,
                "link": link,
                "description": description,
                "pubDate": pub_date,
            })
    return items


def _cluster_items(items: list[dict]) -> list[list[int]]:
    """
    Simple greedy clustering: group items whose titles share >50% significant words.
    Returns list of clusters, each cluster is a list of item IDs.
    """
    if not items:
        return []

    # Build word sets per item
    word_sets = []
    for item in items:
        word_sets.append((_title_words(item["title"]), item["id"]))

    assigned = set()
    clusters = []

    for i, (words_i, id_i) in enumerate(word_sets):
        if id_i in assigned:
            continue
        cluster = [id_i]
        assigned.add(id_i)
        for j, (words_j, id_j) in enumerate(word_sets):
            if j <= i or id_j in assigned:
                continue
            if _word_overlap(words_i, words_j) > 0.5:
                cluster.append(id_j)
                assigned.add(id_j)
        clusters.append(cluster)

    return clusters


# ---------------------------------------------------------------------------
# Stats endpoint (before /{id} to avoid route conflict)
# ---------------------------------------------------------------------------

@router.get("/stats")
async def global_stats():
    """Global news portal statistics."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute("SELECT COUNT(*) as cnt FROM news_portals") as cur:
            total_portals = (await cur.fetchone())["cnt"]

        async with db.execute("SELECT COUNT(*) as cnt FROM news_sources") as cur:
            total_sources = (await cur.fetchone())["cnt"]

        async with db.execute(
            "SELECT COUNT(*) as cnt FROM news_drafts WHERE status = 'pending'"
        ) as cur:
            pending_drafts = (await cur.fetchone())["cnt"]

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        async with db.execute(
            "SELECT COUNT(*) as cnt FROM news_drafts WHERE status = 'published' AND DATE(published_at) = ?",
            (today,)
        ) as cur:
            published_today = (await cur.fetchone())["cnt"]

    return {
        "total_portals": total_portals,
        "total_sources": total_sources,
        "pending_drafts": pending_drafts,
        "published_today": published_today,
    }


# ---------------------------------------------------------------------------
# Portals CRUD
# ---------------------------------------------------------------------------

@router.get("/")
async def list_portals():
    """List all portals with domain name, source count, and draft counts."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT
                np.id, np.name, np.my_domain_id, np.niche, np.language,
                np.auto_publish, np.posts_per_day, np.check_interval_min,
                np.active, np.created_at,
                md.domain AS domain_name,
                (SELECT COUNT(*) FROM news_sources ns WHERE ns.portal_id = np.id) AS source_count,
                (SELECT COUNT(*) FROM news_drafts nd WHERE nd.portal_id = np.id AND nd.status = 'pending') AS pending_drafts,
                (SELECT COUNT(*) FROM news_drafts nd WHERE nd.portal_id = np.id AND nd.status = 'published') AS published_drafts
            FROM news_portals np
            LEFT JOIN my_domains md ON md.id = np.my_domain_id
            ORDER BY np.created_at DESC
        """) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


@router.post("/")
async def create_portal(body: PortalCreate):
    """Create a new news portal."""
    await ensure_tables()
    # Validate that my_domain_id exists
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id FROM my_domains WHERE id = ?", (body.my_domain_id,)) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="Domain not found")

        await db.execute(
            """INSERT INTO news_portals
               (name, my_domain_id, niche, editorial_prompt, language, auto_publish, posts_per_day, check_interval_min, active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (body.name, body.my_domain_id, body.niche, body.editorial_prompt,
             body.language, body.auto_publish, body.posts_per_day, body.check_interval_min, body.active),
        )
        await db.commit()
        portal_id = db.total_changes  # not reliable, use lastrowid via cursor
        async with db.execute("SELECT last_insert_rowid()") as cur:
            row = await cur.fetchone()
            portal_id = row[0]

        async with db.execute("SELECT * FROM news_portals WHERE id = ?", (portal_id,)) as cur:
            portal = await cur.fetchone()
    return dict(portal)


@router.get("/{portal_id}")
async def get_portal(portal_id: int):
    """Get portal detail with sources and stats."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute("""
            SELECT np.*, md.domain AS domain_name
            FROM news_portals np
            LEFT JOIN my_domains md ON md.id = np.my_domain_id
            WHERE np.id = ?
        """, (portal_id,)) as cur:
            portal = await cur.fetchone()
        if not portal:
            raise HTTPException(status_code=404, detail="Portal not found")
        portal_dict = dict(portal)

        async with db.execute(
            "SELECT * FROM news_sources WHERE portal_id = ? ORDER BY created_at DESC",
            (portal_id,)
        ) as cur:
            sources = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            "SELECT COUNT(*) as cnt FROM news_items WHERE portal_id = ?",
            (portal_id,)
        ) as cur:
            total_items = (await cur.fetchone())["cnt"]

        async with db.execute(
            "SELECT COUNT(*) as cnt FROM news_clusters WHERE portal_id = ?",
            (portal_id,)
        ) as cur:
            total_clusters = (await cur.fetchone())["cnt"]

        async with db.execute(
            "SELECT status, COUNT(*) as cnt FROM news_drafts WHERE portal_id = ? GROUP BY status",
            (portal_id,)
        ) as cur:
            draft_counts = {r["status"]: r["cnt"] for r in await cur.fetchall()}

    portal_dict["sources"] = sources
    portal_dict["total_items"] = total_items
    portal_dict["total_clusters"] = total_clusters
    portal_dict["draft_counts"] = draft_counts
    return portal_dict


@router.put("/{portal_id}")
async def update_portal(portal_id: int, body: PortalUpdate):
    """Update portal fields."""
    await ensure_tables()
    updates = []
    params = []
    for field in ("name", "niche", "editorial_prompt", "language", "auto_publish",
                  "posts_per_day", "check_interval_min", "active"):
        value = getattr(body, field)
        if value is not None:
            updates.append(f"{field} = ?")
            params.append(value)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    params.append(portal_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id FROM news_portals WHERE id = ?", (portal_id,)) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="Portal not found")
        await db.execute(
            f"UPDATE news_portals SET {', '.join(updates)} WHERE id = ?", params
        )
        await db.commit()
        async with db.execute("SELECT * FROM news_portals WHERE id = ?", (portal_id,)) as cur:
            portal = await cur.fetchone()
    return dict(portal)


@router.delete("/{portal_id}")
async def delete_portal(portal_id: int):
    """Delete portal and all related data (sources, items, clusters, drafts)."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM news_portals WHERE id = ?", (portal_id,)) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="Portal not found")
        # Manual cascade since SQLite foreign keys may not be enforced
        await db.execute("DELETE FROM news_drafts WHERE portal_id = ?", (portal_id,))
        await db.execute("DELETE FROM news_clusters WHERE portal_id = ?", (portal_id,))
        await db.execute("DELETE FROM news_items WHERE portal_id = ?", (portal_id,))
        await db.execute("DELETE FROM news_sources WHERE portal_id = ?", (portal_id,))
        await db.execute("DELETE FROM news_portals WHERE id = ?", (portal_id,))
        await db.commit()
    return {"deleted": portal_id}


# ---------------------------------------------------------------------------
# Sources CRUD
# ---------------------------------------------------------------------------

@router.get("/{portal_id}/sources")
async def list_sources(portal_id: int):
    """List all sources for a portal."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM news_sources WHERE portal_id = ? ORDER BY created_at DESC",
            (portal_id,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


@router.post("/{portal_id}/sources")
async def create_source(portal_id: int, body: SourceCreate):
    """Add a new source to a portal."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id FROM news_portals WHERE id = ?", (portal_id,)) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="Portal not found")
        await db.execute(
            "INSERT INTO news_sources (portal_id, name, url, source_type, active) VALUES (?, ?, ?, ?, ?)",
            (portal_id, body.name, body.url, body.source_type, body.active),
        )
        await db.commit()
        async with db.execute("SELECT last_insert_rowid()") as cur:
            source_id = (await cur.fetchone())[0]
        async with db.execute("SELECT * FROM news_sources WHERE id = ?", (source_id,)) as cur:
            source = await cur.fetchone()
    return dict(source)


@router.put("/sources/{source_id}")
async def update_source(source_id: int, body: SourceUpdate):
    """Update a source."""
    await ensure_tables()
    updates = []
    params = []
    for field in ("name", "url", "source_type", "active"):
        value = getattr(body, field)
        if value is not None:
            updates.append(f"{field} = ?")
            params.append(value)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    params.append(source_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id FROM news_sources WHERE id = ?", (source_id,)) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="Source not found")
        await db.execute(
            f"UPDATE news_sources SET {', '.join(updates)} WHERE id = ?", params
        )
        await db.commit()
        async with db.execute("SELECT * FROM news_sources WHERE id = ?", (source_id,)) as cur:
            source = await cur.fetchone()
    return dict(source)


@router.delete("/sources/{source_id}")
async def delete_source(source_id: int):
    """Delete a source and its fetched items."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM news_sources WHERE id = ?", (source_id,)) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="Source not found")
        await db.execute("DELETE FROM news_items WHERE source_id = ?", (source_id,))
        await db.execute("DELETE FROM news_sources WHERE id = ?", (source_id,))
        await db.commit()
    return {"deleted": source_id}


# ---------------------------------------------------------------------------
# Fetching & Clustering
# ---------------------------------------------------------------------------

@router.post("/{portal_id}/fetch")
async def fetch_and_cluster(portal_id: int):
    """Fetch news from all active sources, deduplicate, and cluster similar items."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute("SELECT id FROM news_portals WHERE id = ?", (portal_id,)) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="Portal not found")

        async with db.execute(
            "SELECT * FROM news_sources WHERE portal_id = ? AND active = 1",
            (portal_id,)
        ) as cur:
            sources = [dict(r) for r in await cur.fetchall()]

    if not sources:
        return {"new_items": 0, "new_clusters": 0, "errors": ["No active sources"]}

    new_items_total = 0
    fetch_errors = []

    async with httpx.AsyncClient(timeout=20, follow_redirects=True, verify=False) as http:
        for source in sources:
            try:
                resp = await http.get(source["url"])
                resp.raise_for_status()
                feed_items = _parse_rss_xml(resp.text)
                if not feed_items:
                    fetch_errors.append(f"{source['name']}: no items parsed")
                    continue

                async with aiosqlite.connect(DB_PATH) as db:
                    inserted = 0
                    for fi in feed_items:
                        fp = _fingerprint(fi["title"])
                        # Strip HTML from description for cleaner storage
                        desc_clean = re.sub(r"<[^>]+>", " ", fi.get("description", ""))
                        desc_clean = re.sub(r"\s+", " ", desc_clean).strip()
                        try:
                            await db.execute(
                                """INSERT OR IGNORE INTO news_items
                                   (source_id, portal_id, title, url, content, published_at, fingerprint)
                                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                (source["id"], portal_id, fi["title"], fi["link"],
                                 desc_clean[:5000], fi.get("pubDate", ""), fp),
                            )
                            if db.total_changes > 0:
                                inserted += 1
                        except Exception:
                            pass  # UNIQUE constraint — item already exists
                    # Update last_checked_at
                    now_str = datetime.now(timezone.utc).isoformat()
                    await db.execute(
                        "UPDATE news_sources SET last_checked_at = ? WHERE id = ?",
                        (now_str, source["id"]),
                    )
                    await db.commit()
                    new_items_total += inserted
            except httpx.HTTPStatusError as e:
                fetch_errors.append(f"{source['name']}: HTTP {e.response.status_code}")
            except Exception as e:
                fetch_errors.append(f"{source['name']}: {str(e)[:100]}")

    # Cluster unclustered items
    # Get all items for this portal that are not yet in any cluster
    new_clusters_count = 0
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Get IDs already in clusters
        async with db.execute(
            "SELECT item_ids FROM news_clusters WHERE portal_id = ?", (portal_id,)
        ) as cur:
            existing_cluster_rows = await cur.fetchall()
        clustered_ids = set()
        for row in existing_cluster_rows:
            try:
                ids = json.loads(row["item_ids"])
                clustered_ids.update(ids)
            except (json.JSONDecodeError, TypeError):
                pass

        # Get unclustered items
        async with db.execute(
            "SELECT id, title FROM news_items WHERE portal_id = ? ORDER BY created_at DESC LIMIT 500",
            (portal_id,)
        ) as cur:
            all_items = [dict(r) for r in await cur.fetchall()]

        unclustered = [item for item in all_items if item["id"] not in clustered_ids]

        if unclustered:
            clusters = _cluster_items(unclustered)
            for cluster_ids in clusters:
                if len(cluster_ids) < 1:
                    continue
                # Use first item's title as cluster label
                label_item = next((i for i in unclustered if i["id"] == cluster_ids[0]), None)
                label = label_item["title"][:120] if label_item else ""
                await db.execute(
                    "INSERT INTO news_clusters (portal_id, label, item_ids, status) VALUES (?, ?, ?, 'new')",
                    (portal_id, label, json.dumps(cluster_ids)),
                )
                new_clusters_count += 1
            await db.commit()

    return {
        "new_items": new_items_total,
        "new_clusters": new_clusters_count,
        "errors": fetch_errors if fetch_errors else None,
    }


# ---------------------------------------------------------------------------
# Drafts / Review Queue
# ---------------------------------------------------------------------------

@router.get("/{portal_id}/drafts")
async def list_drafts(
    portal_id: int,
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    """List drafts for a portal with pagination and optional status filter."""
    await ensure_tables()
    offset = (page - 1) * per_page
    conditions = ["nd.portal_id = ?"]
    params: list = [portal_id]
    if status:
        conditions.append("nd.status = ?")
        params.append(status)
    where = " AND ".join(conditions)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute(
            f"SELECT COUNT(*) as cnt FROM news_drafts nd WHERE {where}", params
        ) as cur:
            total = (await cur.fetchone())["cnt"]

        query_params = params + [per_page, offset]
        async with db.execute(
            f"""SELECT nd.*, nc.label AS cluster_label
                FROM news_drafts nd
                LEFT JOIN news_clusters nc ON nc.id = nd.cluster_id
                WHERE {where}
                ORDER BY nd.created_at DESC
                LIMIT ? OFFSET ?""",
            query_params,
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    return {
        "items": rows,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if per_page else 1,
    }


@router.post("/{portal_id}/generate")
async def generate_draft(portal_id: int, body: GenerateRequest):
    """Generate a unique article draft from a news cluster using OpenAI."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Load portal
        async with db.execute("SELECT * FROM news_portals WHERE id = ?", (portal_id,)) as cur:
            portal = await cur.fetchone()
        if not portal:
            raise HTTPException(status_code=404, detail="Portal not found")
        portal = dict(portal)

        # Load cluster
        async with db.execute(
            "SELECT * FROM news_clusters WHERE id = ? AND portal_id = ?",
            (body.cluster_id, portal_id)
        ) as cur:
            cluster = await cur.fetchone()
        if not cluster:
            raise HTTPException(status_code=404, detail="Cluster not found")
        cluster = dict(cluster)

        # Load items from cluster
        try:
            item_ids = json.loads(cluster["item_ids"])
        except (json.JSONDecodeError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid cluster item_ids")
        if not item_ids:
            raise HTTPException(status_code=400, detail="Cluster has no items")

        placeholders = ",".join("?" * len(item_ids))
        async with db.execute(
            f"SELECT * FROM news_items WHERE id IN ({placeholders})", item_ids
        ) as cur:
            items = [dict(r) for r in await cur.fetchall()]

    if not items:
        raise HTTPException(status_code=400, detail="No items found for this cluster")

    # Build source text for the prompt
    sources_parts = []
    source_urls = []
    for i, item in enumerate(items, 1):
        part = f"Zrodlo {i}: {item['title']}"
        if item.get("content"):
            # Limit content per source to keep prompt manageable
            part += f"\n{item['content'][:800]}"
        if item.get("url"):
            part += f"\nURL: {item['url']}"
            source_urls.append(item["url"])
        sources_parts.append(part)
    sources_text = "\n\n".join(sources_parts)

    portal_language = portal.get("language", "pl")
    portal_editorial_prompt = portal.get("editorial_prompt", "")

    system_message = portal_editorial_prompt or (
        "Jestes doswiadczonym dziennikarzem. Pisz unikalne, angazujace artykuly newsowe w jezyku polskim."
    )

    user_message = (
        f"Na podstawie ponizszych zrodel napisz unikalny artykul newsowy.\n\n"
        f"Zrodla:\n{sources_text}\n\n"
        f"Wymagania:\n"
        f"- Tytul (zwroc jako pierwsza linie, poprzedzona 'TYTUL: ')\n"
        f"- Lead 2-3 zdania\n"
        f"- Tresc 300-600 slow\n"
        f"- Unikalny tekst, NIE kopiuj zdan ze zrodel\n"
        f"- Jezyk: {portal_language}"
    )

    messages = [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_message},
    ]

    try:
        completion = await _openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.8,
            max_tokens=2000,
        )
        response_text = completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"[NewsPortals] OpenAI error: {e}")
        raise HTTPException(status_code=502, detail=f"OpenAI error: {str(e)[:200]}")

    # Parse title from response
    title = ""
    content = response_text
    lines = response_text.split("\n", 1)
    if lines and lines[0].upper().startswith("TYTUL:"):
        title = lines[0].split(":", 1)[1].strip().strip("\"'")
        content = lines[1].strip() if len(lines) > 1 else ""
    elif lines and lines[0].upper().startswith("TYTUL :"):
        title = lines[0].split(":", 1)[1].strip().strip("\"'")
        content = lines[1].strip() if len(lines) > 1 else ""
    else:
        # Fallback: use first sentence as title
        first_sentence = re.split(r"[.!?\n]", response_text, maxsplit=1)
        title = first_sentence[0].strip()[:150]
        content = response_text

    # Extract excerpt (first 2-3 sentences)
    plain = re.sub(r"<[^>]+>", " ", content)
    plain = re.sub(r"\s+", " ", plain).strip()
    sentences = re.split(r"(?<=[.!?])\s+", plain, maxsplit=3)
    excerpt = " ".join(sentences[:2])[:300]

    # Save draft
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            """INSERT INTO news_drafts
               (portal_id, cluster_id, title, content, excerpt, source_urls, status)
               VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
            (portal_id, body.cluster_id, title, content, excerpt, json.dumps(source_urls)),
        )
        # Mark cluster as processed
        await db.execute(
            "UPDATE news_clusters SET status = 'processed' WHERE id = ?",
            (body.cluster_id,)
        )
        await db.commit()
        async with db.execute("SELECT last_insert_rowid()") as cur:
            draft_id = (await cur.fetchone())[0]
        async with db.execute("SELECT * FROM news_drafts WHERE id = ?", (draft_id,)) as cur:
            draft = await cur.fetchone()

    return dict(draft)


@router.post("/drafts/{draft_id}/approve")
async def approve_draft(draft_id: int):
    """Approve a draft and publish it to the portal's linked WordPress domain."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Load draft
        async with db.execute("SELECT * FROM news_drafts WHERE id = ?", (draft_id,)) as cur:
            draft = await cur.fetchone()
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        draft = dict(draft)
        if draft["status"] == "published":
            raise HTTPException(status_code=400, detail="Draft already published")

        # Load portal
        async with db.execute("SELECT * FROM news_portals WHERE id = ?", (draft["portal_id"],)) as cur:
            portal = await cur.fetchone()
        if not portal:
            raise HTTPException(status_code=404, detail="Portal not found")
        portal = dict(portal)

        # Load domain credentials
        async with db.execute("SELECT * FROM my_domains WHERE id = ?", (portal["my_domain_id"],)) as cur:
            domain = await cur.fetchone()
        if not domain:
            raise HTTPException(status_code=404, detail="Linked domain not found")
        domain = dict(domain)

    # Publish to WordPress
    wp_pass = get_plain_password(domain["wp_pass"])
    result = await publish_post(
        domain=domain["domain"],
        wp_login=domain["wp_login"],
        wp_pass=domain["wp_pass"],  # publish_post handles decryption internally
        title=draft["title"],
        content=draft["content"],
        excerpt=draft.get("excerpt", ""),
        http_user=domain.get("http_user", "") or "",
        http_pass=domain.get("http_pass", "") or "",
    )

    if not result.get("success"):
        raise HTTPException(
            status_code=502,
            detail=f"WordPress publish failed: {result.get('error', 'Unknown error')}"
        )

    # Update draft status
    now_str = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE news_drafts SET status = 'published', wp_post_url = ?, published_at = ? WHERE id = ?",
            (result.get("url", ""), now_str, draft_id),
        )
        await db.commit()

    return {
        "draft_id": draft_id,
        "status": "published",
        "wp_post_url": result.get("url", ""),
        "wp_post_id": result.get("post_id"),
    }


@router.post("/drafts/{draft_id}/reject")
async def reject_draft(draft_id: int):
    """Reject a draft (set status to 'rejected')."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, status FROM news_drafts WHERE id = ?", (draft_id,)) as cur:
            draft = await cur.fetchone()
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        await db.execute(
            "UPDATE news_drafts SET status = 'rejected' WHERE id = ?", (draft_id,)
        )
        await db.commit()
    return {"draft_id": draft_id, "status": "rejected"}


@router.put("/drafts/{draft_id}")
async def edit_draft(draft_id: int, body: DraftUpdate):
    """Edit a draft's title, content, or excerpt before approving."""
    await ensure_tables()
    updates = []
    params = []
    if body.title is not None:
        updates.append("title = ?")
        params.append(body.title)
    if body.content is not None:
        updates.append("content = ?")
        params.append(body.content)
    if body.excerpt is not None:
        updates.append("excerpt = ?")
        params.append(body.excerpt)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    params.append(draft_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, status FROM news_drafts WHERE id = ?", (draft_id,)) as cur:
            draft = await cur.fetchone()
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        if dict(draft)["status"] == "published":
            raise HTTPException(status_code=400, detail="Cannot edit a published draft")
        await db.execute(
            f"UPDATE news_drafts SET {', '.join(updates)} WHERE id = ?", params
        )
        await db.commit()
        async with db.execute("SELECT * FROM news_drafts WHERE id = ?", (draft_id,)) as cur:
            updated = await cur.fetchone()
    return dict(updated)

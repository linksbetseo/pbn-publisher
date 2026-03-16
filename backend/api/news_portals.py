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
- GET    /api/news-portals/published-urls                — list all published URLs
"""

import asyncio
import hashlib
import json
import logging
import random
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import aiosqlite
import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from config import DB_PATH
from services.crypto_service import get_plain_password
from services.wordpress_service import publish_post
from services.openai_service import get_gpt_model, get_openai_client, _fix_heading_hierarchy
from services.article_helpers import (
    markdown_to_html as _markdown_to_html,
    strip_markdown_remnants as _strip_markdown_remnants,
    content_fingerprint as _content_fingerprint,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/news-portals", tags=["news-portals"])

# Limit concurrent article/ToV generation to avoid overloading OpenAI / DataForSEO
_NEWS_GENERATE_SEM = asyncio.Semaphore(3)

# ---------------------------------------------------------------------------
# Predefined RSS Feed Catalog (Polish & international portals)
# ---------------------------------------------------------------------------

RSS_CATALOG = [
    # Polskie portale ogólne
    {"category": "ogólne", "portal": "Onet.pl", "feeds": [
        {"name": "Onet Wiadomości", "url": "https://wiadomosci.onet.pl/.feed"},
        {"name": "Onet Biznes", "url": "https://businessinsider.com.pl/.feed"},
        {"name": "Onet Sport", "url": "https://sport.onet.pl/.feed"},
    ]},
    {"category": "ogólne", "portal": "Wirtualna Polska", "feeds": [
        {"name": "WP Wiadomości", "url": "https://wiadomosci.wp.pl/rss.xml"},
        {"name": "WP Finanse", "url": "https://finanse.wp.pl/rss.xml"},
        {"name": "WP Tech", "url": "https://tech.wp.pl/rss.xml"},
    ]},
    {"category": "ogólne", "portal": "Interia", "feeds": [
        {"name": "Interia Fakty", "url": "https://fakty.interia.pl/feed"},
        {"name": "Interia Biznes", "url": "https://biznes.interia.pl/feed"},
        {"name": "Interia Sport", "url": "https://sport.interia.pl/feed"},
    ]},
    # Wiadomości
    {"category": "wiadomości", "portal": "TVN24", "feeds": [
        {"name": "TVN24 Najnowsze", "url": "https://tvn24.pl/najnowsze.xml"},
        {"name": "TVN24 Polska", "url": "https://tvn24.pl/polska.xml"},
        {"name": "TVN24 Świat", "url": "https://tvn24.pl/swiat.xml"},
        {"name": "TVN24 Biznes", "url": "https://tvn24.pl/biznes/najnowsze.xml"},
    ]},
    {"category": "wiadomości", "portal": "Polsat News", "feeds": [
        {"name": "Polsat News", "url": "https://www.polsatnews.pl/rss/wszystkie.xml"},
    ]},
    {"category": "wiadomości", "portal": "Gazeta.pl", "feeds": [
        {"name": "Gazeta.pl Wiadomości", "url": "https://wiadomosci.gazeta.pl/pub/rss/wiadomosci.xml"},
    ]},
    # Technologia
    {"category": "technologia", "portal": "Dobreprogramy", "feeds": [
        {"name": "Dobreprogramy", "url": "https://www.dobreprogramy.pl/feed"},
    ]},
    {"category": "technologia", "portal": "Benchmark.pl", "feeds": [
        {"name": "Benchmark Aktualności", "url": "https://www.benchmark.pl/rss/aktualnosci.xml"},
    ]},
    {"category": "technologia", "portal": "AntyWeb", "feeds": [
        {"name": "AntyWeb", "url": "https://antyweb.pl/feed"},
    ]},
    {"category": "technologia", "portal": "Spider's Web", "feeds": [
        {"name": "Spider's Web", "url": "https://spidersweb.pl/feed"},
    ]},
    {"category": "technologia", "portal": "Niebezpiecznik", "feeds": [
        {"name": "Niebezpiecznik", "url": "https://niebezpiecznik.pl/feed/"},
    ]},
    {"category": "technologia", "portal": "Komputer Świat", "feeds": [
        {"name": "Komputer Świat", "url": "https://www.komputerswiat.pl/rss"},
    ]},
    # Finanse / Biznes
    {"category": "finanse", "portal": "Bankier.pl", "feeds": [
        {"name": "Bankier Wiadomości", "url": "https://www.bankier.pl/rss/wiadomosci.xml"},
        {"name": "Bankier Giełda", "url": "https://www.bankier.pl/rss/gielda.xml"},
    ]},
    {"category": "finanse", "portal": "Money.pl", "feeds": [
        {"name": "Money.pl", "url": "https://www.money.pl/rss/rss.xml"},
    ]},
    {"category": "biznes", "portal": "PulsHR", "feeds": [
        {"name": "PulsHR", "url": "https://www.pulshr.pl/rss.xml"},
    ]},
    {"category": "biznes", "portal": "Forbes.pl", "feeds": [
        {"name": "Forbes Polska", "url": "https://www.forbes.pl/feed"},
    ]},
    # Sport
    {"category": "sport", "portal": "Sport.pl", "feeds": [
        {"name": "Sport.pl", "url": "https://sport.pl/rss.xml"},
    ]},
    {"category": "sport", "portal": "Meczyki.pl", "feeds": [
        {"name": "Meczyki.pl", "url": "https://www.meczyki.pl/rss"},
    ]},
    {"category": "sport", "portal": "Przegląd Sportowy", "feeds": [
        {"name": "Przegląd Sportowy", "url": "https://www.przegladsportowy.pl/rss.xml"},
    ]},
    # Zdrowie
    {"category": "zdrowie", "portal": "Medonet", "feeds": [
        {"name": "Medonet", "url": "https://www.medonet.pl/rss.xml"},
    ]},
    # Motoryzacja
    {"category": "motoryzacja", "portal": "Autokult", "feeds": [
        {"name": "Autokult", "url": "https://autokult.pl/feed"},
    ]},
    {"category": "motoryzacja", "portal": "Moto.pl", "feeds": [
        {"name": "Moto.pl", "url": "https://moto.pl/rss.xml"},
    ]},
    # Budownictwo / Nieruchomości
    {"category": "budownictwo", "portal": "Murator Dom", "feeds": [
        {"name": "Murator Dom", "url": "https://www.muratordom.pl/rss.xml"},
    ]},
    # Kultura / Rozrywka
    {"category": "kultura", "portal": "Filmweb", "feeds": [
        {"name": "Filmweb News", "url": "https://www.filmweb.pl/feed/news/latest"},
    ]},
    # Międzynarodowe
    {"category": "international", "portal": "BBC News", "feeds": [
        {"name": "BBC Top Stories", "url": "https://feeds.bbci.co.uk/news/rss.xml"},
        {"name": "BBC Technology", "url": "https://feeds.bbci.co.uk/news/technology/rss.xml"},
        {"name": "BBC Business", "url": "https://feeds.bbci.co.uk/news/business/rss.xml"},
        {"name": "BBC Science", "url": "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"},
    ]},
    {"category": "international", "portal": "Reuters", "feeds": [
        {"name": "Reuters", "url": "https://www.reutersagency.com/feed/"},
    ]},
    {"category": "international", "portal": "TechCrunch", "feeds": [
        {"name": "TechCrunch", "url": "https://techcrunch.com/feed/"},
    ]},
    {"category": "international", "portal": "The Verge", "feeds": [
        {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml"},
    ]},
]


@router.get("/rss-catalog")
async def get_rss_catalog(category: Optional[str] = Query(None)):
    """Return the predefined RSS feed catalog for quick-add."""
    if category:
        return [c for c in RSS_CATALOG if c["category"] == category]
    return RSS_CATALOG


@router.post("/{portal_id}/sources/bulk-add")
async def bulk_add_sources(portal_id: int, feeds: List[dict]):
    """Bulk-add multiple RSS sources to a portal from the catalog.
    Expects list of {name, url} objects.
    """
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id FROM news_portals WHERE id = ?", (portal_id,)) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="Portal not found")

        # Get existing source URLs to avoid duplicates
        async with db.execute(
            "SELECT url FROM news_sources WHERE portal_id = ?", (portal_id,)
        ) as cur:
            existing_urls = {row["url"] for row in await cur.fetchall()}

        added = 0
        for feed in feeds:
            url = (feed.get("url") or "").strip()
            name = (feed.get("name") or url).strip()
            if not url or url in existing_urls:
                continue
            await db.execute(
                "INSERT INTO news_sources (portal_id, name, url, source_type, active) VALUES (?, ?, ?, 'rss', 1)",
                (portal_id, name, url),
            )
            existing_urls.add(url)
            added += 1
        await db.commit()
    return {"added": added}


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
            CREATE INDEX IF NOT EXISTS idx_news_drafts_published ON news_drafts(portal_id, published_at);
        """)
        # Migrate: add tone_of_voice + site_description columns if missing
        async with db.execute("PRAGMA table_info(news_portals)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        if "tone_of_voice" not in cols:
            await db.execute("ALTER TABLE news_portals ADD COLUMN tone_of_voice TEXT DEFAULT ''")
        if "site_description" not in cols:
            await db.execute("ALTER TABLE news_portals ADD COLUMN site_description TEXT DEFAULT ''")
        if "main_keyword" not in cols:
            await db.execute("ALTER TABLE news_portals ADD COLUMN main_keyword TEXT DEFAULT ''")
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


class BulkCreateRequest(BaseModel):
    domain_ids: List[int]
    niche: str = ""
    editorial_prompt: str = ""
    language: str = "pl"
    auto_publish: int = 1
    posts_per_day: int = 5
    check_interval_min: int = 30
    rss_feeds: List[dict] = []  # [{name, url}]


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
    FIX #48: corrected docstring — group items whose titles share >60% significant words.
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
            # FIX #20: raise clustering threshold from 0.5 to 0.6 — reduces false merges of loosely related news
            if _word_overlap(words_i, words_j) > 0.6:
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

        async with db.execute(
            "SELECT COUNT(*) as cnt FROM news_drafts WHERE status = 'published'"
        ) as cur:
            published_total = (await cur.fetchone())["cnt"]

    return {
        "total_portals": total_portals,
        "total_sources": total_sources,
        "pending_drafts": pending_drafts,
        "published_today": published_today,
        "published_total": published_total,
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
                CASE WHEN COALESCE(np.tone_of_voice, '') != '' THEN 1 ELSE 0 END AS has_tone,
                np.main_keyword,
                md.domain AS domain_name,
                (SELECT COUNT(*) FROM news_sources ns WHERE ns.portal_id = np.id) AS source_count,
                (SELECT COUNT(*) FROM news_drafts nd WHERE nd.portal_id = np.id AND nd.status = 'pending') AS pending_count,
                (SELECT COUNT(*) FROM news_drafts nd WHERE nd.portal_id = np.id AND nd.status = 'published') AS published_count
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


@router.get("/published-urls")
async def list_published_urls(
    portal_id: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    """List all published news articles with their WordPress URLs."""
    await ensure_tables()
    offset = (page - 1) * per_page
    conditions = ["nd.status = 'published'"]
    params: list = []
    if portal_id:
        conditions.append("nd.portal_id = ?")
        params.append(portal_id)
    where = " AND ".join(conditions)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT COUNT(*) as cnt FROM news_drafts nd WHERE {where}", params
        ) as cur:
            total = (await cur.fetchone())["cnt"]

        query_params = params + [per_page, offset]
        async with db.execute(
            f"""SELECT nd.id, nd.title, nd.wp_post_url, nd.published_at, nd.portal_id,
                       np.name AS portal_name
                FROM news_drafts nd
                LEFT JOIN news_portals np ON np.id = nd.portal_id
                WHERE {where}
                ORDER BY nd.published_at DESC
                LIMIT ? OFFSET ?""",
            query_params,
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    logger.info(f"[PublishedURLs] portal_id={portal_id} total={total} returned={len(rows)} where={where}")
    return {
        "items": rows,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if per_page else 1,
    }


# Track last run per portal for status reporting
_autopilot_status: dict = {}  # {portal_id: {last_run, next_run, published, errors, running}}


@router.get("/autopilot-status")
async def get_autopilot_status():
    """Return auto-pilot status for all portals."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, name, auto_publish, posts_per_day, check_interval_min, active
               FROM news_portals WHERE auto_publish = 1 AND active = 1"""
        ) as cur:
            portals = [dict(r) for r in await cur.fetchall()]
    result = []
    for p in portals:
        st = _autopilot_status.get(p["id"], {})
        result.append({
            "portal_id": p["id"],
            "name": p["name"],
            "posts_per_day": p["posts_per_day"],
            "check_interval_min": p["check_interval_min"],
            "last_run": st.get("last_run"),
            "next_run": st.get("next_run"),
            "last_published": st.get("published", 0),
            "last_errors": st.get("errors", 0),
            "running": st.get("running", False),
        })
    return result


@router.post("/autopilot-run")
async def manual_autopilot_run():
    """Manually trigger one news autopilot cycle for all auto-publish portals."""
    result = await run_news_autopilot()
    return result


@router.post("/generate-tone/{portal_id}")
async def generate_tone_of_voice(portal_id: int):
    """Generate Tone of Voice for a portal using n8n-style pipeline:
    1. Parse domain content (DFS content_parsing)
    2. Extract main keyword (GPT)
    3. Generate customer profile (GPT)
    4. SERP analysis for keyword (DFS)
    5. Select top 5 competitor blogs (GPT)
    6. Parse competitor content (DFS)
    7. Mix tone of voice candidates (GPT)
    8. Generate final Tone of Voice (GPT)
    9. Generate site description (GPT)
    """
    async with _NEWS_GENERATE_SEM:
        return await _generate_tone_of_voice_inner(portal_id)


async def _generate_tone_of_voice_inner(portal_id: int):
    import base64 as _b64
    from config import DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD

    await ensure_tables()

    # Load portal + domain
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM news_portals WHERE id=?", (portal_id,)) as cur:
            portal = await cur.fetchone()
        if not portal:
            raise HTTPException(404, "Portal not found")
        portal = dict(portal)
        if not portal.get("my_domain_id"):
            raise HTTPException(400, "Portal nie ma przypisanej domeny")
        async with db.execute("SELECT * FROM my_domains WHERE id=?", (portal["my_domain_id"],)) as cur:
            domain = await cur.fetchone()
        if not domain:
            raise HTTPException(404, "Domain not found")
        domain = dict(domain)

    domain_url = domain["domain"]
    lang = portal.get("language", "pl")
    lang_pl = lang == "pl"

    dfs_ok = bool(DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD)
    dfs_headers = {}
    if dfs_ok:
        creds = _b64.b64encode(f"{DATAFORSEO_LOGIN}:{DATAFORSEO_PASSWORD}".encode()).decode()
        dfs_headers = {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}

    steps_log = []

    # ── STEP 1: Parse domain content via DFS ──
    content_text = ""
    if dfs_ok:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                resp = await c.post(
                    "https://api.dataforseo.com/v3/on_page/content_parsing/live",
                    json=[{"url": f"https://{domain_url}/", "enable_javascript": False}],
                    headers=dfs_headers,
                )
            if resp.status_code == 200:
                data = resp.json()
                for task in data.get("tasks", []):
                    for result in task.get("result", []) or []:
                        for item in result.get("items", []) or []:
                            pc = item.get("page_content", {})
                            for topic in pc.get("main_topic", []) or []:
                                h_title = topic.get("h_title", "")
                                texts = [t.get("text", "") for t in topic.get("primary_content", []) or []]
                                content_text += f"H{topic.get('level', '')} - {h_title}\n" + "\n".join(texts) + "\n\n"
            steps_log.append(f"Content parsed: {len(content_text)} chars")
        except Exception as e:
            steps_log.append(f"Content parsing failed: {e}")

    if not content_text:
        content_text = f"Domena: {domain_url}, nisza: {portal.get('niche', '')}"
        steps_log.append("Using fallback content (domain + niche)")

    # ── STEP 2: Extract main keyword ──
    kw_system = (
        "Analizuj podany content ze strony internetowej i zidentyfikuj główne słowo kluczowe. "
        "Podaj TYLKO jedno słowo kluczowe, bez cudzysłowów, bez wyjaśnień."
    ) if lang_pl else (
        "Analyze the website content and identify the main keyword. "
        "Return ONLY one keyword, no quotes, no explanation."
    )
    main_keyword = await _news_gpt(kw_system, f"Content:\n{content_text[:3000]}", temperature=0.4, max_tokens=50)
    main_keyword = main_keyword.strip().strip('"').strip("'")
    steps_log.append(f"Main keyword: {main_keyword}")

    # ── STEP 3: Customer profile ──
    profile_prompt = (
        f"Stwórz profil klienta dla strony {domain_url} ({portal.get('niche', '')}). Uwzględnij:\n"
        f"1. Przegląd typowego klienta\n2. Demografię\n3. Wartości i postawy\n"
        f"4. Problemy i bolączki\n5. Kluczowe motywacje\n6. Proces decyzyjny\n"
        f"7. Czego szukają na tej stronie\nBądź konkretny i szczegółowy."
    ) if lang_pl else (
        f"Create a customer profile for {domain_url} ({portal.get('niche', '')}). Include:\n"
        f"1. Typical client overview\n2. Demographics\n3. Values and attitudes\n"
        f"4. Problems and pain points\n5. Key motivations\n6. Decision process\n"
        f"7. What they're looking for\nBe specific and detailed."
    )
    customer_profile = await _news_gpt(
        "Jesteś ekspertem od marketingu i analizy klientów." if lang_pl else "You are a marketing and customer analysis expert.",
        profile_prompt, temperature=0.6, max_tokens=1500,
    )
    steps_log.append(f"Customer profile: {len(customer_profile)} chars")

    # ── STEP 4: SERP analysis for keyword ──
    competitor_content = []
    if dfs_ok and main_keyword:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                resp = await c.post(
                    "https://api.dataforseo.com/v3/serp/google/organic/live/advanced",
                    json=[{"keyword": main_keyword, "location_name": "Poland", "language_name": "Polish", "device": "desktop", "depth": 10}],
                    headers=dfs_headers,
                )
            serp_urls = []
            if resp.status_code == 200:
                data = resp.json()
                for task in data.get("tasks", []):
                    for result in task.get("result", []) or []:
                        for item in result.get("items", []) or []:
                            if item.get("type") == "organic" and item.get("url"):
                                serp_urls.append(item["url"])
            steps_log.append(f"SERP results: {len(serp_urls)} URLs")

            # ── STEP 5: Select top 5 blog competitors ──
            if serp_urls:
                select_system = (
                    "Wybierz 5 najlepszych URL-i z blogów/portali informacyjnych. "
                    "Pomiń Wikipedię, strony rządowe, sklepy. Zwróć TYLKO URL-e oddzielone przecinkami."
                ) if lang_pl else (
                    "Select 5 best blog/news URLs. Skip Wikipedia, government, shops. "
                    "Return ONLY URLs separated by commas."
                )
                selected = await _news_gpt(
                    select_system,
                    f"Keyword: {main_keyword}\nURLs:\n" + "\n".join(serp_urls[:15]),
                    temperature=0.2, max_tokens=500,
                )
                comp_urls = [u.strip() for u in selected.split(",") if u.strip().startswith("http")][:5]
                steps_log.append(f"Selected {len(comp_urls)} competitors")

                # ── STEP 6: Parse competitor content ──
                for comp_url in comp_urls:
                    try:
                        async with httpx.AsyncClient(timeout=30) as c:
                            resp = await c.post(
                                "https://api.dataforseo.com/v3/on_page/content_parsing/live",
                                json=[{"url": comp_url, "enable_javascript": False}],
                                headers=dfs_headers,
                            )
                        if resp.status_code == 200:
                            data = resp.json()
                            comp_text = ""
                            for task in data.get("tasks", []):
                                for result in task.get("result", []) or []:
                                    for item in result.get("items", []) or []:
                                        pc = item.get("page_content", {})
                                        for topic in pc.get("main_topic", []) or []:
                                            texts = [t.get("text", "") for t in topic.get("primary_content", []) or []]
                                            comp_text += f"H{topic.get('level', '')} - {topic.get('h_title', '')}\n" + "\n".join(texts) + "\n\n"
                            if comp_text:
                                competitor_content.append(comp_text[:2000])
                    except Exception:
                        pass
                steps_log.append(f"Parsed {len(competitor_content)} competitor pages")
        except Exception as e:
            steps_log.append(f"SERP/competitor analysis failed: {e}")

    # ── STEP 7: Mix tone of voice candidates ──
    mix_system = (
        "Twoim zadaniem jest wymieszanie tonów głosu i stworzenie 15-20 mieszanych tonów "
        "pasujących do siebie. Nie dodawaj nic oprócz mieszanek. Lista tonów do mieszania:\n"
        "Accessible, Conversational, Ambitious, Concise, Assertive, Authentic, Authoritative, "
        "Educational, Supportive, Bold, Clear, Insightful, Inspiring, Commanding, Confident, "
        "Compelling, Aspirational, Polished, Considerate, Consultative, Convincing, Relatable, "
        "Curious, Creative, Decisive, Dependable, Dynamic, Informative, Energetic, Optimistic, "
        "Approachable, Evaluative, Expository, Systematic, Functional"
    )
    mixed_tones = await _news_gpt(mix_system, "Mix these tones.", temperature=0.6, max_tokens=800)
    steps_log.append("Mixed tones generated")

    # ── STEP 8: Generate final Tone of Voice ──
    comp_text_combined = "\n---\n".join(competitor_content[:5]) if competitor_content else "Brak danych konkurencji"

    tone_system = (
        "Stwórz jasną instrukcję Tone of Voice dopasowaną do strony internetowej. "
        "Ton powinien być zgodny z treścią strony, wynikami SERP i profilem klienta.\n\n"
        "Wynik powinien zawierać:\n"
        "- Nazwa i opis tonu\n"
        "- Wytyczne pisania (styl, słownictwo, emocje)\n"
        "- Jak ton angażuje grupę docelową\n"
        "- Wskazówki spójności między artykułami\n\n"
        "Pisz po polsku, konkretnie, jako instrukcję dla AI piszącego artykuły."
    ) if lang_pl else (
        "Create a clear Tone of Voice instruction tailored for a website. "
        "The tone should align with the website content, SERP results, and customer profile.\n\n"
        "Output should include:\n"
        "- Tone name and description\n"
        "- Writing guidelines (style, vocabulary, emotions)\n"
        "- How the tone engages the target audience\n"
        "- Consistency cues across articles\n\n"
        "Write concretely, as an instruction for AI writing articles."
    )

    tone_user = (
        f"<tone_candidates>\n{mixed_tones}\n</tone_candidates>\n\n"
        f"<website_content>\n{content_text[:2000]}\n</website_content>\n\n"
        f"<competitor_content>\n{comp_text_combined[:3000]}\n</competitor_content>\n\n"
        f"<customer_profile>\n{customer_profile[:2000]}\n</customer_profile>\n\n"
        f"<about_website>\nDomena: {domain_url}, Nisza: {portal.get('niche', '')}\n</about_website>"
    )

    tone_of_voice = await _news_gpt(tone_system, tone_user, temperature=0.6, max_tokens=2000)
    steps_log.append(f"Tone of Voice generated: {len(tone_of_voice)} chars")

    # ── STEP 9: Generate site description ──
    desc_system = (
        "Napisz krótki opis strony internetowej (max 400 znaków) — o czym jest ta strona. "
        "Opis jest instrukcją dla bota AI, który później ma pisać content w tematyce tej strony."
    ) if lang_pl else (
        "Write a short website description (max 400 chars) — what this site is about. "
        "This description is an instruction for an AI bot writing content about this site's topic."
    )
    site_description = await _news_gpt(
        desc_system, f"Content:\n{content_text[:2000]}", temperature=0.6, max_tokens=300,
    )
    steps_log.append(f"Site description: {len(site_description)} chars")

    # ── Save to DB ──
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE news_portals SET tone_of_voice=?, site_description=?, main_keyword=? WHERE id=?",
            (tone_of_voice, site_description, main_keyword, portal_id),
        )
        await db.commit()

    logger.info(f"[ToneOfVoice] Generated for portal {portal_id} ({domain_url}): keyword={main_keyword}")

    return {
        "portal_id": portal_id,
        "domain": domain_url,
        "main_keyword": main_keyword,
        "tone_of_voice": tone_of_voice,
        "site_description": site_description,
        "steps": steps_log,
    }


@router.post("/bulk-create")
async def bulk_create_portals(body: BulkCreateRequest):
    """Create news portals for multiple domains at once with shared settings + RSS sources."""
    await ensure_tables()
    if not body.domain_ids:
        raise HTTPException(status_code=400, detail="domain_ids is required")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Validate domains exist
        placeholders = ",".join("?" for _ in body.domain_ids)
        async with db.execute(
            f"SELECT id, domain FROM my_domains WHERE id IN ({placeholders})", body.domain_ids
        ) as cur:
            valid_domains = {r["id"]: r["domain"] for r in await cur.fetchall()}

        # Check which domains already have a portal
        async with db.execute(
            f"SELECT my_domain_id FROM news_portals WHERE my_domain_id IN ({placeholders})", body.domain_ids
        ) as cur:
            existing = {r["my_domain_id"] for r in await cur.fetchall()}

    created = []
    skipped = []
    for did in body.domain_ids:
        if did not in valid_domains:
            skipped.append({"domain_id": did, "reason": "not found"})
            continue
        if did in existing:
            skipped.append({"domain_id": did, "domain": valid_domains[did], "reason": "portal exists"})
            continue

        domain_name = valid_domains[did]
        portal_name = body.niche.strip() or domain_name.split(".")[0].capitalize()
        portal_name = f"News {portal_name}" if not portal_name.lower().startswith("news") else portal_name

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """INSERT INTO news_portals
                   (name, my_domain_id, niche, editorial_prompt, language, auto_publish, posts_per_day, check_interval_min, active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (portal_name, did, body.niche, body.editorial_prompt,
                 body.language, body.auto_publish, body.posts_per_day, body.check_interval_min),
            )
            portal_id = cursor.lastrowid

            # Add RSS sources
            for feed in body.rss_feeds:
                fname = feed.get("name", "")
                furl = feed.get("url", "")
                if furl:
                    await db.execute(
                        "INSERT INTO news_sources (portal_id, name, url, source_type, active) VALUES (?, ?, ?, 'rss', 1)",
                        (portal_id, fname or furl, furl),
                    )
            await db.commit()

        created.append({"portal_id": portal_id, "domain_id": did, "domain": domain_name, "name": portal_name})

    logger.info(f"[BulkCreate] Created {len(created)} portals, skipped {len(skipped)}")
    return {
        "created": len(created),
        "skipped": len(skipped),
        "portals": created,
        "skipped_details": skipped if skipped else None,
    }


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

    # Rotate User-Agent to avoid 403 blocks from portals like TVN24
    _RSS_HEADERS_POOL = [
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
            "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
            "Accept-Language": "pl-PL,pl;q=0.9",
        },
        {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
            "Accept-Language": "pl,en-US;q=0.7,en;q=0.3",
        },
    ]
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, verify=False) as http:
        for source in sources:
            try:
                _headers = random.choice(_RSS_HEADERS_POOL)
                resp = await http.get(source["url"], headers=_headers)
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
                            cur = await db.execute(
                                """INSERT OR IGNORE INTO news_items
                                   (source_id, portal_id, title, url, content, published_at, fingerprint)
                                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                (source["id"], portal_id, fi["title"], fi["link"],
                                 desc_clean[:5000], fi.get("pubDate", ""), fp),
                            )
                            if cur.rowcount > 0:
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


async def _news_gpt(
    system: str, user: str, temperature: float = 0.5,
    max_tokens: int = 1500, model: str = None,
) -> str:
    """GPT helper for news pipeline with retry logic."""
    _client, _default_model, _is_custom = await get_openai_client()
    if model is None:
        model = _default_model
    for attempt in range(3):
        try:
            response = await _client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            if attempt == 2:
                raise
            # FIX #69: add jitter to retry delay (prevents thundering herd with concurrent news generation)
            wait = 2 ** attempt + random.uniform(0, 1.5)
            logger.warning(f"[NewsGPT] attempt {attempt+1} failed: {e} — retrying in {wait:.1f}s")
            await asyncio.sleep(wait)
    return ""


@router.post("/{portal_id}/generate")
async def generate_draft(portal_id: int, body: GenerateRequest):
    """Generate a world-class unique news article from a cluster using multi-step pipeline."""
    async with _NEWS_GENERATE_SEM:
        return await _generate_draft_inner(portal_id, body)


async def _generate_draft_inner(portal_id: int, body: GenerateRequest):
    _t0 = time.time()
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

    # ── Resolve GPT model once for entire pipeline ──
    _resolved_model = await get_gpt_model()

    # ── Build source context ──
    sources_parts = []
    source_urls = []
    source_titles = []
    for i, item in enumerate(items, 1):
        part = f"Źródło {i}: {item['title']}"
        source_titles.append(item['title'])
        if item.get("content"):
            part += f"\n{item['content'][:1200]}"
        if item.get("url"):
            part += f"\nURL: {item['url']}"
            source_urls.append(item["url"])
        sources_parts.append(part)
    sources_text = "\n\n".join(sources_parts)

    portal_language = portal.get("language", "pl")
    portal_niche = portal.get("niche", "")
    portal_editorial = portal.get("editorial_prompt", "")
    portal_tone = portal.get("tone_of_voice", "")
    portal_site_desc = portal.get("site_description", "")
    lang_pl = portal_language == "pl"
    _current_year = datetime.now(timezone.utc).year

    # Build editorial context block
    editorial_ctx = ""
    if portal_editorial:
        editorial_ctx = f"\nWytyczne redakcji: {portal_editorial}" if lang_pl else f"\nEditorial guidelines: {portal_editorial}"
    if portal_tone:
        editorial_ctx += f"\n\nTONE OF VOICE (stosuj ten ton w całym artykule):\n{portal_tone[:1500]}" if lang_pl else f"\n\nTONE OF VOICE (apply this tone throughout the article):\n{portal_tone[:1500]}"
    if portal_site_desc:
        editorial_ctx += f"\n\nOpis strony: {portal_site_desc}" if lang_pl else f"\n\nSite description: {portal_site_desc}"
    niche_ctx = ""
    if portal_niche:
        niche_ctx = f"\nNisza tematyczna: {portal_niche}" if lang_pl else f"\nThematic niche: {portal_niche}"

    # ── STEP 1: Analyze sources → extract key facts, angles, entities ──
    logger.info(f"[NewsGen] Starting pipeline for cluster {body.cluster_id} ({len(items)} sources)")

    if lang_pl:
        analysis_system = (
            "Jesteś analitykiem newsowym. Analizujesz źródła i wyciągasz kluczowe fakty."
        )
        analysis_user = (
            f"Przeanalizuj poniższe źródła newsowe i wyciągnij:\n"
            f"1. GŁÓWNY TEMAT (1 zdanie)\n"
            f"2. 5-8 KLUCZOWYCH FAKTÓW (konkretne dane, liczby, nazwiska, daty)\n"
            f"3. ENCJE (osoby, firmy, instytucje, miejsca, produkty — nazwy własne)\n"
            f"4. KONTEKST (dlaczego to ważne, jaki jest szerszy kontekst)\n"
            f"5. UNIKALNY KĄT (jaki aspekt tematu jest najciekawszy dla czytelnika)\n\n"
            f"Źródła:\n{sources_text}{niche_ctx}"
        )
    else:
        analysis_system = "You are a news analyst. Extract key facts from sources."
        analysis_user = (
            f"Analyze these news sources and extract:\n"
            f"1. MAIN TOPIC (1 sentence)\n"
            f"2. 5-8 KEY FACTS (specific data, numbers, names, dates)\n"
            f"3. ENTITIES (people, companies, institutions, places, products — proper nouns)\n"
            f"4. CONTEXT (why this matters, broader context)\n"
            f"5. UNIQUE ANGLE (most interesting aspect for readers)\n\n"
            f"Sources:\n{sources_text}{niche_ctx}"
        )

    analysis = await _news_gpt(
        analysis_system, analysis_user,
        temperature=0.3, max_tokens=600, model=_resolved_model,
    )
    logger.info(f"[NewsGen] Analysis done: {analysis[:80]}...")

    # ── STEP 2: Generate headline + outline (JSON structured) ──
    if lang_pl:
        outline_system = (
            "Jesteś redaktorem naczelnym prestiżowego portalu informacyjnego. "
            "Tworzysz chwytliwe nagłówki i struktury artykułów."
            f"{editorial_ctx}"
        )
        outline_user = (
            f"Na podstawie analizy stwórz strukturę artykułu newsowego.\n\n"
            f"Analiza:\n{analysis}\n\n"
            f"Odpowiedz TYLKO w formacie JSON (bez markdown):\n"
            f'{{"title": "chwytliwy tytuł 50-70 znaków, bez cudzysłowów", '
            f'"lead": "lead 2-3 zdania, podsumowanie najważniejszych faktów", '
            f'"sections": ["Nagłówek H2 sekcji 1", "Nagłówek H2 sekcji 2", "Nagłówek H2 sekcji 3", "Nagłówek H2 sekcji 4"]}}'
        )
    else:
        outline_system = (
            "You are the editor-in-chief of a prestigious news portal. "
            "Create catchy headlines and article structures."
            f"{editorial_ctx}"
        )
        outline_user = (
            f"Based on the analysis, create a news article structure.\n\n"
            f"Analysis:\n{analysis}\n\n"
            f"Reply ONLY in JSON format (no markdown):\n"
            f'{{"title": "catchy title 50-70 chars, no quotes", '
            f'"lead": "lead 2-3 sentences summarizing key facts", '
            f'"sections": ["H2 heading 1", "H2 heading 2", "H2 heading 3", "H2 heading 4"]}}'
        )

    outline_raw = await _news_gpt(
        outline_system, outline_user,
        temperature=0.6, max_tokens=500, model=_resolved_model,
    )

    # FIX #15: more robust JSON extraction — handle nested braces, ```json wrapper, and GPT preamble
    outline_clean = re.sub(r"```(?:json)?\s*", "", outline_raw).strip().rstrip("`")
    # Strip any text before the first {
    first_brace = outline_clean.find('{')
    if first_brace > 0:
        outline_clean = outline_clean[first_brace:]
    try:
        outline = json.loads(outline_clean)
    except json.JSONDecodeError:
        # FIX #16: use greedy match for nested JSON (sections array has inner strings)
        json_match = re.search(r'\{.*"title".*\}', outline_clean, re.DOTALL)
        if json_match:
            try:
                outline = json.loads(json_match.group(0))
            except json.JSONDecodeError:
                outline = {}
        else:
            outline = {}

    title = outline.get("title", cluster.get("label", source_titles[0] if source_titles else "News"))
    title = title.strip('"\'').strip()
    # FIX #17: strip markdown artifacts and enforce max 70 chars for news titles
    title = re.sub(r'^[#*\s]+', '', title).strip()
    if len(title) > 70:
        title = title[:70].rsplit(' ', 1)[0]
    lead = outline.get("lead", "")
    sections = outline.get("sections", [])

    if not sections:
        # Fallback sections
        if lang_pl:
            sections = ["Co się wydarzyło?", "Szczegóły i kontekst", "Reakcje i komentarze", "Co dalej?"]
        else:
            sections = ["What happened?", "Details and context", "Reactions and comments", "What's next?"]

    logger.info(f"[NewsGen] Title: {title} | Sections: {len(sections)}")

    # ── STEP 3: Generate intro (lead) ──
    if lang_pl:
        intro_system = (
            "Jesteś doświadczonym dziennikarzem. Piszesz wstępy do artykułów newsowych w HTML.\n"
            "STRUKTURA: 2-3 akapity w tagach <p>.\n"
            "1) Akapit 1 = KTO, CO, KIEDY, GDZIE — najważniejszy fakt.\n"
            "2) Akapit 2 = DLACZEGO to ważne, kontekst.\n"
            "Używaj <strong> dla kluczowych nazwisk, liczb, dat.\n"
            "BEZWZGLĘDNY ZAKAZ: NIE używaj markdown. TYLKO HTML."
            f"{editorial_ctx}"
        )
        intro_user = (
            f"Napisz wstęp (lead) do artykułu '{title}'.\n"
            f"Lead do rozwinięcia: {lead}\n"
            f"Kluczowe fakty:\n{analysis}\n"
            f"Źródła: {', '.join(source_titles[:3])}\n"
            f"Tylko HTML <p> i <strong>. Bez nagłówków."
        )
    else:
        intro_system = (
            "You are an experienced journalist. Write news article intros in HTML.\n"
            "STRUCTURE: 2-3 paragraphs in <p> tags.\n"
            "1) Para 1 = WHO, WHAT, WHEN, WHERE — the most important fact.\n"
            "2) Para 2 = WHY it matters, context.\n"
            "Use <strong> for key names, numbers, dates.\n"
            "STRICT: NO markdown. ONLY HTML."
            f"{editorial_ctx}"
        )
        intro_user = (
            f"Write the lead/intro for article '{title}'.\n"
            f"Lead to expand: {lead}\n"
            f"Key facts:\n{analysis}\n"
            f"Sources: {', '.join(source_titles[:3])}\n"
            f"Only HTML <p> and <strong>. No headings."
        )

    # ── STEP 4: Generate body sections (parallel with semaphore) ──
    _sem = asyncio.Semaphore(3)

    async def _gen_section(i: int, heading: str) -> str:
        async with _sem:
            if lang_pl:
                sec_system = (
                    "Jesteś dziennikarzem śledczym i ekspertem tematycznym. Piszesz sekcje artykułów newsowych w HTML.\n"
                    "WYMAGANIA:\n"
                    "- Zacznij od <h2>, dodaj 1-2 <h3> jeśli temat wymaga podziału\n"
                    "- Używaj <p>, <ul>/<li>, <strong> dla ważnych faktów\n"
                    "- Pisz KONKRETNIE: dane, liczby, cytaty, fakty — NIE ogólniki\n"
                    "- ENCJE: Używaj konkretnych nazw własnych (osoby, firmy, instytucje)\n"
                    "- E-E-A-T: Wpleć perspektywę ekspercką ('Eksperci wskazują...', 'Analitycy podkreślają...')\n"
                    "- INFORMATION GAIN: Dodaj 1 fakt/kontekst którego nie ma w typowych newsach na ten temat\n"
                    "- HUMANIZACJA: Mieszaj krótkie zdania (5-8 słów) z długimi (20-30). "
                    "1 pytanie retoryczne lub porównanie na sekcję.\n"
                    "- 150-250 słów na sekcję\n"
                    "BEZWZGLĘDNY ZAKAZ: NIE używaj markdown. TYLKO HTML."
                    f"{editorial_ctx}"
                )
                sec_user = (
                    f"Napisz sekcję artykułu '{title}'.\n"
                    f"H2: '{heading}'\n"
                    f"Fakty i kontekst:\n{analysis}\n"
                    f"Struktura: <h2>{heading}</h2> → akapity <p> z faktami\n"
                    f"Źródła: {', '.join(source_titles[:3])}"
                )
            else:
                sec_system = (
                    "You are an investigative journalist and subject expert. Write news sections in HTML.\n"
                    "REQUIREMENTS:\n"
                    "- Start with <h2>, add 1-2 <h3> if needed\n"
                    "- Use <p>, <ul>/<li>, <strong> for key facts\n"
                    "- Be SPECIFIC: data, numbers, quotes, facts — NO generalities\n"
                    "- ENTITIES: Use proper nouns (people, companies, institutions)\n"
                    "- E-E-A-T: Include expert perspective ('Experts point out...', 'Analysts emphasize...')\n"
                    "- INFORMATION GAIN: Add 1 fact/context missing from typical news on this topic\n"
                    "- HUMANIZATION: Mix short sentences (5-8 words) with long (20-30). "
                    "1 rhetorical question or comparison per section.\n"
                    "- 150-250 words per section\n"
                    "STRICT: NO markdown. ONLY HTML."
                    f"{editorial_ctx}"
                )
                sec_user = (
                    f"Write a section of article '{title}'.\n"
                    f"H2: '{heading}'\n"
                    f"Facts and context:\n{analysis}\n"
                    f"Structure: <h2>{heading}</h2> → <p> paragraphs with facts\n"
                    f"Sources: {', '.join(source_titles[:3])}"
                )
            sec_html = await _news_gpt(
                sec_system, sec_user,
                temperature=0.5, max_tokens=800, model=_resolved_model,
            )
            if not sec_html.strip().startswith("<"):
                sec_html = _markdown_to_html(sec_html)
            sec_html = _strip_markdown_remnants(sec_html)
            logger.info(f"[NewsGen] Section {i+1}/{len(sections)}: {heading[:40]}")
            return sec_html

    # Launch intro + all sections in parallel
    intro_task = _news_gpt(
        intro_system, intro_user,
        temperature=0.5, max_tokens=500, model=_resolved_model,
    )
    section_tasks = [_gen_section(i, h) for i, h in enumerate(sections)]

    results = await asyncio.gather(intro_task, *section_tasks)
    intro_html = results[0]
    sections_html = list(results[1:])

    # Post-process intro
    if not intro_html.strip().startswith("<"):
        intro_html = _markdown_to_html(intro_html)
    intro_html = _strip_markdown_remnants(intro_html)

    # ── STEP 5: Conclusion + Excerpt (parallel) ──
    if lang_pl:
        _concl_headings = ["Podsumowanie", "Co dalej?", "Kluczowe wnioski", "Najważniejsze ustalenia"]
    else:
        _concl_headings = ["Summary", "What's next?", "Key takeaways", "Key findings"]
    _concl_h2 = random.choice(_concl_headings)

    if lang_pl:
        concl_user = (
            f"Napisz krótkie zakończenie artykułu '{title}'.\n"
            f"Omówione tematy: {', '.join(sections[:4])}\n"
            f"STRUKTURA:\n"
            f"<h2>{_concl_h2}</h2>\n"
            f"- 1 akapit: podsumowanie kluczowych faktów\n"
            f"- 1 akapit: co to oznacza / co dalej\n"
            f"Max 100 słów. TYLKO HTML."
        )
        excerpt_user = (
            f"Napisz meta description dla artykułu '{title}'.\n"
            f"Max 155 znaków, bez HTML, zawiera kluczowy fakt. Tylko tekst."
        )
    else:
        concl_user = (
            f"Write a short conclusion for '{title}'.\n"
            f"Topics covered: {', '.join(sections[:4])}\n"
            f"STRUCTURE:\n"
            f"<h2>{_concl_h2}</h2>\n"
            f"- 1 paragraph: key facts summary\n"
            f"- 1 paragraph: what it means / what's next\n"
            f"Max 100 words. HTML ONLY."
        )
        excerpt_user = (
            f"Write meta description for '{title}'.\n"
            f"Max 155 chars, no HTML, includes key fact. Only text."
        )

    concl_system = (
        "Jesteś dziennikarzem. Piszesz zakończenia artykułów w HTML. TYLKO tagi HTML."
    ) if lang_pl else (
        "You are a journalist. Write article conclusions in HTML. ONLY HTML tags."
    )

    conclusion_raw, excerpt_raw = await asyncio.gather(
        _news_gpt(concl_system, concl_user, temperature=0.5, max_tokens=400, model=_resolved_model),
        _news_gpt(
            "Jesteś SEO copywriterem." if lang_pl else "You are an SEO copywriter.",
            excerpt_user, temperature=0.4, max_tokens=80, model=_resolved_model,
        ),
    )

    conclusion_html = conclusion_raw
    if not conclusion_html.strip().startswith("<"):
        conclusion_html = _markdown_to_html(conclusion_html)
    conclusion_html = _strip_markdown_remnants(conclusion_html)

    # FIX #18: enforce 155 char limit (was 300 — too long for meta description)
    excerpt = excerpt_raw.strip('"\'').strip()
    excerpt = re.sub(r'^(?:Meta\s*(?:description|opis)\s*:?\s*)', '', excerpt, flags=re.IGNORECASE).strip()
    if len(excerpt) > 155:
        excerpt = excerpt[:155].rsplit(' ', 1)[0].rstrip('.,;:') + '.'
    # SEO #52: ensure excerpt contains at least a key term from the title
    _title_words = set(re.findall(r'\w{4,}', title.lower()))
    _excerpt_lower = excerpt.lower()
    if _title_words and not any(w in _excerpt_lower for w in _title_words):
        _main_word = max(_title_words, key=len) if _title_words else ""
        if _main_word and len(excerpt) + len(_main_word) + 5 < 155:
            excerpt = f"{_main_word.capitalize()}: {excerpt}"

    # ── STEP 6: Source attribution box (E-E-A-T) ──
    if source_urls:
        if lang_pl:
            src_label = "Źródła"
        else:
            src_label = "Sources"
        # FIX #49: show domain name instead of full URL for cleaner display
        def _display_domain(u: str) -> str:
            d = u.split("//")[-1].split("/")[0]
            path = "/".join(u.split("//")[-1].split("/")[1:3])
            return f"{d}/{path}" if path else d
        src_links = "".join(
            f'<li><a href="{url}" target="_blank" rel="noopener noreferrer nofollow">{_display_domain(url)}</a></li>'
            for url in source_urls[:5]
        )
        _vary_px = random.randint(12, 18)
        _vary_radius = random.randint(4, 8)
        source_box = (
            f'<div role="complementary" aria-label="{src_label}" style="background:#f8fafc;border:1px solid #e2e8f0;'
            f'padding:{_vary_px}px {_vary_px + 4}px;margin:20px 0;'
            f'border-radius:{_vary_radius}px;font-size:0.9em;">'
            f'<strong>{src_label}:</strong>'
            f'<ul style="margin:8px 0 0;padding-left:20px;">{src_links}</ul></div>'
        )
    else:
        source_box = ""

    # ── STEP 7: Assemble article ──
    # Layout variant (vary to avoid footprint)
    _rv = random.random()
    if _rv < 0.3 and source_box:
        # Sources at top (30%)
        content_parts = [source_box, intro_html] + sections_html + [conclusion_html]
    elif _rv < 0.5:
        # Update box variant
        _update_date = datetime.now(timezone.utc).strftime("%d.%m.%Y" if lang_pl else "%Y-%m-%d")
        _update_label = f"Artykuł zaktualizowany: {_update_date}" if lang_pl else f"Updated: {_update_date}"
        update_box = (
            f'<div style="background:#eff6ff;border-left:4px solid #3b82f6;padding:10px 16px;'
            f'margin:0 0 20px;border-radius:0 6px 6px 0;font-size:0.85em;">'
            f'<strong>{_update_label}</strong></div>'
        )
        content_parts = [update_box, intro_html] + sections_html + [conclusion_html, source_box]
    else:
        # Standard (50%)
        content_parts = [intro_html] + sections_html + [conclusion_html, source_box]

    content = "\n\n".join(p for p in content_parts if p)

    # SEO #87: inject internal links to other published articles on same portal
    try:
        async with aiosqlite.connect(DB_PATH) as _db:
            _db.row_factory = aiosqlite.Row
            async with _db.execute(
                """SELECT title, wp_post_url FROM news_drafts
                   WHERE portal_id = ? AND status = 'published' AND wp_post_url != ''
                   ORDER BY published_at DESC LIMIT 20""",
                (portal_id,)
            ) as _cur:
                _published_news = [{"title": r["title"], "keyword": r["title"], "url": r["wp_post_url"]} for r in await _cur.fetchall()]
        if _published_news:
            from services.openai_service import _inject_internal_links
            content = _inject_internal_links(content, _published_news, title, language=portal_language)
    except Exception as _e:
        logger.warning(f"[NewsGen] Internal linking failed: {_e}")

    # SEO #88: enrich news articles with TOC + random elements
    # NOTE: do NOT pass serp_urls here — step 6 already adds a "Źródła:" box.
    # Passing serp_urls would add a duplicate "Źródła i dodatkowe informacje" section.
    try:
        from services.content_enrichments import enrich_article as _enrich_news
        _news_sections = [re.sub(r'<[^>]+>', '', h).strip() for h in re.findall(r'<h2[^>]*>(.*?)</h2>', content, re.DOTALL)][:6]
        _enrich_client, _, _is_custom = await get_openai_client()
        content = await _enrich_news(
            content=content,
            topic=title,
            sections=_news_sections,
            lang_pl=lang_pl,
            openai_client=_enrich_client,
            serp_urls=None,
            is_custom_llm=_is_custom,
        )
    except Exception as _e:
        logger.warning(f"[NewsGen] Enrichment failed: {_e}")

    # Final cleanup
    content = _strip_markdown_remnants(content)
    # SEO #46: fix heading hierarchy in news articles
    content = _fix_heading_hierarchy(content)

    # ── STEP 8: NewsArticle + FAQPage JSON-LD Schema ──
    _now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    # Author entity pool — varies per portal for anti-footprint + E-E-A-T entity building
    _news_authors_pl = [
        {"name": "Redakcja", "desc": "Zespół redakcyjny portalu"},
        {"name": "Dział Informacyjny", "desc": "Redaktorzy i dziennikarze portalu"},
        {"name": "Newsroom", "desc": "Centrum informacyjne portalu"},
    ]
    _news_authors_en = [
        {"name": "Editorial Team", "desc": "Portal editorial staff"},
        {"name": "Newsroom", "desc": "News desk team"},
        {"name": "News Desk", "desc": "Portal journalists and editors"},
    ]
    _author = random.choice(_news_authors_pl if lang_pl else _news_authors_en)
    portal_name = portal.get("name", "News Portal")

    schema_blocks = []

    # NewsArticle schema (preferred over Article for news content — confirmed by Google docs)
    news_article_ld = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": title[:110],
        "description": excerpt[:155] if excerpt else "",
        "datePublished": _now_iso,
        "dateModified": _now_iso,
        "author": {
            "@type": "Person",
            "name": _author["name"],
            "description": _author["desc"],
        },
        "publisher": {
            "@type": "Organization",
            "name": portal_name,
        },
        "mainEntityOfPage": {"@type": "WebPage"},
        "inLanguage": portal_language,
    }
    if source_urls:
        news_article_ld["citation"] = source_urls[:3]
    schema_blocks.append(news_article_ld)

    # FAQPage schema if article has H3 Q&A patterns
    faq_pairs = re.findall(r'<h3[^>]*>(.*?)</h3>\s*<p>(.*?)</p>', content, re.DOTALL | re.IGNORECASE)
    if faq_pairs and len(faq_pairs) >= 2:
        faq_ld = {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": re.sub(r'<[^>]+>', '', q).strip(),
                    "acceptedAnswer": {
                        "@type": "Answer",
                        "text": re.sub(r'<[^>]+>', '', a).strip()
                    }
                }
                for q, a in faq_pairs[:6]
            ]
        }
        schema_blocks.append(faq_ld)

    # Inject schema
    if schema_blocks:
        all_schema = "\n".join(
            f'<script type="application/ld+json">{json.dumps(s, ensure_ascii=False)}</script>'
            for s in schema_blocks
        )
        content = all_schema + "\n" + content

    # SEO #47: content freshness signal for news articles
    from datetime import datetime as _dt47, timezone as _tz47
    _freshness_date = _dt47.now(_tz47.utc).strftime("%d.%m.%Y" if lang_pl else "%Y-%m-%d")
    _freshness_label = "Opublikowano" if lang_pl else "Published"
    _freshness_tag = f'<p style="font-size:0.85em;color:#666;margin-bottom:16px;"><time datetime="{_dt47.now(_tz47.utc).strftime("%Y-%m-%d")}">{_freshness_label}: {_freshness_date}</time></p>'
    content = _freshness_tag + "\n" + content

    # SEO #48: wrap content in lang div for NLP entity recognition
    content = f'<div lang="{portal_language}">\n{content}\n</div>'

    # SEO #51: deduplicate links in news article
    _seen_hrefs_news: set = set()
    def _dedup_news_link(m):
        href = re.search(r'href=["\']([^"\']+)["\']', m.group(0))
        if not href:
            return m.group(0)
        url = href.group(1).rstrip("/")
        if not url or url in ("#", "javascript:void(0)"):
            return re.sub(r"<[^>]+>", "", m.group(0))
        if url in _seen_hrefs_news:
            return re.sub(r"<[^>]+>", "", m.group(0))
        _seen_hrefs_news.add(url)
        return m.group(0)
    content = re.sub(r'<a\s[^>]*?>.*?</a>', _dedup_news_link, content, flags=re.DOTALL | re.IGNORECASE)

    # Fingerprint for dedup
    fingerprint = _content_fingerprint(content)

    _elapsed = round(time.time() - _t0, 1)
    logger.info(f"[NewsGen] Done — '{title}' | {len(sections)} sections | fp={fingerprint[:8]} | {_elapsed}s")

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


@router.post("/{portal_id}/auto-generate")
async def auto_generate(portal_id: int, max_articles: int = Query(5, ge=1, le=20)):
    """One-click: fetch RSS feeds, cluster, and auto-generate drafts for all new clusters."""
    # Step 1: fetch
    fetch_result = await fetch_and_cluster(portal_id)

    # Step 2: get unprocessed clusters
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id FROM news_clusters WHERE portal_id = ? AND status = 'new' ORDER BY created_at DESC LIMIT ?",
            (portal_id, max_articles),
        ) as cur:
            new_clusters = [dict(r) for r in await cur.fetchall()]

    # Step 3: generate a draft for each cluster
    # FIX #19: add delay between articles to respect OpenAI rate limits and avoid Firefly velocity flags
    generated = []
    errors = []
    for i, cluster in enumerate(new_clusters):
        try:
            draft = await generate_draft(portal_id, GenerateRequest(cluster_id=cluster["id"]))
            generated.append({"draft_id": draft.get("id"), "title": draft.get("title", "")})
            # Stagger requests — 2s delay between articles (rate limit + natural publishing pattern)
            if i < len(new_clusters) - 1:
                await asyncio.sleep(2)
        except Exception as e:
            errors.append(f"Cluster {cluster['id']}: {str(e)[:100]}")

    # Telegram notification
    if generated:
        try:
            from api.notifications import should_notify
            if await should_notify("notify_news_generate"):
                from services.telegram_service import send_telegram
                _titles = "\n".join(f"  • {d['title'][:60]}" for d in generated[:5])
                await send_telegram(
                    f"<b>News Portal — wygenerowano {len(generated)} art.</b>\n\n"
                    f"Portal ID: {portal_id}\n"
                    f"{_titles}"
                    + (f"\n  ... i {len(generated) - 5} wiecej" if len(generated) > 5 else "")
                )
        except Exception:
            pass

    return {
        "fetch": fetch_result,
        "generated": len(generated),
        "drafts": generated,
        "errors": errors if errors else None,
    }


async def _safe_image_prompt(title: str) -> str:
    """Transform a news title into a safe, visual image prompt.

    Strips sensitive terms (war, weapons, attacks, violence, politicians)
    and rephrases into a neutral editorial/journalistic illustration prompt
    that won't trigger content policy filters on Flux/Gemini/DALL-E.
    """
    # Quick GPT call to rephrase — cheaper than a failed image generation
    try:
        _client, _model, _is_custom = await get_openai_client()
        resp = await _client.chat.completions.create(
            model=_model,
            max_tokens=80,
            temperature=0.3,
            messages=[{
                "role": "system",
                "content": (
                    "You convert news article titles into safe image generation prompts. "
                    "Rules: 1) Describe a calm, editorial-style photo or illustration that represents the topic. "
                    "2) NEVER mention weapons, explosions, blood, death, specific politicians by name, military attacks, or violence. "
                    "3) Replace conflict topics with diplomatic/symbolic imagery (flags, handshakes, maps, city skylines, press conferences). "
                    "4) Replace crime/disaster with neutral scenes (courtroom, emergency services, community). "
                    "5) Keep it under 60 words. 6) Output ONLY the prompt, no quotes or explanation. "
                    "7) Start with a visual style like 'Editorial photo:', 'News illustration:', or 'Journalistic photograph:'."
                ),
            }, {
                "role": "user",
                "content": title,
            }],
        )
        prompt = resp.choices[0].message.content.strip()
        if prompt and len(prompt) > 10:
            logger.info(f"[NewsApprove] Safe prompt: '{title[:40]}' → '{prompt[:60]}'")
            return prompt
    except Exception as e:
        logger.warning(f"[NewsApprove] Safe prompt GPT failed: {e}")
    # Fallback: generic editorial prompt based on stripped title
    _stripped = re.sub(
        r'\b(wojna|atak|uderzenie|rakiet|bomb|zabił|śmierć|broń|strzelani|terror|iran|rosj|ukrain|putin|trump|hamas|gaza)\w*\b',
        '', title, flags=re.IGNORECASE
    ).strip()
    _stripped = re.sub(r'\s{2,}', ' ', _stripped).strip(' -—:,.')
    if len(_stripped) < 10:
        _stripped = "current events news editorial"
    return f"Editorial news photograph: {_stripped}, professional journalism, neutral tone, press agency style"


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
        # FIX #70: also prevent re-approving rejected drafts — they should be re-created or edited first
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

    # SEO #86: pass keyword and tags for Yoast/RankMath meta optimization
    # Extract main keyword from title (first 3 significant words)
    _title_words = [w for w in re.findall(r'\w{3,}', draft["title"]) if len(w) >= 4][:3]
    _news_keyword = " ".join(_title_words) if _title_words else draft["title"][:50]
    # Generate tags from title words
    _news_tags = [w for w in re.findall(r'\w{4,}', draft["title"].lower())][:8]

    # Generate featured image (AI fallback chain: Flux → Gemini → none)
    # Sanitize title into a safe, visual image prompt (avoid content policy blocks
    # on war/violence/politics topics — rephrase to neutral editorial illustration)
    _image_b64 = None
    _image_prompt = await _safe_image_prompt(draft["title"])
    try:
        from services.freepik_generate_service import generate_image_flux
        _image_b64 = await generate_image_flux(_image_prompt)
        logger.info(f"[NewsApprove] Flux image OK for '{draft['title'][:40]}'")
    except Exception as _img_err:
        logger.warning(f"[NewsApprove] Flux failed: {_img_err}")
        try:
            from services.gemini_image_service import generate_image_gemini
            _image_b64 = await generate_image_gemini(_image_prompt)
            logger.info(f"[NewsApprove] Gemini image fallback OK")
        except Exception as _img_err2:
            logger.warning(f"[NewsApprove] All image sources failed: {_img_err2}")

    # Publish to WordPress
    result = await publish_post(
        domain=domain["domain"],
        wp_login=domain["wp_login"],
        wp_pass=domain["wp_pass"],  # publish_post handles decryption internally
        title=draft["title"],
        content=draft["content"],
        image_b64=_image_b64,
        excerpt=draft.get("excerpt", ""),
        keyword=_news_keyword,
        tags=_news_tags if _news_tags else None,
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

    # Telegram notification
    try:
        from api.notifications import should_notify
        if await should_notify("notify_news_approve"):
            from services.telegram_service import send_telegram
            _wp_url = result.get("url", "")
            await send_telegram(
                f"<b>News Portal — opublikowano</b>\n\n"
                f"{draft['title'][:80]}\n"
                f"Domena: <b>{domain['domain']}</b>\n"
                f"URL: {_wp_url}"
            )
    except Exception:
        pass

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


# ── News Autopilot ────────────────────────────────────────────────────────────


async def run_news_autopilot():
    """Core autopilot: for each auto_publish portal, fetch → generate → publish.

    Respects posts_per_day limit and check_interval_min between runs.
    """
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT np.*, md.domain, md.wp_login, md.wp_pass,
                      COALESCE(md.http_user,'') as http_user,
                      COALESCE(md.http_pass,'') as http_pass
               FROM news_portals np
               JOIN my_domains md ON md.id = np.my_domain_id
               WHERE np.auto_publish = 1 AND np.active = 1"""
        ) as cur:
            portals = [dict(r) for r in await cur.fetchall()]

    if not portals:
        logger.info("[NewsAutopilot] No auto-publish portals configured")
        return {"portals_processed": 0}

    total_published = 0
    total_errors = 0
    portal_results = []

    for portal in portals:
        pid = portal["id"]
        interval = portal.get("check_interval_min", 30) or 30
        limit = portal.get("posts_per_day", 5) or 5

        # Check if enough time passed since last run
        last = _autopilot_status.get(pid, {})
        if last.get("last_run"):
            elapsed_min = (datetime.now(timezone.utc) - datetime.fromisoformat(last["last_run"])).total_seconds() / 60
            if elapsed_min < interval:
                logger.info(f"[NewsAutopilot] Portal {pid} ({portal['name']}): skipping, {elapsed_min:.0f}/{interval}min elapsed")
                portal_results.append({"portal_id": pid, "skipped": True, "reason": f"{elapsed_min:.0f}/{interval}min"})
                continue

        _autopilot_status[pid] = {**last, "running": True}
        published = 0
        errors = 0

        try:
            # Step 1: fetch & cluster
            logger.info(f"[NewsAutopilot] Portal {pid} ({portal['name']}): fetching RSS...")
            fetch_result = await fetch_and_cluster(pid)
            new_items = fetch_result.get("new_items", 0)
            new_clusters = fetch_result.get("new_clusters", 0)
            logger.info(f"[NewsAutopilot] Portal {pid}: {new_items} items, {new_clusters} clusters")

            # Step 2: get unprocessed clusters (up to posts_per_day)
            # Also count how many already published today
            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0).isoformat()
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT COUNT(*) as cnt FROM news_drafts WHERE portal_id=? AND status='published' AND published_at>=?",
                    (pid, today_start),
                ) as cur:
                    today_count = (await cur.fetchone())["cnt"]

            remaining = max(0, limit - today_count)
            if remaining == 0:
                logger.info(f"[NewsAutopilot] Portal {pid}: already published {today_count}/{limit} today, skipping")
                portal_results.append({"portal_id": pid, "skipped": True, "reason": f"limit {today_count}/{limit}"})
                now = datetime.now(timezone.utc)
                _autopilot_status[pid] = {
                    "last_run": now.isoformat(),
                    "next_run": (now + timedelta(minutes=interval)).isoformat(),
                    "published": 0, "errors": 0, "running": False,
                }
                continue

            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT id FROM news_clusters WHERE portal_id=? AND status='new' ORDER BY created_at DESC LIMIT ?",
                    (pid, remaining),
                ) as cur:
                    clusters = [dict(r) for r in await cur.fetchall()]

            # Step 3: generate + auto-approve each
            for i, cluster in enumerate(clusters):
                try:
                    draft = await generate_draft(pid, GenerateRequest(cluster_id=cluster["id"]))
                    draft_id = draft.get("id")
                    if draft_id:
                        # Auto-approve (publish to WP)
                        pub = await approve_draft(draft_id)
                        published += 1
                        logger.info(f"[NewsAutopilot] Portal {pid}: published '{draft.get('title', '')[:50]}' → {pub.get('wp_post_url', '')}")
                    # Stagger between articles
                    if i < len(clusters) - 1:
                        await asyncio.sleep(random.randint(3, 8))
                except Exception as e:
                    errors += 1
                    logger.warning(f"[NewsAutopilot] Portal {pid} cluster {cluster['id']}: {e}")

        except Exception as e:
            errors += 1
            logger.error(f"[NewsAutopilot] Portal {pid} failed: {e}", exc_info=True)

        total_published += published
        total_errors += errors
        now = datetime.now(timezone.utc)
        _autopilot_status[pid] = {
            "last_run": now.isoformat(),
            "next_run": (now + timedelta(minutes=interval)).isoformat(),
            "published": published,
            "errors": errors,
            "running": False,
        }
        portal_results.append({"portal_id": pid, "published": published, "errors": errors})

        # Telegram notification
        if published:
            try:
                from api.notifications import should_notify
                if await should_notify("notify_news_approve"):
                    from services.telegram_service import send_telegram
                    await send_telegram(
                        f"<b>News Autopilot — {portal['name']}</b>\n\n"
                        f"Opublikowano: <b>{published}</b> artykułów\n"
                        f"Domena: {portal['domain']}\n"
                        f"Błędy: {errors}"
                    )
            except Exception:
                pass

    logger.info(f"[NewsAutopilot] Done: {total_published} published, {total_errors} errors across {len(portals)} portals")
    return {
        "portals_processed": len(portals),
        "total_published": total_published,
        "total_errors": total_errors,
        "details": portal_results,
    }


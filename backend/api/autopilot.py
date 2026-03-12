"""
PBN Autopilot — automatyczne uzupełnianie treści na domenach PBN.

Przepływ:
1. Przypisz domenę PBN → seed keyword + ustawienia
2. Generuj Topical Map (DataForSEO) → lista fraz (pillar + supporting)
3. Scheduler publikuje X artykułów dziennie na każdej domenie
   - Pobiera pending keyword z kolejki
   - Generuje unikalny artykuł (OpenAI)
   - Publikuje na WP danej domeny
"""
import asyncio
import json as _json
import logging
import os
import random
import uuid as _uuid
from datetime import date, datetime
from typing import List, Optional

import aiosqlite
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from config import DB_PATH
from services.topical_map_service import generate_topical_map
from services.openai_service import generate_article, generate_image
from services.freepik_service import generate_image_freepik
from services.freepik_generate_service import generate_image_zimage, generate_image_flux
from services.wordpress_service import publish_post, get_or_create_category, get_categories, check_wp_credentials

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/autopilot", tags=["autopilot"])

# Limit max 3 concurrent article generations during the daily run
_DAILY_SEM = asyncio.Semaphore(3)

DFS_LOGIN = os.getenv("DATAFORSEO_LOGIN", "")
DFS_PASSWORD = os.getenv("DATAFORSEO_PASSWORD", "")

VARIATION_HINTS = [
    "praktyczny poradnik krok po kroku",
    "porównanie dostępnych opcji i rozwiązań",
    "najczęstsze błędy i jak ich unikać",
    "perspektywa eksperta branżowego",
    "korzyści i zastosowania w praktyce",
    "case study i przykłady z życia",
    "trendy i nowości w branży",
    "przewodnik dla początkujących",
    "zaawansowane techniki i strategie",
    "najważniejsze fakty i mity",
    "jak wybrać najlepsze rozwiązanie",
    "oszczędność czasu i pieniędzy",
    "bezpieczeństwo i na co uważać",
]


# ── Models ──────────────────────────────────────────────────────────────────

class ScheduleCreate(BaseModel):
    my_domain_id: int
    seed_keyword: str
    posts_per_day: int = 1
    language: str = "pl"
    min_volume: int = 10
    client_domain: str = ""
    anchor_text: str = ""
    image_source: str = "freepik_stock"  # freepik_stock | gemini | dalle | none
    custom_prompt: str = ""


class ScheduleUpdate(BaseModel):
    posts_per_day: Optional[int] = None
    active: Optional[int] = None
    client_domain: Optional[str] = None
    anchor_text: Optional[str] = None
    image_source: Optional[str] = None
    custom_prompt: Optional[str] = None
    min_volume: Optional[int] = None
    language: Optional[str] = None


class RunNowRequest(BaseModel):
    schedule_id: int
    limit: Optional[int] = None  # nadpisuje posts_per_day


# In-flight map generation guard — prevents double-click race condition
_map_generating: set = set()

# ── DB helpers ───────────────────────────────────────────────────────────────

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS domain_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    my_domain_id INTEGER NOT NULL REFERENCES my_domains(id) ON DELETE CASCADE,
    seed_keyword TEXT NOT NULL,
    posts_per_day INTEGER DEFAULT 1,
    language TEXT DEFAULT 'pl',
    min_volume INTEGER DEFAULT 10,
    client_domain TEXT DEFAULT '',
    anchor_text TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    map_generated INTEGER DEFAULT 0,
    total_keywords INTEGER DEFAULT 0,
    published_count INTEGER DEFAULT 0,
    last_run_at TEXT,
    image_source TEXT DEFAULT 'freepik_stock',
    custom_prompt TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS domain_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL REFERENCES domain_schedules(id) ON DELETE CASCADE,
    my_domain_id INTEGER NOT NULL,
    keyword TEXT NOT NULL,
    keyword_type TEXT DEFAULT 'supporting',
    pillar_label TEXT DEFAULT '',
    pillar_anchor TEXT DEFAULT '',
    search_volume INTEGER DEFAULT 0,
    keyword_difficulty REAL DEFAULT 0,
    status TEXT DEFAULT 'pending',
    wp_post_url TEXT DEFAULT '',
    wp_category_id INTEGER DEFAULT NULL,
    title TEXT DEFAULT '',
    published_at TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS domain_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL REFERENCES domain_schedules(id) ON DELETE CASCADE,
    my_domain_id INTEGER NOT NULL,
    pillar_anchor TEXT NOT NULL,
    pillar_label TEXT NOT NULL,
    wp_category_id INTEGER DEFAULT NULL,
    wp_category_slug TEXT DEFAULT '',
    synced INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(schedule_id, pillar_anchor)
);

CREATE TABLE IF NOT EXISTS run_jobs (
    job_id TEXT PRIMARY KEY,
    schedule_id INTEGER,
    status TEXT DEFAULT 'running',
    published INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    total INTEGER DEFAULT 0,
    total_published INTEGER DEFAULT 0,
    results_json TEXT DEFAULT '[]',
    error TEXT DEFAULT '',
    done INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


_tables_ensured = False


async def ensure_tables():
    global _tables_ensured
    if _tables_ensured:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_TABLES)
        # Migracje — dodaj kolumny jeśli nie istnieją
        for col, typedef in [
            ("wp_category_id", "INTEGER DEFAULT NULL"),
            ("pillar_anchor", "TEXT DEFAULT ''"),
            ("title", "TEXT DEFAULT ''"),
        ]:
            try:
                await db.execute(f"ALTER TABLE domain_keywords ADD COLUMN {col} {typedef}")
            except Exception:
                pass
        # Migracja domain_schedules
        for col, typedef in [
            ("image_source", "TEXT DEFAULT 'freepik_stock'"),
            ("custom_prompt", "TEXT DEFAULT ''"),
        ]:
            try:
                await db.execute(f"ALTER TABLE domain_schedules ADD COLUMN {col} {typedef}")
            except Exception:
                pass
        await db.commit()
    _tables_ensured = True


async def get_schedule(db, schedule_id: int) -> Optional[dict]:
    db.row_factory = aiosqlite.Row
    async with db.execute(
        """SELECT s.*, md.domain, md.wp_login, md.wp_pass,
                  COALESCE(md.http_user,'') as http_user,
                  COALESCE(md.http_pass,'') as http_pass
           FROM domain_schedules s
           JOIN my_domains md ON md.id = s.my_domain_id
           WHERE s.id = ?""",
        (schedule_id,)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/schedules")
async def list_schedules():
    """Lista wszystkich harmonogramów z domenami."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT s.*, md.domain, md.active as domain_active, md.wp_ok
               FROM domain_schedules s
               JOIN my_domains md ON md.id = s.my_domain_id
               ORDER BY s.created_at DESC"""
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


@router.post("/schedules")
async def create_schedule(body: ScheduleCreate):
    """Utwórz harmonogram dla domeny PBN."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        # Sprawdź czy domena istnieje
        async with db.execute("SELECT id FROM my_domains WHERE id = ?", (body.my_domain_id,)) as cur:
            if not await cur.fetchone():
                from fastapi import HTTPException
                raise HTTPException(404, "Domena nie istnieje")
        # Sprawdź duplikat
        async with db.execute(
            "SELECT id FROM domain_schedules WHERE my_domain_id = ? AND seed_keyword = ?",
            (body.my_domain_id, body.seed_keyword)
        ) as cur:
            existing = await cur.fetchone()
        if existing:
            return {"id": existing[0], "message": "Harmonogram już istnieje"}

        cursor = await db.execute(
            """INSERT INTO domain_schedules
               (my_domain_id, seed_keyword, posts_per_day, language, min_volume, client_domain, anchor_text, image_source, custom_prompt)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (body.my_domain_id, body.seed_keyword, body.posts_per_day,
             body.language, body.min_volume, body.client_domain, body.anchor_text,
             body.image_source, body.custom_prompt)
        )
        schedule_id = cursor.lastrowid
        await db.commit()
    return {"id": schedule_id, "message": "Harmonogram utworzony. Wygeneruj Topical Map."}


@router.patch("/schedules/{schedule_id}")
async def update_schedule(schedule_id: int, body: ScheduleUpdate):
    """Aktualizuj ustawienia harmonogramu."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        if body.posts_per_day is not None:
            await db.execute("UPDATE domain_schedules SET posts_per_day=? WHERE id=?", (body.posts_per_day, schedule_id))
        if body.active is not None:
            await db.execute("UPDATE domain_schedules SET active=? WHERE id=?", (body.active, schedule_id))
        if body.client_domain is not None:
            await db.execute("UPDATE domain_schedules SET client_domain=? WHERE id=?", (body.client_domain, schedule_id))
        if body.anchor_text is not None:
            await db.execute("UPDATE domain_schedules SET anchor_text=? WHERE id=?", (body.anchor_text, schedule_id))
        if body.image_source is not None:
            await db.execute("UPDATE domain_schedules SET image_source=? WHERE id=?", (body.image_source, schedule_id))
        if body.custom_prompt is not None:
            await db.execute("UPDATE domain_schedules SET custom_prompt=? WHERE id=?", (body.custom_prompt, schedule_id))
        if body.min_volume is not None:
            await db.execute("UPDATE domain_schedules SET min_volume=? WHERE id=?", (body.min_volume, schedule_id))
        if body.language is not None:
            await db.execute("UPDATE domain_schedules SET language=? WHERE id=?", (body.language, schedule_id))
        await db.commit()
    return {"ok": True}


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: int):
    """Usuń harmonogram i wszystkie słowa kluczowe."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM domain_schedules WHERE id=?", (schedule_id,))
        await db.commit()
    return {"deleted": schedule_id}


@router.get("/schedules/{schedule_id}/keywords")
async def list_keywords(schedule_id: int, status: Optional[str] = None):
    """Lista słów kluczowych dla harmonogramu."""
    await ensure_tables()
    cond = "WHERE schedule_id = ?"
    params = [schedule_id]
    if status:
        cond += " AND status = ?"
        params.append(status)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM domain_keywords {cond} ORDER BY keyword_type DESC, search_volume DESC",
            params
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


@router.post("/schedules/{schedule_id}/generate-map")
async def generate_map_for_schedule(schedule_id: int, force_refresh: bool = False):
    """
    Wygeneruj Topical Map dla harmonogramu i zapisz frazy do kolejki.
    Jeśli mapa już istnieje — dodaje tylko nowe frazy.
    force_refresh=true pomija cache DataForSEO (kosztowne, używaj tylko gdy potrzebujesz świeżych danych).
    """
    await ensure_tables()

    # Idempotency guard — prevent concurrent double-generation for same schedule
    if schedule_id in _map_generating:
        from fastapi import HTTPException
        raise HTTPException(409, "Generowanie mapy już w toku dla tego harmonogramu")
    _map_generating.add(schedule_id)
    try:
        return await _do_generate_map(schedule_id, force_refresh=force_refresh)
    finally:
        _map_generating.discard(schedule_id)


async def _do_generate_map(schedule_id: int, force_refresh: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        sched = await get_schedule(db, schedule_id)
    if not sched:
        from fastapi import HTTPException
        raise HTTPException(404, "Harmonogram nie istnieje")

    if not DFS_LOGIN or not DFS_PASSWORD:
        from fastapi import HTTPException
        raise HTTPException(400, "Brak konfiguracji DataForSEO (DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD)")

    # Generuj mapę
    tmap = await generate_topical_map(
        seed=sched["seed_keyword"],
        location_code=2616 if sched["language"] == "pl" else 2840,
        language_code=sched["language"],
        min_volume=sched["min_volume"],
        max_clusters=8,
        dfs_login=DFS_LOGIN,
        dfs_password=DFS_PASSWORD,
        force_refresh=force_refresh,
    )

    # ── Cannibalization guard ────────────────────────────────────────────────
    import re as _re_cann

    def _kw_stem(kw: str) -> str:
        tokens = _re_cann.sub(r"[^a-z0-9ąćęłńóśźż ]+", "", kw.lower()).split()
        return " ".join(tokens[:2])

    inserted = 0
    cannibal_flagged = 0
    async with aiosqlite.connect(DB_PATH) as db:
        # Pobierz istniejące frazy żeby nie duplikować
        async with db.execute(
            "SELECT keyword FROM domain_keywords WHERE schedule_id = ?", (schedule_id,)
        ) as cur:
            existing_kws = {row[0] for row in await cur.fetchall()}

        # Also exclude keywords already published on this domain (cross-schedule dedup)
        async with db.execute(
            "SELECT DISTINCT keyword FROM domain_keywords WHERE my_domain_id=? AND status='published'",
            (sched["my_domain_id"],)
        ) as cur:
            domain_published_kws = {row[0] for row in await cur.fetchall()}

        # Also exclude titles from manual publisher (posts table) — avoid content overlap
        async with db.execute(
            "SELECT DISTINCT title FROM posts WHERE my_domain_id=? AND status='published'",
            (sched["my_domain_id"],)
        ) as cur:
            manual_titles = {row[0] for row in await cur.fetchall()}

        existing_kws |= domain_published_kws

        # Build stem index from already-published keywords + manual post titles for cannibalization detection
        published_stems: dict[str, str] = {_kw_stem(kw): kw for kw in domain_published_kws}
        for title in manual_titles:
            stem = _kw_stem(title)
            published_stems.setdefault(stem, title)

        for pillar in tmap.get("pillars", []):
            anchor = pillar["anchor"]
            label = pillar["label"]

            # Zapisz klaster do domain_categories (jeśli nie istnieje)
            await db.execute(
                """INSERT OR IGNORE INTO domain_categories
                   (schedule_id, my_domain_id, pillar_anchor, pillar_label)
                   VALUES (?,?,?,?)""",
                (schedule_id, sched["my_domain_id"], anchor, label)
            )

            # Pillar keyword
            pk = pillar["pillar_keyword"]
            if pk and pk not in existing_kws:
                stem = _kw_stem(pk)
                init_status = "cannibal_risk" if stem in published_stems else "pending"
                if init_status == "cannibal_risk":
                    cannibal_flagged += 1
                await db.execute(
                    """INSERT INTO domain_keywords
                       (schedule_id, my_domain_id, keyword, keyword_type, pillar_label, pillar_anchor, search_volume, keyword_difficulty, status)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (schedule_id, sched["my_domain_id"], pk, "pillar",
                     label, anchor, pillar.get("pillar_volume", 0), pillar.get("pillar_difficulty", 0), init_status)
                )
                existing_kws.add(pk)
                published_stems.setdefault(stem, pk)
                inserted += 1

            # Supporting keywords
            for sk in pillar.get("supporting_keywords", []):
                kw = sk["keyword"]
                if kw and kw not in existing_kws:
                    stem = _kw_stem(kw)
                    init_status = "cannibal_risk" if stem in published_stems else "pending"
                    if init_status == "cannibal_risk":
                        cannibal_flagged += 1
                    await db.execute(
                        """INSERT INTO domain_keywords
                           (schedule_id, my_domain_id, keyword, keyword_type, pillar_label, pillar_anchor, search_volume, keyword_difficulty, status)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (schedule_id, sched["my_domain_id"], kw, "supporting",
                         label, anchor, sk.get("search_volume", 0), sk.get("keyword_difficulty", 0), init_status)
                    )
                    existing_kws.add(kw)
                    published_stems.setdefault(stem, kw)
                    inserted += 1

        # Policz łącznie
        async with db.execute(
            "SELECT COUNT(*) FROM domain_keywords WHERE schedule_id = ?", (schedule_id,)
        ) as cur:
            total = (await cur.fetchone())[0]

        await db.execute(
            "UPDATE domain_schedules SET map_generated=1, total_keywords=? WHERE id=?",
            (total, schedule_id)
        )
        await db.commit()

    return {
        "inserted": inserted,
        "total_keywords": total,
        "pillars": len(tmap.get("pillars", [])),
        "cannibal_flagged": cannibal_flagged,
        "site_metrics": tmap.get("site_metrics", {}),
    }


@router.get("/schedules/{schedule_id}/categories")
async def list_categories(schedule_id: int):
    """Lista klastrów (kategorii WP) dla harmonogramu."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM domain_categories WHERE schedule_id=? ORDER BY id",
            (schedule_id,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


@router.get("/schedules/{schedule_id}/cannibalization")
async def check_cannibalization(schedule_id: int):
    """
    Wykrywa kanibalizację słów kluczowych na domenie.
    Grupuje frazy o podobnym rdzeniu (pierwsze 2 tokeny) i zwraca kolizje.
    """
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT my_domain_id FROM domain_schedules WHERE id=?", (schedule_id,)
        ) as cur:
            sched_row = await cur.fetchone()
        if not sched_row:
            from fastapi import HTTPException
            raise HTTPException(404, "Schedule not found")
        my_domain_id = sched_row["my_domain_id"]

        # All keywords (all schedules) on this domain
        async with db.execute(
            """SELECT dk.id, dk.keyword, dk.keyword_type, dk.status, dk.wp_post_url,
                      dk.schedule_id, ds.seed_keyword as schedule_name
               FROM domain_keywords dk
               JOIN domain_schedules ds ON ds.id = dk.schedule_id
               WHERE dk.my_domain_id = ?
               ORDER BY dk.keyword""",
            (my_domain_id,)
        ) as cur:
            all_kws = [dict(r) for r in await cur.fetchall()]

    # Group by normalised 2-token stem — simple but effective for Polish/English
    import re as _re
    def _stem(kw: str) -> str:
        tokens = _re.sub(r"[^a-z0-9ąćęłńóśźż ]+", "", kw.lower()).split()
        return " ".join(tokens[:2])

    groups: dict[str, list] = {}
    for row in all_kws:
        stem = _stem(row["keyword"])
        groups.setdefault(stem, []).append(row)

    collisions = []
    for stem, items in groups.items():
        if len(items) < 2:
            continue
        published = [i for i in items if i["status"] == "published"]
        pending = [i for i in items if i["status"] == "pending"]
        collisions.append({
            "stem": stem,
            "count": len(items),
            "published": len(published),
            "pending": len(pending),
            "keywords": [
                {
                    "id": i["id"],
                    "keyword": i["keyword"],
                    "type": i["keyword_type"],
                    "status": i["status"],
                    "url": i["wp_post_url"],
                    "schedule_id": i["schedule_id"],
                }
                for i in items
            ],
        })

    collisions.sort(key=lambda x: x["count"], reverse=True)
    return {
        "total_keywords": len(all_kws),
        "collision_groups": len(collisions),
        "collisions": collisions,
    }


@router.post("/schedules/{schedule_id}/sync-categories")
async def sync_categories(schedule_id: int):
    """
    Tworzy kategorie w WordPress dla każdego klastra Topical Map.
    Jeden klaster = jedna kategoria w menu WP.
    Zapisuje wp_category_id do domain_categories i aktualizuje domain_keywords.
    """
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        sched = await get_schedule(db, schedule_id)
        if not sched:
            from fastapi import HTTPException
            raise HTTPException(404, "Harmonogram nie istnieje")

        db.row_factory = aiosqlite.Row

        # Backfill domain_categories z domain_keywords
        # Obsługuje stare mapy (pillar_anchor pusty) i nowe (pillar_anchor wypełniony)
        async with db.execute(
            """SELECT DISTINCT
                   COALESCE(NULLIF(pillar_anchor,''), pillar_label) as anchor,
                   pillar_label
               FROM domain_keywords
               WHERE schedule_id=? AND pillar_label != ''""",
            (schedule_id,)
        ) as cur:
            kw_clusters = await cur.fetchall()

        for row in kw_clusters:
            await db.execute(
                """INSERT OR IGNORE INTO domain_categories
                   (schedule_id, my_domain_id, pillar_anchor, pillar_label)
                   VALUES (?,?,?,?)""",
                (schedule_id, sched["my_domain_id"], row["anchor"], row["pillar_label"])
            )
        await db.commit()

        async with db.execute(
            "SELECT * FROM domain_categories WHERE schedule_id=?", (schedule_id,)
        ) as cur:
            categories = [dict(r) for r in await cur.fetchall()]

    if not categories:
        return {"synced": 0, "message": "Brak klastrów — najpierw wygeneruj mapę (↻ Mapa)"}

    results = []
    for cat in categories:
        label = cat["pillar_label"]
        anchor = cat["pillar_anchor"]

        # Generuj slug z anchora (ascii, myślniki)
        import re as _re
        slug = _re.sub(r"[^a-z0-9]+", "-", anchor.lower()).strip("-")

        cat_id = await get_or_create_category(
            domain=sched["domain"],
            wp_login=sched["wp_login"],
            wp_pass=sched["wp_pass"],
            name=label,
            slug=slug,
            http_user=sched.get("http_user", ""),
            http_pass=sched.get("http_pass", ""),
        )

        if cat_id:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE domain_categories SET wp_category_id=?, wp_category_slug=?, synced=1 WHERE id=?",
                    (cat_id, slug, cat["id"])
                )
                # Zaktualizuj wszystkie frazy z tego klastra
                await db.execute(
                    "UPDATE domain_keywords SET wp_category_id=? WHERE schedule_id=? AND pillar_anchor=?",
                    (cat_id, schedule_id, anchor)
                )
                await db.commit()
            results.append({"label": label, "slug": slug, "wp_category_id": cat_id, "ok": True})
            logger.info(f"[Autopilot] Category created/found: '{label}' (ID={cat_id}) on {sched['domain']}")
        else:
            results.append({"label": label, "slug": slug, "wp_category_id": None, "ok": False})
            logger.warning(f"[Autopilot] Failed to create category '{label}' on {sched['domain']}")

    synced = sum(1 for r in results if r["ok"])
    return {"synced": synced, "total": len(results), "categories": results}


async def _job_create(job_id: str, schedule_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO run_jobs (job_id, schedule_id, status, published, failed, total, results_json, done) VALUES (?,?,?,?,?,?,?,?)",
            (job_id, schedule_id, 'running', 0, 0, 0, '[]', 0)
        )
        await db.commit()


async def _job_update(job_id: str, **kwargs):
    if 'results' in kwargs:
        kwargs['results_json'] = _json.dumps(kwargs.pop('results'), ensure_ascii=False)
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [job_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE run_jobs SET {sets} WHERE job_id=?", vals)
        await db.commit()


async def _job_get(job_id: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM run_jobs WHERE job_id=?", (job_id,)) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    d = dict(row)
    d['results'] = _json.loads(d.pop('results_json', '[]'))
    d['done'] = bool(d['done'])
    return d


async def _with_retry(coro_fn, max_attempts: int = 3, base_delay: float = 2.0):
    """Retry async coroutine with exponential backoff. coro_fn is a zero-arg async callable."""
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return await coro_fn()
        except Exception as e:
            last_exc = e
            if attempt < max_attempts - 1:
                wait = base_delay * (2 ** attempt)  # 2s, 4s, 8s
                logger.warning(f"[Retry] attempt {attempt+1}/{max_attempts} failed: {e} — retrying in {wait:.0f}s")
                await asyncio.sleep(wait)
    raise last_exc


async def _get_pillar_url(schedule_id: int, my_domain_id: int, pillar_anchor: str) -> tuple[str, str]:
    """
    Returns (pillar_wp_url, pillar_keyword) for a given pillar_anchor in a schedule.
    Used to inject internal link from supporting articles → pillar page.
    """
    if not pillar_anchor:
        return "", ""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT wp_post_url, keyword FROM domain_keywords
               WHERE schedule_id=? AND my_domain_id=? AND pillar_anchor=?
                 AND keyword_type='pillar' AND status='published' AND wp_post_url!=''
               LIMIT 1""",
            (schedule_id, my_domain_id, pillar_anchor)
        ) as cur:
            row = await cur.fetchone()
    if row:
        return row[0], row[1]
    return "", ""


async def _fetch_image(image_source: str, keyword: str, title: str, img_prompt: str) -> tuple[str | None, str]:
    """
    Fetch image based on image_source setting.
    Returns (base64_str_or_None, provider_name).
    image_source: 'freepik_stock' | 'freepik_zimage' | 'freepik_flux' | 'gemini' | 'dalle' | 'none'
    """
    if image_source == "none":
        return None, "none"

    providers = {
        "freepik_stock": [("freepik_stock", lambda: generate_image_freepik(keyword))],
        "freepik_zimage": [
            ("freepik_zimage", lambda: generate_image_zimage(img_prompt)),
            ("freepik_stock", lambda: generate_image_freepik(keyword)),
        ],
        "freepik_flux": [
            ("freepik_flux", lambda: generate_image_flux(img_prompt)),
            ("freepik_stock", lambda: generate_image_freepik(keyword)),
        ],
        "dalle": [
            ("dalle", lambda: generate_image(f"Professional illustration for article about: {title}. Clean, modern, no text.")),
            ("freepik_stock", lambda: generate_image_freepik(keyword)),
        ],
    }
    order = providers.get(image_source, providers["freepik_stock"])

    for _provider, _fn in order:
        try:
            img = await _fn()
            return img, _provider
        except Exception as e:
            logger.warning(f"[Image] {_provider} failed for '{keyword}': {e}")
            _fetch_image._last_errors = getattr(_fetch_image, "_last_errors", {})
            _fetch_image._last_errors[_provider] = str(e)
    return None, "none"


async def _run_job(job_id: str, schedule_id: int, body: RunNowRequest):
    """Background task: generate and publish articles, persisting status to run_jobs table."""
    try:
        await ensure_tables()
        async with aiosqlite.connect(DB_PATH) as db:
            sched = await get_schedule(db, schedule_id)
        if not sched:
            await _job_update(job_id, done=1, error="Harmonogram nie istnieje")
            return

        limit = body.limit if body.limit else sched["posts_per_day"]

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM domain_keywords
                   WHERE schedule_id = ? AND status = 'pending'
                   ORDER BY keyword_type DESC, search_volume DESC
                   LIMIT ?""",
                (schedule_id, limit)
            ) as cur:
                keywords = [dict(r) for r in await cur.fetchall()]

        if not keywords:
            await _job_update(job_id, done=1, error="Brak pending keywords")
            return

        wp_ok = await check_wp_credentials(sched["domain"], sched["wp_login"], sched["wp_pass"],
                                           http_user=sched.get("http_user", ""), http_pass=sched.get("http_pass", ""))
        if not wp_ok:
            logger.warning(f"[Autopilot] WP credentials invalid for {sched['domain']} — aborting")
            await _job_update(job_id, done=1, error=f"WP credentials invalid for {sched['domain']}")
            return

        await _job_update(job_id, total=len(keywords))

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT title, keyword, wp_post_url FROM domain_keywords
                   WHERE schedule_id=? AND status='published' AND wp_post_url!=''
                   ORDER BY published_at DESC LIMIT 50""",
                (schedule_id,)
            ) as cur:
                published_posts = [{"title": r["title"] or r["keyword"], "keyword": r["keyword"], "url": r["wp_post_url"]} for r in await cur.fetchall()]

        domain_fingerprints: set = set()
        _published = 0
        _failed = 0
        _results = []

        for kw_row in keywords:
            keyword = kw_row["keyword"]

            # Skip if already published on this domain (race condition guard)
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT COUNT(*) FROM domain_keywords WHERE my_domain_id=? AND keyword=? AND status='published'",
                    (sched["my_domain_id"], keyword)
                ) as cur:
                    already = (await cur.fetchone())[0]
            if already:
                logger.info(f"[Autopilot] Skipping '{keyword}' — already published on this domain")
                # Mark as published to remove from pending queue
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE domain_keywords SET status='published' WHERE id=?", (kw_row["id"],))
                    await db.commit()
                continue

            variation = random.choice(VARIATION_HINTS)
            location_code = 2616 if sched["language"] == "pl" else 2840

            # Fetch pillar page URL for internal linking (supporting → pillar)
            pillar_url, pillar_keyword = "", ""
            if kw_row.get("keyword_type") == "supporting" and kw_row.get("pillar_anchor"):
                pillar_url, pillar_keyword = await _get_pillar_url(
                    schedule_id, sched["my_domain_id"], kw_row["pillar_anchor"]
                )

            try:
                _pillar_url = pillar_url
                _pillar_anchor = pillar_keyword or kw_row.get("pillar_label", "")
                async with asyncio.timeout(360):  # 6 min per artykuł
                    article = await _with_retry(lambda: generate_article(
                        topic=keyword,
                        client_domain=sched["client_domain"] or "",
                        anchor_text=sched["anchor_text"] or keyword,
                        language=sched["language"],
                        variation_hint=variation,
                        custom_prompt=sched.get("custom_prompt", "") or "",
                        dfs_login=DFS_LOGIN,
                        dfs_password=DFS_PASSWORD,
                        location_code=location_code,
                        published_posts=published_posts,
                        domain_fingerprints=domain_fingerprints,
                        pillar_page_url=_pillar_url,
                        pillar_page_anchor=_pillar_anchor,
                    ))
                    title = article["title"]
                    content = article["content"]
                    excerpt = article.get("excerpt", "")
                    lsi_tags = article.get("lsi_tags", [])
                    category_id = kw_row.get("wp_category_id") or None

                    img_prompt = (
                        f"High-quality professional photo for blog article: '{title}'. "
                        f"Topic: {keyword}. Realistic scene, natural lighting, no text, no watermarks, clean modern aesthetic."
                    )
                    image_b64, image_provider = await _fetch_image(
                        sched.get("image_source", "freepik_stock"), keyword, title, img_prompt
                    )

                    async def _do_publish():
                        r = await publish_post(
                            domain=sched["domain"],
                            wp_login=sched["wp_login"],
                            wp_pass=sched["wp_pass"],
                            title=title,
                            content=content,
                            image_b64=image_b64,
                            category_id=category_id,
                            excerpt=excerpt,
                            keyword=keyword,
                            tags=lsi_tags,
                            http_user=sched.get("http_user", ""),
                            http_pass=sched.get("http_pass", ""),
                        )
                        if not r.get("success"):
                            raise RuntimeError(r.get("error", "WP publish failed"))
                        return r
                    result = await _with_retry(_do_publish, max_attempts=2, base_delay=5.0)

                if result.get("success"):
                    wp_url = result.get("url", "")
                    _published += 1
                    published_posts.append({"title": title, "keyword": keyword, "url": wp_url})
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            """INSERT INTO posts (client_id, client_domain, my_domain_id, title, content,
                               wp_post_url, status) VALUES (?,?,?,?,?,?,?)""",
                            (None, sched["client_domain"] or sched["domain"],
                             sched["my_domain_id"], title, content, wp_url, "published")
                        )
                        await db.execute(
                            """UPDATE domain_keywords SET status='published', title=?, wp_post_url=?, published_at=?
                               WHERE id=?""",
                            (title, wp_url, datetime.utcnow().isoformat(), kw_row["id"])
                        )
                        await db.commit()
                    _results.append({"status": "published", "keyword": keyword, "url": wp_url, "title": title, "image": image_provider})
                else:
                    _failed += 1
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("UPDATE domain_keywords SET status='failed' WHERE id=?", (kw_row["id"],))
                        await db.commit()
                    _results.append({"status": "failed", "keyword": keyword, "error": result.get("error", "WP error")})

                await _job_update(job_id, published=_published, failed=_failed, results=_results)

            except asyncio.TimeoutError:
                _failed += 1
                logger.error(f"[Autopilot] Timeout for '{keyword}' after 6 min")
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE domain_keywords SET status='failed' WHERE id=?", (kw_row["id"],))
                    await db.commit()
                _results.append({"status": "failed", "keyword": keyword, "error": "Timeout (6 min)"})
                await _job_update(job_id, published=_published, failed=_failed, results=_results)
                continue

            except Exception as e:
                _failed += 1
                logger.error(f"Autopilot error for {keyword}: {e}")
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE domain_keywords SET status='failed' WHERE id=?", (kw_row["id"],))
                    await db.commit()
                _results.append({"status": "failed", "keyword": keyword, "error": str(e)})
                await _job_update(job_id, published=_published, failed=_failed, results=_results)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM domain_keywords WHERE schedule_id=? AND status='published'",
                (schedule_id,)
            ) as cur:
                total_pub = (await cur.fetchone())[0]
            await db.execute(
                "UPDATE domain_schedules SET published_count=?, last_run_at=? WHERE id=?",
                (total_pub, datetime.utcnow().isoformat(), schedule_id)
            )
            await db.commit()

        await _job_update(job_id, total_published=total_pub, done=1)

    except Exception as e:
        logger.error(f"[Autopilot] Job {job_id} crashed: {e}")
        await _job_update(job_id, done=1, error=str(e))


@router.post("/schedules/{schedule_id}/run")
async def run_schedule_now(schedule_id: int, body: RunNowRequest, background_tasks: BackgroundTasks):
    """
    Uruchom publikację w tle — zwraca job_id natychmiast.
    Frontned odpytuje GET /schedules/{id}/run-status/{job_id} co 3s.
    """
    job_id = str(_uuid.uuid4())
    await _job_create(job_id, schedule_id)
    background_tasks.add_task(_run_job, job_id, schedule_id, body)
    return {"job_id": job_id, "status": "running"}


@router.get("/schedules/{schedule_id}/run-status/{job_id}")
async def run_status(schedule_id: int, job_id: str):
    """Pobierz status uruchomionego joba."""
    job = await _job_get(job_id)
    if not job:
        from fastapi import HTTPException
        raise HTTPException(404, "Job nie istnieje")
    return job


@router.post("/schedules/{schedule_id}/run-all")
async def run_all_keywords(schedule_id: int, background_tasks: BackgroundTasks):
    """
    Opublikuj WSZYSTKIE pending keywords dla harmonogramu (ignoruje posts_per_day).
    Zwraca job_id — odpytuj /run-status/{job_id} co 3s.
    """
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM domain_keywords WHERE schedule_id=? AND status='pending'",
            (schedule_id,)
        ) as cur:
            pending_count = (await cur.fetchone())[0]

    if pending_count == 0:
        return {"job_id": None, "total_pending": 0, "message": "Brak pending keywords"}

    job_id = str(_uuid.uuid4())
    body = RunNowRequest(schedule_id=schedule_id, limit=pending_count)
    await _job_create(job_id, schedule_id)
    background_tasks.add_task(_run_job, job_id, schedule_id, body)
    return {"job_id": job_id, "total_pending": pending_count, "status": "running"}


@router.post("/schedules/{schedule_id}/retry-failed")
async def retry_failed_keywords(schedule_id: int, background_tasks: BackgroundTasks):
    """
    Resetuj status 'failed' → 'pending' dla wszystkich nieudanych fraz w harmonogramie.
    Następnie uruchom job jak /run (używa posts_per_day jako limit).
    """
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM domain_keywords WHERE schedule_id=? AND status='failed'",
            (schedule_id,)
        ) as cur:
            count = (await cur.fetchone())[0]
        if count == 0:
            return {"message": "Brak failed keywords", "reset": 0}
        await db.execute(
            "UPDATE domain_keywords SET status='pending' WHERE schedule_id=? AND status='failed'",
            (schedule_id,)
        )
        await db.commit()

    job_id = str(_uuid.uuid4())
    body = RunNowRequest(schedule_id=schedule_id, limit=count)
    await _job_create(job_id, schedule_id)
    background_tasks.add_task(_run_job, job_id, schedule_id, body)
    return {"job_id": job_id, "reset": count, "status": "running"}


_SCHEDULE_SEM = asyncio.Semaphore(3)  # max 3 schedules processed concurrently


async def _run_schedule_daily(sched: dict) -> dict:
    """Process a single schedule in the daily autopilot run."""
    async with _SCHEDULE_SEM:
        published = 0
        failed = 0
        schedule_id = sched["id"]
        limit = sched["posts_per_day"]

        # Pre-check WP credentials
        wp_ok = await check_wp_credentials(sched["domain"], sched["wp_login"], sched["wp_pass"],
                                           http_user=sched.get("http_user", ""), http_pass=sched.get("http_pass", ""))
        if not wp_ok:
            logger.warning(f"[Daily] WP credentials invalid for {sched['domain']} — skipping")
            return {"domain": sched["domain"], "schedule_id": schedule_id, "published": 0, "failed": 0, "skipped": True, "reason": "WP credentials invalid"}

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM domain_keywords WHERE schedule_id=? AND status='pending'
                   ORDER BY keyword_type DESC, search_volume DESC LIMIT ?""",
                (schedule_id, limit)
            ) as cur:
                keywords = [dict(r) for r in await cur.fetchall()]
            async with db.execute(
                """SELECT title, keyword, wp_post_url FROM domain_keywords
                   WHERE schedule_id=? AND status='published' AND wp_post_url!=''
                   ORDER BY published_at DESC LIMIT 50""",
                (schedule_id,)
            ) as cur:
                published_posts = [{"title": r["title"] or r["keyword"], "keyword": r["keyword"], "url": r["wp_post_url"]} for r in await cur.fetchall()]

        domain_fingerprints: set = set()

        for kw_row in keywords:
            keyword = kw_row["keyword"]

            # Skip if already published on this domain (race condition guard)
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT COUNT(*) FROM domain_keywords WHERE my_domain_id=? AND keyword=? AND status='published'",
                    (sched["my_domain_id"], keyword)
                ) as cur:
                    already = (await cur.fetchone())[0]
            if already:
                logger.info(f"[Daily] Skipping '{keyword}' — already published on this domain")
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE domain_keywords SET status='published' WHERE id=?", (kw_row["id"],))
                    await db.commit()
                continue

            variation = random.choice(VARIATION_HINTS)
            # Fetch pillar page URL for internal linking (supporting → pillar)
            pillar_url, pillar_keyword = "", ""
            if kw_row.get("keyword_type") == "supporting" and kw_row.get("pillar_anchor"):
                pillar_url, pillar_keyword = await _get_pillar_url(
                    schedule_id, sched["my_domain_id"], kw_row["pillar_anchor"]
                )
            async with _DAILY_SEM:
                try:
                    location_code = 2616 if sched["language"] == "pl" else 2840
                    _pillar_url = pillar_url
                    _pillar_anchor = pillar_keyword or kw_row.get("pillar_label", "")
                    async with asyncio.timeout(360):
                        article = await _with_retry(lambda: generate_article(
                            topic=keyword,
                            client_domain=sched["client_domain"] or "",
                            anchor_text=sched["anchor_text"] or keyword,
                            language=sched["language"],
                            variation_hint=variation,
                            custom_prompt=sched.get("custom_prompt", "") or "",
                            dfs_login=DFS_LOGIN,
                            dfs_password=DFS_PASSWORD,
                            location_code=location_code,
                            published_posts=published_posts,
                            domain_fingerprints=domain_fingerprints,
                            pillar_page_url=_pillar_url,
                            pillar_page_anchor=_pillar_anchor,
                        ))
                        excerpt = article.get("excerpt", "")
                        lsi_tags = article.get("lsi_tags", [])

                        img_prompt_daily = (
                            f"High-quality professional photo for blog article: '{article['title']}'. "
                            f"Topic: {keyword}. Realistic scene, natural lighting, no text, no watermarks, clean modern aesthetic."
                        )
                        image_b64, _ = await _fetch_image(
                            sched.get("image_source", "freepik_stock"), keyword, article["title"], img_prompt_daily
                        )

                        _art = article  # capture for lambda
                        async def _do_publish_daily():
                            r = await publish_post(
                                domain=sched["domain"],
                                wp_login=sched["wp_login"],
                                wp_pass=sched["wp_pass"],
                                title=_art["title"],
                                content=_art["content"],
                                image_b64=image_b64,
                                category_id=kw_row.get("wp_category_id") or None,
                                excerpt=excerpt,
                                keyword=keyword,
                                tags=lsi_tags,
                                http_user=sched.get("http_user", ""),
                                http_pass=sched.get("http_pass", ""),
                            )
                            if not r.get("success"):
                                raise RuntimeError(r.get("error", "WP publish failed"))
                            return r
                        result = await _with_retry(_do_publish_daily, max_attempts=2, base_delay=5.0)

                    if result.get("success"):
                        wp_url = result.get("url", "")
                        published += 1
                        published_posts.append({"title": article["title"], "keyword": keyword, "url": wp_url})
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute(
                                "INSERT INTO posts (client_id, client_domain, my_domain_id, title, content, wp_post_url, status) VALUES (?,?,?,?,?,?,?)",
                                (None, sched["client_domain"] or "",
                                 sched["my_domain_id"], article["title"], article["content"], wp_url, "published")
                            )
                            await db.execute(
                                "UPDATE domain_keywords SET status='published', title=?, wp_post_url=?, published_at=? WHERE id=?",
                                (article["title"], wp_url, datetime.utcnow().isoformat(), kw_row["id"])
                            )
                            await db.commit()
                    else:
                        failed += 1
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute("UPDATE domain_keywords SET status='failed' WHERE id=?", (kw_row["id"],))
                            await db.commit()
                except asyncio.TimeoutError:
                    failed += 1
                    logger.error(f"[Daily] Timeout for '{keyword}' after 6 min on {sched['domain']}")
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("UPDATE domain_keywords SET status='failed' WHERE id=?", (kw_row["id"],))
                        await db.commit()
                except Exception as e:
                    failed += 1
                    logger.error(f"Daily run error domain={sched['domain']} kw={keyword}: {e}")
            await asyncio.sleep(2)  # avoid bursting OpenAI rate limits between keywords

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM domain_keywords WHERE schedule_id=? AND status='published'", (schedule_id,)
            ) as cur:
                total_pub = (await cur.fetchone())[0]
            await db.execute(
                "UPDATE domain_schedules SET published_count=?, last_run_at=? WHERE id=?",
                (total_pub, datetime.utcnow().isoformat(), schedule_id)
            )
            await db.commit()

        return {
            "domain": sched["domain"],
            "schedule_id": schedule_id,
            "published": published,
            "failed": failed,
        }


@router.post("/run-daily")
async def run_daily_all():
    """
    Uruchom dzienny autopilot dla WSZYSTKICH aktywnych harmonogramów.
    Każdy harmonogram dostaje posts_per_day artykułów.
    Harmonogramy przetwarzane równolegle (max 3 naraz).
    Zwraca podsumowanie (nie streamuje).
    """
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT s.*, md.domain, md.wp_login, md.wp_pass,
                      COALESCE(md.http_user,'') as http_user,
                      COALESCE(md.http_pass,'') as http_pass
               FROM domain_schedules s
               JOIN my_domains md ON md.id = s.my_domain_id
               WHERE s.active = 1 AND md.active = 1 AND s.map_generated = 1"""
        ) as cur:
            schedules = [dict(r) for r in await cur.fetchall()]

    results = await asyncio.gather(
        *[_run_schedule_daily(sched) for sched in schedules],
        return_exceptions=True,
    )
    # Unwrap any exceptions into error entries
    final = []
    for sched, res in zip(schedules, results):
        if isinstance(res, Exception):
            logger.error(f"[Daily] Schedule {sched['id']} ({sched['domain']}) crashed: {res}")
            final.append({"domain": sched["domain"], "schedule_id": sched["id"], "published": 0, "failed": 0, "error": str(res)})
        else:
            final.append(res)

    return {"schedules_processed": len(final), "results": final}


@router.get("/stats")
async def autopilot_stats():
    """Globalne statystyki autopilota — używane przez Dashboard widget."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM domain_schedules") as cur:
            total_schedules = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM domain_schedules WHERE active=1") as cur:
            active_schedules = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM domain_keywords") as cur:
            total_keywords = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM domain_keywords WHERE status='pending'") as cur:
            pending = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM domain_keywords WHERE status='published'") as cur:
            published = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM domain_keywords WHERE status='failed'") as cur:
            failed = (await cur.fetchone())[0]
        # Last 3 completed jobs
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM run_jobs WHERE done=1 ORDER BY created_at DESC LIMIT 3"
        ) as cur:
            recent_jobs = [dict(r) for r in await cur.fetchall()]
        # Next cron run: daily 08:00 UTC
        now = datetime.utcnow()
        next_cron = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if next_cron <= now:
            from datetime import timedelta
            next_cron += timedelta(days=1)

    for job in recent_jobs:
        job['results'] = _json.loads(job.pop('results_json', '[]'))

    return {
        "total_schedules": total_schedules,
        "active_schedules": active_schedules,
        "total_keywords": total_keywords,
        "pending_keywords": pending,
        "published_keywords": published,
        "failed_keywords": failed,
        "next_cron_utc": next_cron.strftime("%Y-%m-%dT%H:%M:00Z"),
        "recent_jobs": recent_jobs,
    }


@router.get("/jobs")
async def get_jobs():
    """Return recent run_jobs — both running and completed (last 20)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM run_jobs ORDER BY created_at DESC LIMIT 20"
        ) as cur:
            jobs = [dict(r) for r in await cur.fetchall()]
    for job in jobs:
        try:
            job['results'] = _json.loads(job.pop('results_json', '[]'))
        except Exception:
            job['results'] = []
    return jobs


class BulkScheduleItem(BaseModel):
    my_domain_id: int
    seed_keyword: str  # can be overridden per domain or shared


class BulkCreateRequest(BaseModel):
    domain_ids: List[int]
    seed_keyword: str
    posts_per_day: int = 1
    language: str = "pl"
    min_volume: int = 10
    client_domain: str = ""
    anchor_text: str = ""
    custom_prompt: str = ""


class BulkActionRequest(BaseModel):
    schedule_ids: List[int]
    limit: Optional[int] = None  # for bulk run — overrides posts_per_day


@router.post("/bulk-create")
async def bulk_create_schedules(body: BulkCreateRequest):
    """
    Utwórz harmonogramy hurtowo dla wielu domen naraz.
    Jedna fraza seed dla wszystkich (lub różne — do edycji po fakcie).
    Pomija domeny które już mają harmonogram z tą frazą.
    """
    await ensure_tables()
    created = []
    skipped = []
    errors = []

    async with aiosqlite.connect(DB_PATH) as db:
        for domain_id in body.domain_ids:
            try:
                async with db.execute("SELECT id, domain FROM my_domains WHERE id = ?", (domain_id,)) as cur:
                    dom = await cur.fetchone()
                if not dom:
                    errors.append({"domain_id": domain_id, "error": "not found"})
                    continue

                async with db.execute(
                    "SELECT id FROM domain_schedules WHERE my_domain_id = ? AND seed_keyword = ?",
                    (domain_id, body.seed_keyword)
                ) as cur:
                    existing = await cur.fetchone()

                if existing:
                    skipped.append({"domain_id": domain_id, "domain": dom[1], "schedule_id": existing[0]})
                    continue

                cursor = await db.execute(
                    """INSERT INTO domain_schedules
                       (my_domain_id, seed_keyword, posts_per_day, language, min_volume, client_domain, anchor_text, custom_prompt)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (domain_id, body.seed_keyword, body.posts_per_day,
                     body.language, body.min_volume, body.client_domain, body.anchor_text,
                     body.custom_prompt)
                )
                created.append({"domain_id": domain_id, "domain": dom[1], "schedule_id": cursor.lastrowid})
            except Exception as e:
                errors.append({"domain_id": domain_id, "error": str(e)})

        await db.commit()

    return {"created": len(created), "skipped": len(skipped), "errors": len(errors),
            "details": {"created": created, "skipped": skipped, "errors": errors}}


@router.post("/bulk-generate-maps")
async def bulk_generate_maps(body: BulkActionRequest):
    """
    Generuj Topical Map hurtowo dla wielu harmonogramów.
    Uruchamia sekwencyjnie (nie parallel) żeby nie przeciążyć DataForSEO.
    """
    await ensure_tables()
    if not DFS_LOGIN or not DFS_PASSWORD:
        from fastapi import HTTPException
        raise HTTPException(400, "Brak konfiguracji DataForSEO")

    results = []
    _dfs_sem = asyncio.Semaphore(3)  # max 3 concurrent DFS requests

    for schedule_id in body.schedule_ids:
        async with aiosqlite.connect(DB_PATH) as db:
            sched = await get_schedule(db, schedule_id)
        if not sched:
            results.append({"schedule_id": schedule_id, "error": "not found"})
            continue
        try:
            async with _dfs_sem:
                tmap = await generate_topical_map(
                    seed=sched["seed_keyword"],
                    location_code=2616 if sched["language"] == "pl" else 2840,
                    language_code=sched["language"],
                    min_volume=sched["min_volume"],
                    max_clusters=8,
                    dfs_login=DFS_LOGIN,
                    dfs_password=DFS_PASSWORD,
                )
            await asyncio.sleep(0.5)  # small delay between DFS requests
            inserted = 0
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT keyword FROM domain_keywords WHERE schedule_id = ?", (schedule_id,)
                ) as cur:
                    existing_kws = {row[0] for row in await cur.fetchall()}

                for pillar in tmap.get("pillars", []):
                    anchor = pillar["anchor"]
                    label = pillar["label"]
                    await db.execute(
                        """INSERT OR IGNORE INTO domain_categories
                           (schedule_id, my_domain_id, pillar_anchor, pillar_label)
                           VALUES (?,?,?,?)""",
                        (schedule_id, sched["my_domain_id"], anchor, label)
                    )
                    pk = pillar["pillar_keyword"]
                    if pk and pk not in existing_kws:
                        await db.execute(
                            """INSERT INTO domain_keywords
                               (schedule_id, my_domain_id, keyword, keyword_type, pillar_label, pillar_anchor, search_volume, keyword_difficulty)
                               VALUES (?,?,?,?,?,?,?,?)""",
                            (schedule_id, sched["my_domain_id"], pk, "pillar",
                             label, anchor, pillar.get("pillar_volume", 0), pillar.get("pillar_difficulty", 0))
                        )
                        existing_kws.add(pk)
                        inserted += 1
                    for sk in pillar.get("supporting_keywords", []):
                        kw = sk["keyword"]
                        if kw and kw not in existing_kws:
                            await db.execute(
                                """INSERT INTO domain_keywords
                                   (schedule_id, my_domain_id, keyword, keyword_type, pillar_label, pillar_anchor, search_volume, keyword_difficulty)
                                   VALUES (?,?,?,?,?,?,?,?)""",
                                (schedule_id, sched["my_domain_id"], kw, "supporting",
                                 label, anchor, sk.get("search_volume", 0), sk.get("keyword_difficulty", 0))
                            )
                            existing_kws.add(kw)
                            inserted += 1

                async with db.execute(
                    "SELECT COUNT(*) FROM domain_keywords WHERE schedule_id = ?", (schedule_id,)
                ) as cur:
                    total = (await cur.fetchone())[0]
                await db.execute(
                    "UPDATE domain_schedules SET map_generated=1, total_keywords=? WHERE id=?",
                    (total, schedule_id)
                )
                await db.commit()

            results.append({
                "schedule_id": schedule_id,
                "domain": sched["domain"],
                "inserted": inserted,
                "total_keywords": total,
                "pillars": len(tmap.get("pillars", [])),
            })
            logger.info(f"[BulkMap] {sched['domain']}: {inserted} kw inserted")
        except Exception as e:
            logger.error(f"[BulkMap] schedule {schedule_id} error: {e}")
            results.append({"schedule_id": schedule_id, "domain": sched.get("domain", "?"), "error": str(e)})

    ok = sum(1 for r in results if "error" not in r)
    return {"processed": len(results), "ok": ok, "failed": len(results) - ok, "results": results}


@router.post("/bulk-run")
async def bulk_run_schedules(body: BulkActionRequest):
    """
    Uruchom publikację hurtowo dla wielu harmonogramów.
    Każdy harmonogram dostaje body.limit (lub posts_per_day) artykułów.
    Równoległe wykonanie z semaforem (max 3 jednocześnie).
    """
    await ensure_tables()

    sem = asyncio.Semaphore(3)

    async def _run_one(schedule_id: int) -> dict:
        async with sem:
            async with aiosqlite.connect(DB_PATH) as db:
                sched = await get_schedule(db, schedule_id)
            if not sched:
                return {"schedule_id": schedule_id, "error": "not found"}

            limit = body.limit if body.limit else sched["posts_per_day"]

            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    """SELECT * FROM domain_keywords WHERE schedule_id = ? AND status = 'pending'
                       ORDER BY keyword_type DESC, search_volume DESC LIMIT ?""",
                    (schedule_id, limit)
                ) as cur:
                    keywords = [dict(r) for r in await cur.fetchall()]
                async with db.execute(
                    """SELECT title, keyword, wp_post_url FROM domain_keywords
                       WHERE schedule_id=? AND status='published' AND wp_post_url!=''
                       ORDER BY published_at DESC LIMIT 50""",
                    (schedule_id,)
                ) as cur:
                    published_posts = [{"title": r["title"] or r["keyword"], "keyword": r["keyword"], "url": r["wp_post_url"]} for r in await cur.fetchall()]

            if not keywords:
                return {"schedule_id": schedule_id, "domain": sched["domain"], "published": 0, "failed": 0, "skipped": True, "reason": "no pending keywords"}

            wp_ok = await check_wp_credentials(sched["domain"], sched["wp_login"], sched["wp_pass"],
                                               http_user=sched.get("http_user", ""), http_pass=sched.get("http_pass", ""))
            if not wp_ok:
                return {"schedule_id": schedule_id, "domain": sched["domain"], "published": 0, "failed": 0, "skipped": True, "reason": "WP credentials invalid"}

            published = 0
            failed = 0
            domain_fingerprints: set = set()

            for kw_row in keywords:
                keyword = kw_row["keyword"]
                variation = random.choice(VARIATION_HINTS)
                try:
                    location_code = 2616 if sched["language"] == "pl" else 2840
                    article = await generate_article(
                        topic=keyword,
                        client_domain=sched["client_domain"] or "",
                        anchor_text=sched["anchor_text"] or keyword,
                        language=sched["language"],
                        variation_hint=variation,
                        custom_prompt=sched.get("custom_prompt", "") or "",
                        dfs_login=DFS_LOGIN,
                        dfs_password=DFS_PASSWORD,
                        location_code=location_code,
                        published_posts=published_posts,
                        domain_fingerprints=domain_fingerprints,
                    )
                    img_prompt_bulk = (
                        f"High-quality professional photo for blog article: '{article['title']}'. "
                        f"Topic: {keyword}. Realistic scene, natural lighting, no text, no watermarks."
                    )
                    image_b64, _ = await _fetch_image(
                        sched.get("image_source", "freepik_stock"), keyword, article["title"], img_prompt_bulk
                    )

                    _art = article
                    _img = image_b64
                    async def _do_publish_bulk(_a=_art, _i=_img, _k=kw_row, _s=sched, _kw=keyword):
                        r = await publish_post(
                            domain=_s["domain"],
                            wp_login=_s["wp_login"],
                            wp_pass=_s["wp_pass"],
                            title=_a["title"],
                            content=_a["content"],
                            image_b64=_i,
                            category_id=_k.get("wp_category_id") or None,
                            excerpt=_a.get("excerpt", ""),
                            keyword=_kw,
                            http_user=_s.get("http_user", ""),
                            http_pass=_s.get("http_pass", ""),
                        )
                        if not r.get("success"):
                            raise RuntimeError(r.get("error", "WP publish failed"))
                        return r
                    result = await _with_retry(_do_publish_bulk, max_attempts=2, base_delay=5.0)
                    if result.get("success"):
                        wp_url = result.get("url", "")
                        published += 1
                        published_posts.append({"title": article["title"], "keyword": keyword, "url": wp_url})
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute(
                                """INSERT INTO posts (client_id, client_domain, my_domain_id, title, content,
                                   wp_post_url, status) VALUES (?,?,?,?,?,?,?)""",
                                (None, sched["client_domain"] or "",
                                 sched["my_domain_id"], article["title"], article["content"], wp_url, "published")
                            )
                            await db.execute(
                                "UPDATE domain_keywords SET status='published', title=?, wp_post_url=?, published_at=? WHERE id=?",
                                (article["title"], wp_url, datetime.utcnow().isoformat(), kw_row["id"])
                            )
                            await db.commit()
                    else:
                        failed += 1
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute("UPDATE domain_keywords SET status='failed' WHERE id=?", (kw_row["id"],))
                            await db.commit()
                except Exception as e:
                    failed += 1
                    logger.error(f"[BulkRun] {sched['domain']} kw={keyword}: {e}")
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("UPDATE domain_keywords SET status='failed' WHERE id=?", (kw_row["id"],))
                        await db.commit()

            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT COUNT(*) FROM domain_keywords WHERE schedule_id=? AND status='published'", (schedule_id,)
                ) as cur:
                    total_pub = (await cur.fetchone())[0]
                await db.execute(
                    "UPDATE domain_schedules SET published_count=?, last_run_at=? WHERE id=?",
                    (total_pub, datetime.utcnow().isoformat(), schedule_id)
                )
                await db.commit()

            return {"schedule_id": schedule_id, "domain": sched["domain"], "published": published, "failed": failed}

    all_results = await asyncio.gather(*[_run_one(sid) for sid in body.schedule_ids])
    total_pub = sum(r.get("published", 0) for r in all_results)
    total_fail = sum(r.get("failed", 0) for r in all_results)
    return {"processed": len(all_results), "total_published": total_pub, "total_failed": total_fail, "results": list(all_results)}


@router.post("/bulk-set-ppd")
async def bulk_set_posts_per_day(body: BulkActionRequest):
    """Ustaw posts_per_day hurtowo dla wielu harmonogramów."""
    if body.limit is None:
        from fastapi import HTTPException
        raise HTTPException(400, "Podaj limit (= posts_per_day)")
    async with aiosqlite.connect(DB_PATH) as db:
        for schedule_id in body.schedule_ids:
            await db.execute(
                "UPDATE domain_schedules SET posts_per_day=? WHERE id=?",
                (body.limit, schedule_id)
            )
        await db.commit()
    return {"updated": len(body.schedule_ids), "posts_per_day": body.limit}


@router.post("/keywords/{keyword_id}/retry")
async def retry_keyword(keyword_id: int):
    """Reset a failed keyword back to pending."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, status FROM domain_keywords WHERE id = ?", (keyword_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            from fastapi import HTTPException
            raise HTTPException(404, "Keyword not found")
        await db.execute(
            "UPDATE domain_keywords SET status='pending', wp_post_url=NULL WHERE id = ?",
            (keyword_id,)
        )
        await db.commit()
    return {"updated": keyword_id, "status": "pending"}


@router.get("/schedules/{schedule_id}/export-csv")
async def export_keywords_csv(schedule_id: int):
    """Export keyword map as CSV — keyword, type, pillar, volume, difficulty, status, published_at, url."""
    from fastapi.responses import StreamingResponse
    import csv
    import io

    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM domain_schedules WHERE id=?", (schedule_id,)
        ) as cur:
            sched_row = await cur.fetchone()
        if not sched_row:
            from fastapi import HTTPException
            raise HTTPException(404, "Harmonogram nie istnieje")
        async with db.execute(
            """SELECT keyword, keyword_type, pillar_label, search_volume, keyword_difficulty,
                      status, published_at, wp_post_url, title
               FROM domain_keywords WHERE schedule_id=?
               ORDER BY keyword_type DESC, search_volume DESC""",
            (schedule_id,)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "keyword", "keyword_type", "pillar_label", "search_volume",
        "keyword_difficulty", "status", "published_at", "wp_post_url", "title"
    ])
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)

    seed = dict(sched_row).get("seed_keyword", str(schedule_id)).replace(" ", "_")[:30]
    filename = f"keywords_{seed}_{schedule_id}.csv"

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.get("/test-env")
async def test_env():
    """Debug: show which API keys are set (masked)."""
    import os
    keys = ["GEMINI_API_KEY", "OPENAI_API_KEY", "FREEPIK_API_KEY", "DATAFORSEO_LOGIN"]
    return {
        k: (os.getenv(k, "")[:6] + "..." if os.getenv(k) else "NOT SET")
        for k in keys
    }


@router.get("/test-images")
async def test_images():
    """Test all image providers. Returns status and image size for each."""
    import time
    keyword = "SEO optimization"
    title = "SEO Optimization Guide 2025"
    prompt = f"High-quality professional photo for blog article: '{title}'. Topic: {keyword}. Realistic scene, natural lighting, no text, no watermarks."
    results = {}

    for source in ["freepik_stock", "freepik_zimage", "freepik_flux", "dalle"]:
        t0 = time.time()
        try:
            img, provider = await _fetch_image(source, keyword, title, prompt)
            elapsed = round(time.time() - t0, 2)
            results[source] = {
                "ok": img is not None,
                "provider_used": provider,
                "size_kb": round(len(img) / 1024) if img else 0,
                "elapsed_s": elapsed,
            }
        except Exception as e:
            results[source] = {"ok": False, "error": str(e)[:200], "elapsed_s": round(time.time() - t0, 2)}
        # Attach provider errors if any
        results[source]["provider_errors"] = getattr(_fetch_image, "_last_errors", {})

    return results

@router.get("/test-freepik-raw")
async def test_freepik_raw():
    """Debug: check raw Freepik z-image POST response and poll URLs."""
    import os
    import asyncio
    import httpx
    api_key = os.getenv("FREEPIK_API_KEY", "")
    headers = {"x-freepik-api-key": api_key, "Content-Type": "application/json", "Accept": "application/json"}
    prompt = "professional blog photo SEO optimization"
    results = {}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.freepik.com/v1/ai/text-to-image/z-image",
            headers=headers,
            json={"prompt": prompt, "image_size": "landscape_4_3", "output_format": "jpeg", "num_inference_steps": 8}
        )
        results["post_status"] = resp.status_code
        results["post_body"] = resp.json() if resp.status_code < 300 else resp.text[:500]

        data = resp.json() if resp.status_code < 300 else {}
        task_id = data.get("task_id") or data.get("data", {}).get("task_id")
        results["task_id"] = task_id

        if task_id:
            await asyncio.sleep(10)
            for url in [
                f"https://api.freepik.com/v1/ai/text-to-image/z-image/{task_id}",
                f"https://api.freepik.com/v1/ai/text-to-image/z-image?task_id={task_id}",
            ]:
                r = await client.get(url, headers=headers)
                key = "get_by_path" if task_id in url and "?" not in url else "get_by_param"
                results[key] = {"status": r.status_code, "body": r.text[:800]}

    return results

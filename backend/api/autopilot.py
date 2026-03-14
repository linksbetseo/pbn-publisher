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
import re
import time
import uuid as _uuid
from datetime import date, datetime, timezone
from typing import List, Optional

import aiosqlite
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from config import DB_PATH
from services.topical_map_service import generate_topical_map, _compute_site_metrics, _coherence_score, _seed_tokens
from services.dataforseo_service import DataForSEOClient
from services.openai_service import generate_article
from services.wordpress_service import publish_post, get_or_create_category, get_categories, check_wp_credentials
from api.autopilot_helpers import (
    VARIATION_HINTS,
    job_create as _job_create,
    job_update as _job_update,
    job_get as _job_get,
    with_retry as _with_retry,
    get_pillar_url as _get_pillar_url,
    fetch_image as _fetch_image,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/autopilot", tags=["autopilot"])

# Limit max 3 concurrent article generations during the daily run
_DAILY_SEM = asyncio.Semaphore(3)

DFS_LOGIN = os.getenv("DATAFORSEO_LOGIN", "")
DFS_PASSWORD = os.getenv("DATAFORSEO_PASSWORD", "")

# ── Models ──────────────────────────────────────────────────────────────────

class ScheduleCreate(BaseModel):
    my_domain_id: int
    seed_keyword: str
    posts_per_day: int = 1
    language: str = "pl"
    min_volume: int = 10
    min_coherence: float = 0.0  # SiteRadius filter: 0=off, 0.1=light, 0.3=moderate, 0.5=strict
    client_domain: str = ""
    anchor_text: str = ""
    image_source: str = "freepik_flux"
    custom_prompt: str = ""
    tone_of_voice: str = "ekspert"


class ScheduleUpdate(BaseModel):
    posts_per_day: Optional[int] = None
    active: Optional[int] = None
    client_domain: Optional[str] = None
    anchor_text: Optional[str] = None
    image_source: Optional[str] = None
    custom_prompt: Optional[str] = None
    min_volume: Optional[int] = None
    min_coherence: Optional[float] = None
    language: Optional[str] = None
    tone_of_voice: Optional[str] = None


class RunNowRequest(BaseModel):
    schedule_id: int
    limit: Optional[int] = None  # nadpisuje posts_per_day


# In-flight map generation guard — prevents double-click race condition
# Uses dict {schedule_id: timestamp} for auto-cleanup of stale entries (10min timeout)
_map_generating: dict = {}
_MAP_GEN_TIMEOUT = 600  # 10 minutes max

# ── Tone of voice ────────────────────────────────────────────────────────────
_TONE_PRESETS = {
    "ekspert": "Pisz w tonie eksperckim, autorytatywnym.",
    "przyjazny": "Pisz w tonie przyjaznym, doradczym.",
    "formalny": "Pisz w tonie formalnym, biznesowym.",
    "poradnikowy": "Pisz w tonie poradnikowym, krok po kroku.",
}

def _effective_prompt(sched: dict) -> str:
    """Build custom_prompt with tone_of_voice prefix.

    If tone_of_voice is a short preset key (ekspert/przyjazny/…) → use preset sentence.
    If it's a long AI-generated tone instruction → inject verbatim.
    """
    base = (sched.get("custom_prompt") or "").strip()
    tone = (sched.get("tone_of_voice") or "").strip()
    if not tone:
        return base
    # Short preset key?
    if tone in _TONE_PRESETS:
        return f"{_TONE_PRESETS[tone]} {base}".strip()
    # Long AI-generated tone instruction — inject as-is
    return f"{tone}\n\n{base}".strip()


async def _gpt_call(system: str, user: str, temperature: float = 0.5,
                     max_tokens: int = 1500) -> str:
    """Simple GPT helper with retry for autopilot tone generation."""
    from openai import AsyncOpenAI as _AO
    from services.openai_service import get_gpt_model
    client = _AO()
    model = await get_gpt_model()
    for attempt in range(3):
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            if attempt == 2:
                raise
            wait = 2 ** attempt + random.uniform(0, 1.5)
            logger.warning(f"[AutopilotGPT] attempt {attempt+1} failed: {e} — retrying in {wait:.1f}s")
            await asyncio.sleep(wait)
    return ""


async def _generate_tone_for_schedule(schedule_id: int) -> dict:
    """Full AI-generated Tone of Voice pipeline for an autopilot schedule.

    Steps:
    1. Parse domain content (DataForSEO)
    2. Extract main keyword (GPT)
    3. Customer profile (GPT)
    4. SERP analysis (DataForSEO)
    5. Select top 5 blog competitors (GPT)
    6. Parse competitor content (DataForSEO)
    7. Mix tone of voice candidates (GPT)
    8. Generate final Tone of Voice (GPT)
    """
    import base64 as _b64
    import httpx

    await ensure_tables()

    async with aiosqlite.connect(DB_PATH) as db:
        sched = await get_schedule(db, schedule_id)
    if not sched:
        return {"error": "Schedule not found"}

    domain_url = sched["domain"]
    seed = sched.get("seed_keyword", "")
    lang = sched.get("language", "pl")
    lang_pl = lang == "pl"

    dfs_ok = bool(DFS_LOGIN and DFS_PASSWORD)
    dfs_headers = {}
    if dfs_ok:
        creds = _b64.b64encode(f"{DFS_LOGIN}:{DFS_PASSWORD}".encode()).decode()
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
        content_text = f"Domena: {domain_url}, nisza/seed: {seed}"
        steps_log.append("Using fallback content (domain + seed)")

    # ── STEP 2: Extract main keyword ──
    kw_system = (
        "Analizuj podany content ze strony internetowej i zidentyfikuj główne słowo kluczowe. "
        "Podaj TYLKO jedno słowo kluczowe, bez cudzysłowów, bez wyjaśnień."
    ) if lang_pl else (
        "Analyze the website content and identify the main keyword. "
        "Return ONLY one keyword, no quotes, no explanation."
    )
    main_keyword = await _gpt_call(kw_system, f"Content:\n{content_text[:3000]}", temperature=0.4, max_tokens=50)
    main_keyword = main_keyword.strip().strip('"').strip("'")
    if not main_keyword:
        main_keyword = seed
    steps_log.append(f"Main keyword: {main_keyword}")

    # ── STEP 3: Customer profile ──
    profile_prompt = (
        f"Stwórz profil klienta dla strony {domain_url} (seed: {seed}). Uwzględnij:\n"
        f"1. Przegląd typowego klienta\n2. Demografię\n3. Wartości i postawy\n"
        f"4. Problemy i bolączki\n5. Kluczowe motywacje\n6. Proces decyzyjny\n"
        f"7. Czego szukają na tej stronie\nBądź konkretny i szczegółowy."
    ) if lang_pl else (
        f"Create a customer profile for {domain_url} (seed: {seed}). Include:\n"
        f"1. Typical client overview\n2. Demographics\n3. Values and attitudes\n"
        f"4. Problems and pain points\n5. Key motivations\n6. Decision process\n"
        f"7. What they're looking for\nBe specific and detailed."
    )
    customer_profile = await _gpt_call(
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
                selected = await _gpt_call(
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
    mixed_tones = await _gpt_call(mix_system, "Mix these tones.", temperature=0.6, max_tokens=800)
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
        f"<about_website>\nDomena: {domain_url}, Seed keyword: {seed}\n</about_website>"
    )

    tone_of_voice = await _gpt_call(tone_system, tone_user, temperature=0.6, max_tokens=2000)
    steps_log.append(f"Tone of Voice generated: {len(tone_of_voice)} chars")

    # ── Save to DB ──
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE domain_schedules SET tone_of_voice=? WHERE id=?",
            (tone_of_voice, schedule_id),
        )
        await db.commit()

    logger.info(f"[ToneOfVoice] Generated for schedule {schedule_id} ({domain_url}): keyword={main_keyword}")

    return {
        "schedule_id": schedule_id,
        "domain": domain_url,
        "main_keyword": main_keyword,
        "tone_of_voice": tone_of_voice,
        "steps": steps_log,
    }

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
    image_source TEXT DEFAULT 'freepik_flux',
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
            ("coherence", "REAL DEFAULT 0.0"),
        ]:
            try:
                await db.execute(f"ALTER TABLE domain_keywords ADD COLUMN {col} {typedef}")
            except Exception:
                pass
        # Migracja domain_schedules
        for col, typedef in [
            ("image_source", "TEXT DEFAULT 'freepik_flux'"),
            ("custom_prompt", "TEXT DEFAULT ''"),
            ("min_coherence", "REAL DEFAULT 0.0"),
            ("tone_of_voice", "TEXT DEFAULT 'ekspert'"),
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
               (my_domain_id, seed_keyword, posts_per_day, language, min_volume, min_coherence, client_domain, anchor_text, image_source, custom_prompt, tone_of_voice)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (body.my_domain_id, body.seed_keyword, body.posts_per_day,
             body.language, body.min_volume, body.min_coherence, body.client_domain, body.anchor_text,
             body.image_source, body.custom_prompt, body.tone_of_voice)
        )
        schedule_id = cursor.lastrowid
        await db.commit()
    return {"id": schedule_id, "message": "Harmonogram utworzony. Wygeneruj Topical Map."}


@router.patch("/schedules/{schedule_id}")
async def update_schedule(schedule_id: int, body: ScheduleUpdate):
    """Aktualizuj ustawienia harmonogramu."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        # FIX #64: consolidate into single UPDATE instead of N separate queries per field
        updates = {}
        for field in ("posts_per_day", "active", "client_domain", "anchor_text",
                      "image_source", "custom_prompt", "min_volume", "min_coherence", "language", "tone_of_voice"):
            val = getattr(body, field, None)
            if val is not None:
                updates[field] = val
        if updates:
            sets = ", ".join(f"{k}=?" for k in updates)
            vals = list(updates.values()) + [schedule_id]
            await db.execute(f"UPDATE domain_schedules SET {sets} WHERE id=?", vals)
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

    # FIX #65: use module-level time import instead of inline import
    now = time.time()
    stale = [k for k, ts in _map_generating.items() if now - ts > _MAP_GEN_TIMEOUT]
    for k in stale:
        _map_generating.pop(k, None)

    # Idempotency guard — prevent concurrent double-generation for same schedule
    if schedule_id in _map_generating:
        from fastapi import HTTPException
        raise HTTPException(409, "Generowanie mapy już w toku dla tego harmonogramu")
    _map_generating[schedule_id] = now
    try:
        return await _do_generate_map(schedule_id, force_refresh=force_refresh)
    finally:
        _map_generating.pop(schedule_id, None)


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
        min_coherence=float(sched.get("min_coherence") or 0.0),
    )

    # FIX #67: use module-level re import instead of inline import re as _re_cann

    def _kw_stem(kw: str) -> str:
        tokens = re.sub(r"[^a-z0-9ąćęłńóśźż ]+", "", kw.lower()).split()
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
                       (schedule_id, my_domain_id, keyword, keyword_type, pillar_label, pillar_anchor, search_volume, keyword_difficulty, coherence, status)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (schedule_id, sched["my_domain_id"], pk, "pillar",
                     label, anchor, pillar.get("pillar_volume", 0), pillar.get("pillar_difficulty", 0),
                     pillar.get("focus_score", 0), init_status)
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
                           (schedule_id, my_domain_id, keyword, keyword_type, pillar_label, pillar_anchor, search_volume, keyword_difficulty, coherence, status)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (schedule_id, sched["my_domain_id"], kw, "supporting",
                         label, anchor, sk.get("search_volume", 0), sk.get("keyword_difficulty", 0),
                         sk.get("coherence", 0), init_status)
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


@router.get("/schedules/{schedule_id}/site-metrics")
async def get_site_metrics(schedule_id: int):
    """Oblicz SiteFocus / SiteRadius z istniejących keywords (bez regeneracji mapy)."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        sched = await get_schedule(db, schedule_id)
        if not sched:
            from fastapi import HTTPException
            raise HTTPException(404, "Schedule not found")

        # Fetch all keywords for this schedule
        async with db.execute(
            """SELECT keyword, keyword_type, pillar_label, search_volume, keyword_difficulty,
                      coherence, status
               FROM domain_keywords WHERE schedule_id = ?""",
            (schedule_id,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    if not rows:
        return {"site_metrics": {}, "message": "Brak fraz — wygeneruj mapę najpierw"}

    # Build pillar structure from DB keywords
    pillars_map: dict[str, dict] = {}
    for kw in rows:
        label = kw.get("pillar_label") or "default"
        if label not in pillars_map:
            pillars_map[label] = {
                "label": label,
                "total_volume": 0,
                "focus_score": 0.0,
                "supporting_keywords": [],
            }
        vol = kw.get("search_volume") or 0
        coh = kw.get("coherence") or 0.0
        pillars_map[label]["total_volume"] += vol
        if kw["keyword_type"] == "pillar":
            pillars_map[label]["focus_score"] = coh
        else:
            pillars_map[label]["supporting_keywords"].append(kw)

    # Compute focus_score as avg coherence if pillar didn't have it
    for p in pillars_map.values():
        if p["focus_score"] == 0 and p["supporting_keywords"]:
            scores = [s.get("coherence") or 0 for s in p["supporting_keywords"]]
            p["focus_score"] = sum(scores) / len(scores) if scores else 0

    pillars = list(pillars_map.values())
    metrics = _compute_site_metrics(pillars, sched["seed_keyword"], rows)
    return {"site_metrics": metrics}


@router.post("/schedules/{schedule_id}/recalculate-coherence")
async def recalculate_coherence(schedule_id: int):
    """Przelicz coherence dla istniejących keywordów (po poprawce stemmera)."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        sched = await get_schedule(db, schedule_id)
        if not sched:
            from fastapi import HTTPException
            raise HTTPException(404, "Schedule not found")

        seed = sched["seed_keyword"]
        seed_toks = _seed_tokens(seed)

        async with db.execute(
            "SELECT id, keyword FROM domain_keywords WHERE schedule_id = ?",
            (schedule_id,),
        ) as cur:
            rows = await cur.fetchall()

        updated = 0
        for row in rows:
            coh = _coherence_score(row["keyword"], seed_toks, seed)
            await db.execute(
                "UPDATE domain_keywords SET coherence = ? WHERE id = ?",
                (coh, row["id"]),
            )
            updated += 1
        await db.commit()

    return {"updated": updated, "seed": seed}


@router.post("/schedules/{schedule_id}/generate-tone")
async def generate_tone(schedule_id: int, background_tasks: BackgroundTasks):
    """Uruchom pełny pipeline generowania Tone of Voice (w tle)."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        sched = await get_schedule(db, schedule_id)
    if not sched:
        from fastapi import HTTPException
        raise HTTPException(404, "Schedule not found")

    async def _run():
        try:
            result = await _generate_tone_for_schedule(schedule_id)
            logger.info(f"[ToneOfVoice] Done for {schedule_id}: {result.get('steps', [])}")
        except Exception as e:
            logger.error(f"[ToneOfVoice] Failed for {schedule_id}: {e}")

    background_tasks.add_task(_run)
    return {"status": "generating", "schedule_id": schedule_id, "domain": sched["domain"]}


@router.get("/schedules/{schedule_id}/tone")
async def get_tone(schedule_id: int):
    """Pobierz aktualny tone_of_voice dla harmonogramu."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT tone_of_voice FROM domain_schedules WHERE id=?",
            (schedule_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        from fastapi import HTTPException
        raise HTTPException(404, "Schedule not found")
    tone = (dict(row).get("tone_of_voice") or "").strip()
    is_generated = bool(tone and tone not in _TONE_PRESETS)
    return {"tone_of_voice": tone, "is_generated": is_generated}


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

    # SEO #24: improved cannibalization detection — Jaccard similarity instead of first-2-tokens
    def _stem(kw: str) -> str:
        tokens = re.sub(r"[^a-z0-9ąćęłńóśźż ]+", "", kw.lower()).split()
        return " ".join(tokens[:2])

    def _kw_tokens(kw: str) -> set:
        return set(re.sub(r"[^a-z0-9ąćęłńóśźż ]+", "", kw.lower()).split())

    groups: dict[str, list] = {}
    for row in all_kws:
        stem = _stem(row["keyword"])
        groups.setdefault(stem, []).append(row)

    # Also detect cross-stem collisions via Jaccard similarity > 0.7
    _all_items = list(all_kws)
    for i in range(len(_all_items)):
        for j in range(i + 1, min(len(_all_items), i + 100)):  # limit to nearby for performance
            t1 = _kw_tokens(_all_items[i]["keyword"])
            t2 = _kw_tokens(_all_items[j]["keyword"])
            if not t1 or not t2:
                continue
            _jaccard = len(t1 & t2) / len(t1 | t2)
            if _jaccard > 0.7:
                stem_key = f"~sim:{_stem(_all_items[i]['keyword'])}"
                if stem_key not in groups:
                    groups[stem_key] = []
                if _all_items[i] not in groups[stem_key]:
                    groups[stem_key].append(_all_items[i])
                if _all_items[j] not in groups[stem_key]:
                    groups[stem_key].append(_all_items[j])

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

        # FIX #66: use module-level re import instead of inline import
        slug = re.sub(r"[^a-z0-9]+", "-", anchor.lower()).strip("-")

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
            # SEO #25: pillar-first ordering — ensure pillar pages published before supporting
            # SEO #22: quick wins (high volume + low KD) prioritized after pillars
            async with db.execute(
                """SELECT * FROM domain_keywords
                   WHERE schedule_id = ? AND status = 'pending'
                   ORDER BY
                     CASE keyword_type WHEN 'pillar' THEN 0 ELSE 1 END,
                     CASE WHEN search_volume >= 50 AND keyword_difficulty < 25 THEN 0 ELSE 1 END,
                     keyword_difficulty ASC, search_volume DESC
                   LIMIT ?""",
                (schedule_id, limit)
            ) as cur:
                keywords = [dict(r) for r in await cur.fetchall()]

        if not keywords:
            await _job_update(job_id, done=1, error="Brak pending keywords")
            return

        # SEO #113: seasonal keyword detection — boost keywords whose search volume peaks in current/next month
        # Uses monthly_searches trend data from topical_map (stored during map generation)
        _current_month = datetime.now(timezone.utc).month
        _seasonal_boost_kws = set()
        try:
            async with aiosqlite.connect(DB_PATH) as _sdb:
                _sdb.row_factory = aiosqlite.Row
                async with _sdb.execute(
                    "SELECT keyword, search_volume FROM domain_keywords WHERE schedule_id=? AND status='pending'",
                    (schedule_id,)
                ) as _scur:
                    _seasonal_rows = await _scur.fetchall()
            # Simple seasonal heuristic: keywords containing month-related terms get boosted
            _month_terms_pl = {
                1: ["styczeń", "noworoczn", "zimow"],
                2: ["luty", "walentynk", "karnawał"],
                3: ["marzec", "wiosenn", "wielkanoc"],
                4: ["kwiecień", "wiosna", "wielkanoc"],
                5: ["maj", "majówk", "komunia"],
                6: ["czerwiec", "wakacj", "letn"],
                7: ["lipiec", "wakacj", "letn", "urlop"],
                8: ["sierpień", "wakacj", "szkoln"],
                9: ["wrzesień", "szkoln", "jesień"],
                10: ["październik", "halloween", "jesien"],
                11: ["listopad", "czarny piątek", "black friday"],
                12: ["grudzień", "świąt", "boże narodzenie", "sylwest", "prezent"],
            }
            _season_terms = _month_terms_pl.get(_current_month, []) + _month_terms_pl.get((_current_month % 12) + 1, [])
            for _sr in _seasonal_rows:
                _kw_lower = (_sr["keyword"] or "").lower()
                if any(t in _kw_lower for t in _season_terms):
                    _seasonal_boost_kws.add(_sr["keyword"])
            if _seasonal_boost_kws:
                # Re-sort: seasonal keywords first (after pillars), then normal order
                _pillar_kws = [k for k in keywords if k["keyword_type"] == "pillar"]
                _seasonal_kws = [k for k in keywords if k["keyword"] in _seasonal_boost_kws and k["keyword_type"] != "pillar"]
                _rest_kws = [k for k in keywords if k["keyword"] not in _seasonal_boost_kws and k["keyword_type"] != "pillar"]
                keywords = _pillar_kws + _seasonal_kws + _rest_kws
                logger.info(f"[Autopilot] SEO #113: {len(_seasonal_boost_kws)} seasonal keywords boosted for month {_current_month}")
        except Exception as _se:
            logger.warning(f"[Autopilot] Seasonal detection error: {_se}")

        wp_ok = await check_wp_credentials(sched["domain"], sched["wp_login"], sched["wp_pass"],
                                           http_user=sched.get("http_user", ""), http_pass=sched.get("http_pass", ""))
        if not wp_ok:
            logger.warning(f"[Autopilot] WP credentials invalid for {sched['domain']} — aborting")
            await _job_update(job_id, done=1, error=f"WP credentials invalid for {sched['domain']}")
            return

        await _job_update(job_id, total=len(keywords))

        # SEO #92: auto-sync categories if none synced yet (prevents "Uncategorized" posts)
        try:
            async with aiosqlite.connect(DB_PATH) as _catdb:
                async with _catdb.execute(
                    "SELECT COUNT(*) FROM domain_categories WHERE schedule_id=? AND synced=1",
                    (schedule_id,)
                ) as _catcur:
                    _synced_count = (await _catcur.fetchone())[0]
            if _synced_count == 0:
                logger.info(f"[Autopilot] No synced categories — auto-syncing for schedule {schedule_id}")
                try:
                    await sync_categories(schedule_id)
                    # Reload keywords to get updated wp_category_id
                    async with aiosqlite.connect(DB_PATH) as db:
                        db.row_factory = aiosqlite.Row
                        async with db.execute(
                            """SELECT * FROM domain_keywords
                               WHERE schedule_id = ? AND status = 'pending'
                               ORDER BY CASE keyword_type WHEN 'pillar' THEN 0 ELSE 1 END,
                               keyword_difficulty ASC, search_volume DESC LIMIT ?""",
                            (schedule_id, limit)
                        ) as cur:
                            keywords = [dict(r) for r in await cur.fetchall()]
                except Exception as _sync_err:
                    logger.warning(f"[Autopilot] Category auto-sync failed: {_sync_err}")
        except Exception:
            pass

        # SEO #29: fetch ALL published posts then random sample 50 — ensures links to older valuable posts
        # SEO #114: topical-map-aware interlinking — include pillar_label for cluster-aware linking
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT title, keyword, wp_post_url, pillar_label, keyword_type FROM domain_keywords
                   WHERE schedule_id=? AND status='published' AND wp_post_url!=''""",
                (schedule_id,)
            ) as cur:
                _all_published = [{"title": r["title"] or r["keyword"], "keyword": r["keyword"],
                                   "url": r["wp_post_url"], "cluster": r["pillar_label"] or "",
                                   "type": r["keyword_type"] or "supporting"} for r in await cur.fetchall()]
        if len(_all_published) > 50:
            published_posts = random.sample(_all_published, 50)
        else:
            published_posts = _all_published

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

                # SEO #114: prioritize same-cluster posts for interlinking
                _kw_cluster = kw_row.get("pillar_label", "")
                if _kw_cluster and published_posts:
                    _cluster_posts = [p for p in published_posts if p.get("cluster") == _kw_cluster]
                    _other_posts = [p for p in published_posts if p.get("cluster") != _kw_cluster]
                    _interlink_posts = _cluster_posts + _other_posts[:max(0, 50 - len(_cluster_posts))]
                else:
                    _interlink_posts = published_posts

                # SEO #111: rel prev/next for series — find previous article in same cluster
                _prev_url = ""
                if _kw_cluster:
                    _same_cluster_published = [p for p in _all_published if p.get("cluster") == _kw_cluster]
                    if _same_cluster_published:
                        _prev_url = _same_cluster_published[-1].get("url", "")

                async with asyncio.timeout(360):  # 6 min per artykuł
                    article = await _with_retry(lambda: generate_article(
                        topic=keyword,
                        client_domain=sched["client_domain"] or "",
                        anchor_text=sched["anchor_text"] or keyword,
                        language=sched["language"],
                        variation_hint=variation,
                        custom_prompt=_effective_prompt(sched),
                        dfs_login=DFS_LOGIN,
                        dfs_password=DFS_PASSWORD,
                        location_code=location_code,
                        published_posts=_interlink_posts,
                        domain_fingerprints=domain_fingerprints,
                        pillar_page_url=_pillar_url,
                        pillar_page_anchor=_pillar_anchor,
                        pbn_domain=sched["domain"],
                    ))
                    title = article["title"]
                    content = article["content"]
                    excerpt = article.get("excerpt", "")
                    lsi_tags = article.get("lsi_tags", [])
                    category_id = kw_row.get("wp_category_id") or None
                    _wc = article.get("word_count", 0)
                    _kd = article.get("keyword_density", 0)

                    # SEO #111: inject rel prev/next for series articles (same cluster)
                    if _prev_url:
                        content = f'<link rel="prev" href="{_prev_url}" />\n{content}'

                    # SEO #28: quality gate — skip low-quality articles
                    if _wc < 600:
                        logger.warning(f"[Autopilot] Quality gate: '{keyword}' too short ({_wc} words) — skipping")
                        _results.append({"status": "skipped", "keyword": keyword, "error": f"Too short: {_wc} words"})
                        continue
                    if _kd > 0 and (_kd < 0.5 or _kd > 3.0):
                        logger.warning(f"[Autopilot] Quality gate: '{keyword}' KW density {_kd}% out of range — publishing anyway")

                    # SEO #26: check for duplicate slug on WP before publishing
                    from services.wordpress_service import _keyword_to_slug
                    _check_slug = _keyword_to_slug(keyword)
                    try:
                        import httpx as _httpx
                        _wp_bases = [f"https://{sched['domain']}", f"http://{sched['domain']}"] if not sched['domain'].startswith("http") else [sched['domain'].rstrip("/")]
                        async with _httpx.AsyncClient(verify=False, timeout=10) as _sc:
                            for _wb in _wp_bases:
                                try:
                                    _sr = await _sc.get(f"{_wb}/wp-json/wp/v2/posts", params={"slug": _check_slug, "per_page": 1})
                                    if _sr.status_code == 200 and _sr.json():
                                        logger.warning(f"[Autopilot] Duplicate slug '{_check_slug}' on {sched['domain']} — appending suffix")
                                        # SEO #67: use clean slug suffix instead of date in parens
                                        import hashlib as _hs
                                        _suffix = _hs.md5(keyword.encode()).hexdigest()[:4]
                                        title = f"{title} — {_suffix}"
                                    break
                                except Exception:
                                    continue
                    except Exception:
                        pass

                    img_prompt = (
                        f"A photorealistic scene that visually represents: {title}. "
                        f"Show a concrete moment or setting related to '{keyword}'. "
                        f"Editorial photography style, natural lighting, shallow depth of field. "
                        f"NO text, NO letters, NO watermarks, NO logos. 16:9 landscape."
                    )
                    image_b64, image_provider = await _fetch_image(
                        sched.get("image_source", "freepik_flux"), keyword, title, img_prompt
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
                               wp_post_url, status, keyword) VALUES (?,?,?,?,?,?,?,?)""",
                            (None, sched["client_domain"] or sched["domain"],
                             sched["my_domain_id"], title, content, wp_url, "published", keyword)
                        )
                        await db.execute(
                            """UPDATE domain_keywords SET status='published', title=?, wp_post_url=?, published_at=?
                               WHERE id=?""",
                            (title, wp_url, datetime.now(timezone.utc).isoformat(), kw_row["id"])
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
                (total_pub, datetime.now(timezone.utc).isoformat(), schedule_id)
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


@router.post("/keywords/{keyword_id}/preview")
async def preview_keyword(keyword_id: int):
    """
    Generate article preview for a keyword without publishing to WP.
    Returns the full article content for review.
    """
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM domain_keywords WHERE id=?", (keyword_id,)) as cur:
            kw_row = await cur.fetchone()
        if not kw_row:
            from fastapi import HTTPException
            raise HTTPException(404, "Keyword nie istnieje")
        kw_row = dict(kw_row)
        sched = await get_schedule(db, kw_row["schedule_id"])
    if not sched:
        from fastapi import HTTPException
        raise HTTPException(404, "Harmonogram nie istnieje")

    keyword = kw_row["keyword"]
    variation = random.choice(VARIATION_HINTS)
    location_code = 2616 if sched["language"] == "pl" else 2840

    pillar_url, pillar_keyword = "", ""
    if kw_row.get("keyword_type") == "supporting" and kw_row.get("pillar_anchor"):
        pillar_url, pillar_keyword = await _get_pillar_url(
            kw_row["schedule_id"], sched["my_domain_id"], kw_row["pillar_anchor"]
        )

    from services.openai_service import generate_article
    article = await generate_article(
        topic=keyword,
        client_domain=sched["client_domain"] or "",
        anchor_text=sched["anchor_text"] or keyword,
        language=sched["language"],
        variation_hint=variation,
        custom_prompt=_effective_prompt(sched),
        dfs_login=DFS_LOGIN,
        dfs_password=DFS_PASSWORD,
        location_code=location_code,
        pillar_page_url=pillar_url,
        pillar_page_anchor=pillar_keyword or kw_row.get("pillar_label", ""),
        pbn_domain=sched["domain"],
    )

    return {
        "keyword": keyword,
        "title": article["title"],
        "content": article["content"],
        "excerpt": article.get("excerpt", ""),
        "lsi_tags": article.get("lsi_tags", []),
    }


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

        # Velocity throttle: slow down once domain has many articles (anti-spam signal)
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM domain_keywords WHERE my_domain_id=? AND status='published'",
                (sched["my_domain_id"],)
            ) as cur:
                total_published = (await cur.fetchone())[0]
        if total_published > 50:
            limit = min(limit, 1)  # max 1/day for established sites
        elif total_published > 20:
            limit = min(limit, 2)

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
                   ORDER BY CASE keyword_type WHEN 'pillar' THEN 0 ELSE 1 END,
                   keyword_difficulty ASC, search_volume DESC LIMIT ?""",
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

        _first_article = True
        for kw_row in keywords:
            keyword = kw_row["keyword"]

            # Anti-footprint: random delay between articles (30-180s) to avoid burst publishing
            if not _first_article:
                delay = random.randint(30, 180)
                logger.info(f"[Daily] Anti-footprint delay: waiting {delay}s before next article on {sched['domain']}")
                await asyncio.sleep(delay)
            _first_article = False

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
                            custom_prompt=_effective_prompt(sched),
                            dfs_login=DFS_LOGIN,
                            dfs_password=DFS_PASSWORD,
                            location_code=location_code,
                            published_posts=published_posts,
                            domain_fingerprints=domain_fingerprints,
                            pillar_page_url=_pillar_url,
                            pillar_page_anchor=_pillar_anchor,
                            pbn_domain=sched["domain"],
                        ))
                        excerpt = article.get("excerpt", "")
                        lsi_tags = article.get("lsi_tags", [])

                        img_prompt_daily = (
                            f"A photorealistic scene that visually represents: {article['title']}. "
                            f"Show a concrete moment or setting related to '{keyword}'. "
                            f"Editorial photography style, natural lighting, shallow depth of field. "
                            f"NO text, NO letters, NO watermarks, NO logos. 16:9 landscape."
                        )
                        image_b64, _ = await _fetch_image(
                            sched.get("image_source", "freepik_flux"), keyword, article["title"], img_prompt_daily
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
                                "INSERT INTO posts (client_id, client_domain, my_domain_id, title, content, wp_post_url, status, keyword) VALUES (?,?,?,?,?,?,?,?)",
                                (None, sched["client_domain"] or "",
                                 sched["my_domain_id"], article["title"], article["content"], wp_url, "published", keyword)
                            )
                            await db.execute(
                                "UPDATE domain_keywords SET status='published', title=?, wp_post_url=?, published_at=? WHERE id=?",
                                (article["title"], wp_url, datetime.now(timezone.utc).isoformat(), kw_row["id"])
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
                (total_pub, datetime.now(timezone.utc).isoformat(), schedule_id)
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

    # Send Telegram summary (respects user preference)
    try:
        from api.notifications import should_notify
        if await should_notify("notify_autopilot_done"):
            from services.telegram_service import send_telegram
            total_pub = sum(r.get("published", 0) for r in final)
            total_fail = sum(r.get("failed", 0) for r in final)
            total_errors = sum(1 for r in final if r.get("error"))
            msg = (
                f"<b>Autopilot daily run</b>\n\n"
                f"Schedules: <b>{len(final)}</b>\n"
                f"Published: <b>{total_pub}</b>\n"
            )
            if total_fail:
                msg += f"Failed: <b>{total_fail}</b>\n"
            if total_errors:
                msg += f"Errors: <b>{total_errors}</b>\n"
            await send_telegram(msg)
    except Exception:
        pass

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
        # Next cron run: daily between 06:00-11:00 UTC (randomized for anti-footprint)
        now = datetime.now(timezone.utc)
        next_cron = now.replace(hour=8, minute=30, second=0, microsecond=0)
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


class ImportMapRequest(BaseModel):
    my_domain_id: int
    seed_keyword: str
    pillars: list  # [{anchor, label, pillar_keyword, pillar_volume, pillar_difficulty, supporting_keywords: [{keyword, search_volume, keyword_difficulty}]}]
    posts_per_day: int = 1
    language: str = "pl"
    client_domain: str = ""
    anchor_text: str = ""
    image_source: str = "freepik_flux"
    custom_prompt: str = ""


@router.post("/import-map")
async def import_topical_map(body: ImportMapRequest):
    """
    Import a standalone Topical Map into an Autopilot schedule.
    Creates the schedule + loads all keywords directly (skips DataForSEO generation).
    """
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        # Check domain exists
        async with db.execute("SELECT id FROM my_domains WHERE id = ?", (body.my_domain_id,)) as cur:
            if not await cur.fetchone():
                from fastapi import HTTPException
                raise HTTPException(404, "Domena nie istnieje")

        # Create schedule
        cursor = await db.execute(
            """INSERT INTO domain_schedules
               (my_domain_id, seed_keyword, posts_per_day, language, client_domain, anchor_text, image_source, custom_prompt, map_generated)
               VALUES (?,?,?,?,?,?,?,?,1)""",
            (body.my_domain_id, body.seed_keyword, body.posts_per_day,
             body.language, body.client_domain, body.anchor_text,
             body.image_source, body.custom_prompt)
        )
        schedule_id = cursor.lastrowid

        inserted = 0
        for pillar in body.pillars:
            anchor = pillar.get("anchor", "")
            label = pillar.get("label", "")

            # Save category
            await db.execute(
                """INSERT OR IGNORE INTO domain_categories
                   (schedule_id, my_domain_id, pillar_anchor, pillar_label)
                   VALUES (?,?,?,?)""",
                (schedule_id, body.my_domain_id, anchor, label)
            )

            # Pillar keyword
            pk = pillar.get("pillar_keyword", "")
            if pk:
                await db.execute(
                    """INSERT INTO domain_keywords
                       (schedule_id, my_domain_id, keyword, keyword_type, pillar_label, pillar_anchor, search_volume, keyword_difficulty, status)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (schedule_id, body.my_domain_id, pk, "pillar",
                     label, anchor, pillar.get("pillar_volume", 0), pillar.get("pillar_difficulty", 0), "pending")
                )
                inserted += 1

            # Supporting keywords
            for sk in pillar.get("supporting_keywords", []):
                kw = sk.get("keyword", "")
                if kw:
                    await db.execute(
                        """INSERT INTO domain_keywords
                           (schedule_id, my_domain_id, keyword, keyword_type, pillar_label, pillar_anchor, search_volume, keyword_difficulty, status)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (schedule_id, body.my_domain_id, kw, "supporting",
                         label, anchor, sk.get("search_volume", 0), sk.get("keyword_difficulty", 0), "pending")
                    )
                    inserted += 1

        await db.execute(
            "UPDATE domain_schedules SET total_keywords=? WHERE id=?",
            (inserted, schedule_id)
        )
        await db.commit()

    return {"schedule_id": schedule_id, "inserted": inserted, "message": f"Zaimportowano {inserted} fraz do autopilota"}


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


class BulkCreateAutoRequest(BaseModel):
    domain_ids: List[int]
    posts_per_day: int = 1
    language: str = "pl"
    min_volume: int = 10
    custom_prompt: str = ""


@router.post("/bulk-create-auto")
async def bulk_create_auto(body: BulkCreateAutoRequest):
    """
    Auto-seed bulk create: for each PBN domain, DataForSEO discovers what the
    domain ranks for (or could rank for), GPT picks the best seed keyword,
    then creates the schedule.
    """
    await ensure_tables()
    if not DFS_LOGIN or not DFS_PASSWORD:
        raise HTTPException(400, "Brak konfiguracji DataForSEO — ustaw DATAFORSEO_LOGIN i DATAFORSEO_PASSWORD")

    dfs = DataForSEOClient(DFS_LOGIN, DFS_PASSWORD)
    location_code = 2616 if body.language == "pl" else 2840
    results = []
    created_count = 0
    skipped_count = 0
    error_count = 0

    async def _dfs_call_safe(coro_fn, *args, retries=2, **kwargs):
        """Call DataForSEO with retry on rate limit (429 / Too many requests)."""
        for attempt in range(retries + 1):
            try:
                return await coro_fn(*args, **kwargs)
            except Exception as e:
                err_str = str(e).lower()
                if "too many" in err_str or "429" in err_str:
                    wait = 3 * (attempt + 1)
                    logger.info(f"[auto-seed] DFS rate limit, waiting {wait}s (attempt {attempt+1})")
                    await asyncio.sleep(wait)
                    if attempt == retries:
                        raise
                else:
                    raise
        return None

    for idx, domain_id in enumerate(body.domain_ids):
        # Rate limit: wait between domains (skip first)
        if idx > 0:
            await asyncio.sleep(3)

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT id, domain FROM my_domains WHERE id = ?", (domain_id,)) as cur:
                dom = await cur.fetchone()
        if not dom:
            results.append({"domain_id": domain_id, "domain": "?", "error": "not found"})
            error_count += 1
            continue

        domain_name = dict(dom)["domain"]

        # 1) DataForSEO: what does this PBN domain rank for?
        try:
            site_keywords = await _dfs_call_safe(
                dfs.keywords_for_site,
                domain_name, location_code=location_code,
                language_code=body.language, limit=200,
            )
            site_keywords = site_keywords or []
        except Exception as e:
            logger.warning(f"[auto-seed] DFS keywords_for_site failed for {domain_name}: {e}")
            site_keywords = []

        # 2) GPT picks the best seed — from DFS data or from domain name analysis
        gpt_model = "gpt-4o-mini"
        try:
            gpt_model = await get_gpt_model()
        except Exception:
            pass

        from openai import AsyncOpenAI as _AO
        _oai = _AO()

        if site_keywords:
            # Domain has rankings — pick seed from actual keyword data
            top_kws = sorted(site_keywords, key=lambda x: x["search_volume"], reverse=True)[:60]
            kw_list = "\n".join(f"- {k['keyword']} (vol: {k['search_volume']})" for k in top_kws)
            gpt_prompt = f"""Pick the single best seed keyword for building a topical map on this PBN domain.

RULES:
- The seed must be a SPECIFIC niche topic (2-4 words), NOT a generic term
- BAD examples: "sklep internetowy", "prawo pracy", "zdrowie" — too broad, no topical authority
- GOOD examples: "buty do biegania", "prawo pracy zdalnej", "suplementy diety sportowców" — specific niche
- The seed should match the ACTUAL content/niche of the domain, not be a generic category
- It should allow generating 15-30 supporting articles with long tail keywords

Domain: {domain_name}
Keywords it ranks for:
{kw_list}

Return: {{"seed": "specific niche keyword in {body.language}", "reason": "short reason"}}"""
        else:
            # New domain — scrape page content to determine niche
            page_content = ""
            try:
                page_content = await _dfs_call_safe(dfs.page_content, f"https://{domain_name}")
                page_content = page_content or ""
            except Exception as e:
                logger.warning(f"[auto-seed] page_content failed for {domain_name}: {e}")
            if not page_content:
                try:
                    page_content = await _dfs_call_safe(dfs.page_content, f"http://{domain_name}")
                    page_content = page_content or ""
                except Exception:
                    pass

            if page_content:
                # Trim to reasonable length for GPT
                content_snippet = page_content[:4000]
                gpt_prompt = f"""Analyze the content of this website and pick the best seed keyword for building a topical map.

RULES:
- The seed must be a SPECIFIC niche topic (2-4 words, in {body.language}), NOT a generic term
- BAD: "sklep internetowy", "zdrowie", "moda" — too broad
- GOOD: "buty do biegania", "kosmetyki naturalne", "meble ogrodowe" — specific niche
- Match the seed to the ACTUAL products/services/content on the page
- It should allow generating 15-30 supporting articles with long tail keywords

Domain: {domain_name}
Page content:
{content_snippet}

Return: {{"seed": "specific niche keyword in {body.language}", "reason": "short reason based on page content"}}"""
            else:
                # Last resort — analyze domain name
                gpt_prompt = f"""Analyze this domain name and pick the best seed keyword for building a topical map.

RULES:
- Look at the domain name, interpret what niche/topic it suggests
- The seed must be a SPECIFIC niche topic (2-4 words, in {body.language}), NOT a generic term
- BAD: "sklep internetowy", "zdrowie" — too broad, useless for topical authority
- GOOD: "zabawki edukacyjne dla dzieci", "naturalne kosmetyki do włosów" — specific niche
- It should allow generating 15-30 supporting articles with long tail keywords

Domain: {domain_name}

Return: {{"seed": "specific niche keyword in {body.language}", "reason": "short reason"}}"""

        try:
            response = await _oai.chat.completions.create(
                model=gpt_model,
                messages=[
                    {"role": "system", "content": "You are an SEO expert specializing in PBN domain analysis. Return valid JSON only, no markdown."},
                    {"role": "user", "content": gpt_prompt},
                ],
                temperature=0.3,
                max_tokens=200,
            )
            raw = response.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```$", "", raw)
            seed_data = _json.loads(raw)
            chosen_seed = seed_data.get("seed", "")
            reason = seed_data.get("reason", "")
            if not site_keywords:
                reason = f"(z analizy strony) {reason}"
        except Exception as e:
            logger.warning(f"[auto-seed] GPT failed for {domain_name}: {e}")
            # Last resort fallback: extract words from domain name
            domain_base = domain_name.replace("www.", "").split(".")[0]
            chosen_seed = domain_base
            reason = "fallback: nazwa domeny"

        if not chosen_seed:
            results.append({"domain_id": domain_id, "domain": domain_name, "error": "AI nie wybrało seeda"})
            error_count += 1
            continue

        # 3) Create schedule (skip if already exists with same seed)
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT id FROM domain_schedules WHERE my_domain_id = ? AND seed_keyword = ?",
                (domain_id, chosen_seed)
            ) as cur:
                existing = await cur.fetchone()
            if existing:
                results.append({"domain_id": domain_id, "domain": domain_name, "seed_keyword": chosen_seed, "skipped": True, "reason": "harmonogram już istnieje"})
                skipped_count += 1
                continue

            cursor = await db.execute(
                """INSERT INTO domain_schedules
                   (my_domain_id, seed_keyword, posts_per_day, language, min_volume, custom_prompt)
                   VALUES (?,?,?,?,?,?)""",
                (domain_id, chosen_seed, body.posts_per_day, body.language, body.min_volume, body.custom_prompt),
            )
            await db.commit()
            results.append({"domain_id": domain_id, "domain": domain_name, "seed_keyword": chosen_seed, "reason": reason, "schedule_id": cursor.lastrowid})
            created_count += 1

    return {
        "created": created_count,
        "skipped": skipped_count,
        "errors": error_count,
        "results": results,
    }


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
async def bulk_run_schedules(body: BulkActionRequest, background_tasks: BackgroundTasks):
    """
    Uruchom publikację hurtowo w tle — zwraca job_id natychmiast.
    Frontend odpytuje GET /bulk-run-status/{job_id} co 5s.
    """
    await ensure_tables()
    job_id = str(_uuid.uuid4())
    await _job_create(job_id, 0)  # schedule_id=0 means bulk job
    await _job_update(job_id, total=len(body.schedule_ids))
    background_tasks.add_task(_bulk_run_bg, job_id, body.schedule_ids, body.limit)
    return {"job_id": job_id, "status": "running", "schedules": len(body.schedule_ids)}


async def _bulk_run_bg(job_id: str, schedule_ids: list[int], limit_override: int | None):
    """Background task: bulk publish across multiple schedules."""
    sem = asyncio.Semaphore(3)
    all_results = []
    total_pub = 0
    total_fail = 0

    async def _run_one(schedule_id: int) -> dict:
        async with sem:
            async with aiosqlite.connect(DB_PATH) as db:
                sched = await get_schedule(db, schedule_id)
            if not sched:
                return {"schedule_id": schedule_id, "error": "not found"}

            limit = limit_override if limit_override else sched["posts_per_day"]

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
                        custom_prompt=_effective_prompt(sched),
                        dfs_login=DFS_LOGIN,
                        dfs_password=DFS_PASSWORD,
                        location_code=location_code,
                        published_posts=published_posts,
                        domain_fingerprints=domain_fingerprints,
                        pbn_domain=sched["domain"],
                    )
                    img_prompt_bulk = (
                        f"A photorealistic scene that visually represents: {article['title']}. "
                        f"Show a concrete moment or setting related to '{keyword}'. "
                        f"Editorial photography style, natural lighting, shallow depth of field. "
                        f"NO text, NO letters, NO watermarks, NO logos. 16:9 landscape."
                    )
                    image_b64, _ = await _fetch_image(
                        sched.get("image_source", "freepik_flux"), keyword, article["title"], img_prompt_bulk
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
                                   wp_post_url, status, keyword) VALUES (?,?,?,?,?,?,?,?)""",
                                (None, sched["client_domain"] or "",
                                 sched["my_domain_id"], article["title"], article["content"], wp_url, "published", keyword)
                            )
                            await db.execute(
                                "UPDATE domain_keywords SET status='published', title=?, wp_post_url=?, published_at=? WHERE id=?",
                                (article["title"], wp_url, datetime.now(timezone.utc).isoformat(), kw_row["id"])
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
                    total_pub_count = (await cur.fetchone())[0]
                await db.execute(
                    "UPDATE domain_schedules SET published_count=?, last_run_at=? WHERE id=?",
                    (total_pub_count, datetime.now(timezone.utc).isoformat(), schedule_id)
                )
                await db.commit()

            return {"schedule_id": schedule_id, "domain": sched["domain"], "published": published, "failed": failed}

    try:
        # Run schedules concurrently (semaphore limits to 3 at a time)
        tasks = [_run_one(sid) for sid in schedule_ids]
        for coro in asyncio.as_completed(tasks):
            result = await coro
            all_results.append(result)
            total_pub = sum(r.get("published", 0) for r in all_results)
            total_fail = sum(r.get("failed", 0) for r in all_results)
            # Update job progress after each schedule completes
            await _job_update(
                job_id,
                published=total_pub,
                failed=total_fail,
                total_published=total_pub,
                results=all_results,
            )

        # Mark job as done
        await _job_update(job_id, done=1, published=total_pub, failed=total_fail, total_published=total_pub, results=all_results)

        # Telegram notification
        try:
            from api.notifications import should_notify
            if await should_notify("notify_bulk_publish_done"):
                from services.telegram_service import send_telegram
                msg = (
                    f"<b>Bulk Run zakończony</b>\n\n"
                    f"Harmonogramów: {len(schedule_ids)}\n"
                    f"Opublikowano: <b>{total_pub}</b>\n"
                    f"Błędów: <b>{total_fail}</b>"
                )
                await send_telegram(msg)
        except Exception:
            pass

    except Exception as e:
        logger.error(f"[BulkRunBG] Fatal error: {e}")
        await _job_update(job_id, done=1, error=str(e), results=all_results)


@router.get("/bulk-run-status/{job_id}")
async def bulk_run_status(job_id: str):
    """Pobierz status hurtowej publikacji."""
    job = await _job_get(job_id)
    if not job:
        from fastapi import HTTPException
        raise HTTPException(404, "Job nie istnieje")
    return job


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


@router.get("/logs")
async def get_autopilot_logs():
    """Return recent autopilot activity from domain_keywords (last 200 actions)."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT dk.id, dk.keyword, dk.status, dk.published_at, dk.wp_post_url, dk.title,
                      ds.seed_keyword, md.domain
               FROM domain_keywords dk
               JOIN domain_schedules ds ON ds.id = dk.schedule_id
               JOIN my_domains md ON md.id = ds.my_domain_id
               WHERE dk.status IN ('published', 'failed')
               ORDER BY dk.published_at DESC
               LIMIT 200"""
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return rows


@router.get("/export-csv")
async def export_all_keywords_csv():
    """Export ALL autopilot keywords across all schedules as CSV."""
    from fastapi.responses import StreamingResponse
    import csv
    import io

    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT dk.keyword, dk.keyword_type, dk.pillar_label, dk.search_volume,
                      dk.keyword_difficulty, dk.status, dk.published_at, dk.wp_post_url, dk.title,
                      ds.seed_keyword, md.domain
               FROM domain_keywords dk
               JOIN domain_schedules ds ON ds.id = dk.schedule_id
               JOIN my_domains md ON md.id = ds.my_domain_id
               ORDER BY md.domain, dk.status, dk.search_volume DESC"""
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "domain", "seed_keyword", "keyword", "keyword_type", "pillar_label",
        "search_volume", "keyword_difficulty", "status", "published_at", "wp_post_url", "title"
    ])
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="autopilot_all_keywords.csv"'}
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
    prompt = f"A photorealistic scene that visually represents: {title}. Show a concrete moment related to '{keyword}'. Editorial photography, natural lighting, NO text, NO watermarks. 16:9 landscape."
    results = {}

    for source in ["freepik_flux", "freepik_zimage", "freepik_stock", "gemini", "dalle"]:
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


# ---------------------------------------------------------------------------
# AI Seed Keyword Discovery — scrape client domain via DataForSEO,
# then use GPT to pick best seed keywords for topical maps.
# ---------------------------------------------------------------------------

class AISeedRequest(BaseModel):
    client_domain: str
    language: str = "pl"
    count: int = 5  # how many seed keywords to suggest


@router.post("/ai-seed-keywords")
async def ai_seed_keywords(body: AISeedRequest):
    """
    Discover seed keywords for a client domain:
    1. DataForSEO keywords_for_site — what the domain ranks for
    2. GPT picks the best seed keywords for topical map generation
    """
    if not DFS_LOGIN or not DFS_PASSWORD:
        from fastapi import HTTPException
        raise HTTPException(400, "DataForSEO not configured")
    if not body.client_domain.strip():
        from fastapi import HTTPException
        raise HTTPException(400, "client_domain is required")

    location_code = 2616 if body.language == "pl" else 2840  # Poland or US

    dfs = DataForSEOClient(DFS_LOGIN, DFS_PASSWORD)
    try:
        site_keywords = await dfs.keywords_for_site(
            body.client_domain.strip(),
            location_code=location_code,
            language_code=body.language,
            limit=200,
        )
    except Exception as e:
        logger.warning(f"[AI-Seed] DataForSEO keywords_for_site failed: {e}")
        site_keywords = []

    if not site_keywords:
        return {
            "seeds": [],
            "message": "Nie znaleziono fraz — domena może nie mieć jeszcze pozycji w Google.",
            "raw_keywords": [],
        }

    # Group by topic clusters using GPT
    top_kws = sorted(site_keywords, key=lambda x: x["search_volume"], reverse=True)[:80]
    kw_list = "\n".join(f"- {k['keyword']} (vol: {k['search_volume']}, pos: {k.get('position', '?')})" for k in top_kws)

    from openai import AsyncOpenAI
    openai_client = AsyncOpenAI()
    gpt_model = "gpt-4o-mini"
    try:
        from services.openai_service import get_gpt_model
        gpt_model = await get_gpt_model()
    except Exception:
        pass

    prompt = f"""Analyze these keywords that a website ranks for and suggest {body.count} best seed keywords for building PBN topical maps.

Each seed keyword should:
- Be a SPECIFIC niche topic (2-4 words), NOT a generic broad term
- BAD: "sklep internetowy", "zdrowie", "prawo pracy" — too broad, no topical authority
- GOOD: "buty do biegania", "prawo pracy zdalnej", "suplementy dla sportowców" — specific niche
- Have enough related subtopics for 15-30 supporting long-tail articles
- Match the actual niche/products the site operates in

Keywords the site ranks for:
{kw_list}

Return ONLY a JSON array of objects, no markdown:
[{{"seed": "specific niche keyword", "reason": "short reason why this is a good seed", "estimated_articles": 20}}]"""

    try:
        response = await openai_client.chat.completions.create(
            model=gpt_model,
            messages=[
                {"role": "system", "content": "You are an SEO expert. Return valid JSON only, no markdown fences."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=1000,
        )
        raw_text = response.choices[0].message.content.strip()
        # Strip markdown fences if GPT adds them
        if raw_text.startswith("```"):
            raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
            raw_text = re.sub(r"\s*```$", "", raw_text)
        import json
        seeds = json.loads(raw_text)
    except Exception as e:
        logger.warning(f"[AI-Seed] GPT analysis failed: {e}")
        # Fallback: pick top volume keywords as seeds
        seen = set()
        seeds = []
        for k in top_kws:
            words = k["keyword"].split()
            if len(words) >= 2 and k["keyword"] not in seen:
                seen.add(k["keyword"])
                seeds.append({"seed": k["keyword"], "reason": f"vol: {k['search_volume']}", "estimated_articles": 20})
                if len(seeds) >= body.count:
                    break

    return {
        "seeds": seeds[:body.count],
        "raw_keywords_count": len(site_keywords),
        "top_keywords": top_kws[:20],
    }

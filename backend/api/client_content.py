"""
Client Content Manager — generowanie contentu dla klientów.

Flow:
1. Klient ma domenę w client_domains
2. Generujesz Topical Map (DataForSEO) → lista fraz
3. Generujesz Tone of Voice (AI pipeline)
4. Zaznaczasz frazy → generujesz artykuły
5. Eksportujesz (CSV / HTML ZIP) do przeglądu klienta
"""
import asyncio
import base64 as _b64
import json as _json
import logging
import os
import random
import re
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from typing import List, Optional

import aiosqlite
import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import DB_PATH
from services.topical_map_service import generate_topical_map, _compute_site_metrics, _coherence_score, _seed_tokens
from services.openai_service import generate_article, get_gpt_model

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/client-content", tags=["client-content"])

DFS_LOGIN = os.getenv("DATAFORSEO_LOGIN", "")
DFS_PASSWORD = os.getenv("DATAFORSEO_PASSWORD", "")

# ── Models ──────────────────────────────────────────────────────────────────

class MapGenerateRequest(BaseModel):
    seed_keyword: str
    language: str = "pl"
    min_volume: int = 10


class ArticleGenerateRequest(BaseModel):
    keyword_ids: List[int]
    custom_prompt: str = ""
    language: str = "pl"
    image_source: str = "none"


class ToneUpdateRequest(BaseModel):
    tone_of_voice: str


# ── DB ──────────────────────────────────────────────────────────────────────

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS client_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_domain_id INTEGER NOT NULL REFERENCES client_domains(id) ON DELETE CASCADE,
    keyword TEXT NOT NULL,
    keyword_type TEXT DEFAULT 'supporting',
    pillar_label TEXT DEFAULT '',
    pillar_anchor TEXT DEFAULT '',
    search_volume INTEGER DEFAULT 0,
    keyword_difficulty REAL DEFAULT 0,
    coherence REAL DEFAULT 0.0,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS client_articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_domain_id INTEGER NOT NULL REFERENCES client_domains(id) ON DELETE CASCADE,
    keyword_id INTEGER REFERENCES client_keywords(id) ON DELETE SET NULL,
    keyword TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    excerpt TEXT DEFAULT '',
    status TEXT DEFAULT 'draft',
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
        # Migracje client_domains — dodaj kolumny jeśli nie istnieją
        for col, typedef in [
            ("seed_keyword", "TEXT DEFAULT ''"),
            ("tone_of_voice", "TEXT DEFAULT ''"),
            ("site_description", "TEXT DEFAULT ''"),
            ("map_generated", "INTEGER DEFAULT 0"),
            ("total_keywords", "INTEGER DEFAULT 0"),
            ("language", "TEXT DEFAULT 'pl'"),
        ]:
            try:
                await db.execute(f"ALTER TABLE client_domains ADD COLUMN {col} {typedef}")
            except Exception:
                pass
        await db.commit()
    _tables_ensured = True


async def _get_domain(db, domain_id: int) -> Optional[dict]:
    db.row_factory = aiosqlite.Row
    async with db.execute(
        "SELECT * FROM client_domains WHERE id = ?", (domain_id,)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


# ── GPT helper ──────────────────────────────────────────────────────────────

async def _gpt_call(system: str, user: str, temperature: float = 0.5,
                     max_tokens: int = 1500) -> str:
    from openai import AsyncOpenAI as _AO
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
            logger.warning(f"[ClientGPT] attempt {attempt+1} failed: {e} — retrying in {wait:.1f}s")
            await asyncio.sleep(wait)
    return ""


# ── Domain info ─────────────────────────────────────────────────────────────

@router.get("/domains/{domain_id}")
async def get_domain_info(domain_id: int):
    """Get client domain with content stats."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        dom = await _get_domain(db, domain_id)
        if not dom:
            raise HTTPException(404, "Domain not found")

        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT COUNT(*) as total, SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending FROM client_keywords WHERE client_domain_id=?",
            (domain_id,),
        ) as cur:
            kw_stats = dict(await cur.fetchone())

        async with db.execute(
            "SELECT COUNT(*) as total, SUM(CASE WHEN status='draft' THEN 1 ELSE 0 END) as drafts FROM client_articles WHERE client_domain_id=?",
            (domain_id,),
        ) as cur:
            art_stats = dict(await cur.fetchone())

    return {**dom, "keyword_stats": kw_stats, "article_stats": art_stats}


@router.patch("/domains/{domain_id}")
async def update_domain(domain_id: int, body: dict):
    """Update domain fields (seed_keyword, language, etc)."""
    await ensure_tables()
    allowed = {"seed_keyword", "language", "tone_of_voice", "site_description"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(400, "No valid fields")

    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [domain_id]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE client_domains SET {set_clause} WHERE id=?", values)
        await db.commit()
    return {"ok": True}


# ── Topical Map ─────────────────────────────────────────────────────────────

@router.post("/domains/{domain_id}/generate-map")
async def generate_map(domain_id: int, body: MapGenerateRequest):
    """Generate topical map for client domain."""
    await ensure_tables()
    if not DFS_LOGIN or not DFS_PASSWORD:
        raise HTTPException(400, "Brak konfiguracji DataForSEO")

    async with aiosqlite.connect(DB_PATH) as db:
        dom = await _get_domain(db, domain_id)
    if not dom:
        raise HTTPException(404, "Domain not found")

    location_code = 2616 if body.language == "pl" else 2840
    result = await generate_topical_map(
        body.seed_keyword, DFS_LOGIN, DFS_PASSWORD,
        location_code=location_code,
        language_code=body.language,
        min_volume=body.min_volume,
    )

    if not result.get("pillars"):
        raise HTTPException(400, result.get("error", "Brak wyników z DataForSEO"))

    seed_toks = _seed_tokens(body.seed_keyword)
    inserted = 0

    async with aiosqlite.connect(DB_PATH) as db:
        # Clear old keywords
        await db.execute("DELETE FROM client_keywords WHERE client_domain_id=?", (domain_id,))

        for pillar in result["pillars"]:
            label = pillar.get("label", "")
            anchor = pillar.get("anchor", label)
            # Insert pillar keyword
            coh = _coherence_score(label, seed_toks, body.seed_keyword)
            await db.execute(
                """INSERT INTO client_keywords
                   (client_domain_id, keyword, keyword_type, pillar_label, pillar_anchor,
                    search_volume, keyword_difficulty, coherence)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (domain_id, label, "pillar", label, anchor,
                 pillar.get("volume", 0), pillar.get("difficulty", 0), coh),
            )
            inserted += 1

            for sk in pillar.get("supporting_keywords", []):
                coh = _coherence_score(sk["keyword"], seed_toks, body.seed_keyword)
                await db.execute(
                    """INSERT INTO client_keywords
                       (client_domain_id, keyword, keyword_type, pillar_label, pillar_anchor,
                        search_volume, keyword_difficulty, coherence)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (domain_id, sk["keyword"], "supporting", label, anchor,
                     sk.get("search_volume", 0), sk.get("keyword_difficulty", 0), coh),
                )
                inserted += 1

        total_kw = inserted
        await db.execute(
            "UPDATE client_domains SET seed_keyword=?, map_generated=1, total_keywords=?, language=? WHERE id=?",
            (body.seed_keyword, total_kw, body.language, domain_id),
        )
        await db.commit()

    return {
        "domain_id": domain_id,
        "seed_keyword": body.seed_keyword,
        "inserted": inserted,
        "pillars": len(result["pillars"]),
    }


@router.get("/domains/{domain_id}/keywords")
async def list_keywords(domain_id: int, status: Optional[str] = None,
                        keyword_type: Optional[str] = None):
    """List keywords for client domain."""
    await ensure_tables()
    query = "SELECT * FROM client_keywords WHERE client_domain_id=?"
    params = [domain_id]
    if status:
        query += " AND status=?"
        params.append(status)
    if keyword_type:
        query += " AND keyword_type=?"
        params.append(keyword_type)
    query += " ORDER BY keyword_type DESC, search_volume DESC"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


@router.get("/domains/{domain_id}/site-metrics")
async def site_metrics(domain_id: int):
    """Compute SiteFocus / SiteRadius for client domain."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        dom = await _get_domain(db, domain_id)
        if not dom:
            raise HTTPException(404, "Domain not found")

        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT keyword, keyword_type, pillar_label, search_volume, keyword_difficulty,
                      coherence, status
               FROM client_keywords WHERE client_domain_id=?""",
            (domain_id,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    if not rows:
        return {"site_metrics": {}}

    pillars_map = {}
    for kw in rows:
        label = kw.get("pillar_label") or "default"
        if label not in pillars_map:
            pillars_map[label] = {"label": label, "total_volume": 0, "focus_score": 0.0, "supporting_keywords": []}
        pillars_map[label]["total_volume"] += kw.get("search_volume") or 0
        if kw["keyword_type"] == "pillar":
            pillars_map[label]["focus_score"] = kw.get("coherence") or 0.0
        else:
            pillars_map[label]["supporting_keywords"].append(kw)

    for p in pillars_map.values():
        if p["focus_score"] == 0 and p["supporting_keywords"]:
            scores = [s.get("coherence") or 0 for s in p["supporting_keywords"]]
            p["focus_score"] = sum(scores) / len(scores) if scores else 0

    seed = dom.get("seed_keyword") or ""
    metrics = _compute_site_metrics(list(pillars_map.values()), seed, rows)
    return {"site_metrics": metrics}


# ── Tone of Voice ───────────────────────────────────────────────────────────

@router.post("/domains/{domain_id}/generate-tone")
async def generate_tone(domain_id: int):
    """Full AI-generated Tone of Voice pipeline for client domain."""
    await ensure_tables()

    async with aiosqlite.connect(DB_PATH) as db:
        dom = await _get_domain(db, domain_id)
    if not dom:
        raise HTTPException(404, "Domain not found")

    domain_url = dom["domain"]
    seed = dom.get("seed_keyword") or ""
    lang = dom.get("language") or "pl"
    lang_pl = lang == "pl"

    dfs_ok = bool(DFS_LOGIN and DFS_PASSWORD)
    dfs_headers = {}
    if dfs_ok:
        creds = _b64.b64encode(f"{DFS_LOGIN}:{DFS_PASSWORD}".encode()).decode()
        dfs_headers = {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}

    steps_log = []

    # STEP 1: Parse domain content
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
        content_text = f"Domena: {domain_url}, seed: {seed}"
        steps_log.append("Using fallback content")

    # STEP 2: Main keyword
    kw_system = (
        "Analizuj podany content ze strony internetowej i zidentyfikuj główne słowo kluczowe. "
        "Podaj TYLKO jedno słowo kluczowe, bez cudzysłowów, bez wyjaśnień."
    ) if lang_pl else (
        "Analyze the website content and identify the main keyword. "
        "Return ONLY one keyword, no quotes, no explanation."
    )
    main_keyword = await _gpt_call(kw_system, f"Content:\n{content_text[:3000]}", temperature=0.4, max_tokens=50)
    main_keyword = main_keyword.strip().strip('"').strip("'") or seed
    steps_log.append(f"Main keyword: {main_keyword}")

    # STEP 3: Customer profile
    profile_prompt = (
        f"Stwórz profil klienta dla strony {domain_url} (seed: {seed}). Uwzględnij:\n"
        f"1. Przegląd typowego klienta\n2. Demografię\n3. Wartości i postawy\n"
        f"4. Problemy i bolączki\n5. Kluczowe motywacje\n6. Proces decyzyjny\n"
        f"7. Czego szukają na tej stronie\nBądź konkretny i szczegółowy."
    ) if lang_pl else (
        f"Create a customer profile for {domain_url} (seed: {seed}). Include:\n"
        f"1. Typical client overview\n2. Demographics\n3. Values\n"
        f"4. Pain points\n5. Key motivations\n6. Decision process\n"
        f"7. What they seek\nBe specific."
    )
    customer_profile = await _gpt_call(
        "Jesteś ekspertem od marketingu." if lang_pl else "You are a marketing expert.",
        profile_prompt, temperature=0.6, max_tokens=1500,
    )
    steps_log.append(f"Customer profile: {len(customer_profile)} chars")

    # STEP 4-6: SERP + competitors
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

            if serp_urls:
                select_system = (
                    "Wybierz 5 najlepszych URL-i z blogów/portali informacyjnych. "
                    "Pomiń Wikipedię, strony rządowe, sklepy. Zwróć TYLKO URL-e oddzielone przecinkami."
                ) if lang_pl else "Select 5 best blog/news URLs. Skip Wikipedia, govt, shops. Return ONLY URLs comma-separated."
                selected = await _gpt_call(
                    select_system,
                    f"Keyword: {main_keyword}\nURLs:\n" + "\n".join(serp_urls[:15]),
                    temperature=0.2, max_tokens=500,
                )
                comp_urls = [u.strip() for u in selected.split(",") if u.strip().startswith("http")][:5]
                steps_log.append(f"Selected {len(comp_urls)} competitors")

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
                                            comp_text += "\n".join(texts) + "\n"
                            if comp_text:
                                competitor_content.append(comp_text[:2000])
                    except Exception:
                        pass
                steps_log.append(f"Parsed {len(competitor_content)} competitor pages")
        except Exception as e:
            steps_log.append(f"SERP failed: {e}")

    # STEP 7: Mix tones
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

    # STEP 8: Final Tone of Voice
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
        "Create a clear Tone of Voice instruction tailored for a website.\n\n"
        "Include: Tone name, writing guidelines, audience engagement, consistency cues.\n"
        "Write as an instruction for AI writing articles."
    )
    tone_user = (
        f"<tone_candidates>\n{mixed_tones}\n</tone_candidates>\n\n"
        f"<website_content>\n{content_text[:2000]}\n</website_content>\n\n"
        f"<competitor_content>\n{comp_text_combined[:3000]}\n</competitor_content>\n\n"
        f"<customer_profile>\n{customer_profile[:2000]}\n</customer_profile>\n\n"
        f"<about_website>\nDomena: {domain_url}, Seed: {seed}\n</about_website>"
    )
    tone_of_voice = await _gpt_call(tone_system, tone_user, temperature=0.6, max_tokens=2000)
    steps_log.append(f"Tone generated: {len(tone_of_voice)} chars")

    # Save
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE client_domains SET tone_of_voice=? WHERE id=?",
            (tone_of_voice, domain_id),
        )
        await db.commit()

    return {
        "domain_id": domain_id,
        "domain": domain_url,
        "main_keyword": main_keyword,
        "tone_of_voice": tone_of_voice,
        "steps": steps_log,
    }


@router.get("/domains/{domain_id}/tone")
async def get_tone(domain_id: int):
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        dom = await _get_domain(db, domain_id)
    if not dom:
        raise HTTPException(404, "Domain not found")
    tone = (dom.get("tone_of_voice") or "").strip()
    return {"tone_of_voice": tone, "is_generated": bool(tone)}


# ── Article Generation ──────────────────────────────────────────────────────

@router.post("/domains/{domain_id}/generate-articles")
async def generate_articles(domain_id: int, body: ArticleGenerateRequest):
    """Generate articles for selected keywords."""
    await ensure_tables()

    async with aiosqlite.connect(DB_PATH) as db:
        dom = await _get_domain(db, domain_id)
    if not dom:
        raise HTTPException(404, "Domain not found")

    domain_url = dom["domain"]
    tone = (dom.get("tone_of_voice") or "").strip()
    lang = body.language or dom.get("language") or "pl"

    # Build effective prompt with tone
    effective_prompt = ""
    if tone:
        effective_prompt = tone
    if body.custom_prompt:
        effective_prompt = f"{effective_prompt}\n\n{body.custom_prompt}".strip()

    # Fetch keywords
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" for _ in body.keyword_ids)
        async with db.execute(
            f"SELECT * FROM client_keywords WHERE id IN ({placeholders}) AND client_domain_id=?",
            (*body.keyword_ids, domain_id),
        ) as cur:
            keywords = [dict(r) for r in await cur.fetchall()]

    if not keywords:
        raise HTTPException(400, "Brak fraz do generowania")

    # Fetch existing articles for interlinking
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT keyword, title FROM client_articles WHERE client_domain_id=? AND status='draft'",
            (domain_id,),
        ) as cur:
            existing = [dict(r) for r in await cur.fetchall()]

    results = []
    location_code = 2616 if lang == "pl" else 2840

    for kw in keywords:
        try:
            article = await generate_article(
                topic=kw["keyword"],
                client_domain="",
                anchor_text="",
                language=lang,
                custom_prompt=effective_prompt,
                dfs_login=DFS_LOGIN,
                dfs_password=DFS_PASSWORD,
                location_code=location_code,
                pbn_domain=domain_url,
            )

            title = article.get("title", kw["keyword"])
            content = article.get("content", "")
            excerpt = article.get("excerpt", "")

            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    """INSERT INTO client_articles
                       (client_domain_id, keyword_id, keyword, title, content, excerpt, status)
                       VALUES (?,?,?,?,?,?,?)""",
                    (domain_id, kw["id"], kw["keyword"], title, content, excerpt, "draft"),
                )
                article_id = cursor.lastrowid
                await db.execute(
                    "UPDATE client_keywords SET status='generated' WHERE id=?", (kw["id"],)
                )
                await db.commit()

            results.append({
                "keyword_id": kw["id"],
                "keyword": kw["keyword"],
                "article_id": article_id,
                "title": title,
                "status": "ok",
            })
        except Exception as e:
            logger.error(f"[ClientContent] Failed generating for '{kw['keyword']}': {e}")
            results.append({
                "keyword_id": kw["id"],
                "keyword": kw["keyword"],
                "error": str(e),
                "status": "error",
            })

    ok = sum(1 for r in results if r["status"] == "ok")
    return {"generated": ok, "failed": len(results) - ok, "results": results}


# ── Articles CRUD ───────────────────────────────────────────────────────────

@router.get("/domains/{domain_id}/articles")
async def list_articles(domain_id: int):
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, keyword_id, keyword, title, excerpt, status, created_at FROM client_articles WHERE client_domain_id=? ORDER BY created_at DESC",
            (domain_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


@router.get("/articles/{article_id}")
async def get_article(article_id: int):
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM client_articles WHERE id=?", (article_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Article not found")
    return dict(row)


@router.delete("/articles/{article_id}")
async def delete_article(article_id: int):
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM client_articles WHERE id=?", (article_id,))
        await db.commit()
    return {"ok": True}


# ── Export ──────────────────────────────────────────────────────────────────

@router.get("/domains/{domain_id}/export-csv")
async def export_csv(domain_id: int):
    """Export articles as CSV."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT keyword, title, content, excerpt, status, created_at FROM client_articles WHERE client_domain_id=? ORDER BY created_at",
            (domain_id,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        dom = await _get_domain(db, domain_id)

    import csv
    from io import StringIO
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Keyword", "Title", "Excerpt", "Content (HTML)", "Status", "Created"])
    for r in rows:
        writer.writerow([r["keyword"], r["title"], r["excerpt"], r["content"], r["status"], r["created_at"]])

    domain_name = (dom or {}).get("domain", "export")
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="content_{domain_name}.csv"'},
    )


@router.get("/domains/{domain_id}/export-html")
async def export_html_zip(domain_id: int):
    """Export articles as HTML files in a ZIP."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT keyword, title, content, excerpt, created_at FROM client_articles WHERE client_domain_id=? AND status='draft' ORDER BY created_at",
            (domain_id,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        dom = await _get_domain(db, domain_id)

    if not rows:
        raise HTTPException(400, "Brak artykułów do eksportu")

    domain_name = (dom or {}).get("domain", "export")
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, r in enumerate(rows, 1):
            slug = re.sub(r"[^a-z0-9]+", "-", r["keyword"].lower()).strip("-")[:60]
            html = f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{r['title']}</title>
<style>
body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 800px; margin: 2em auto; padding: 0 1em; line-height: 1.7; color: #1a1a1a; }}
h1 {{ font-size: 1.8em; margin-bottom: 0.3em; }}
.meta {{ color: #666; font-size: 0.9em; margin-bottom: 2em; }}
h2 {{ font-size: 1.3em; margin-top: 1.5em; }}
ul, ol {{ padding-left: 1.5em; }}
</style>
</head>
<body>
<h1>{r['title']}</h1>
<p class="meta">Keyword: {r['keyword']} | {r['created_at']}</p>
{r['content']}
</body>
</html>"""
            zf.writestr(f"{i:02d}-{slug}.html", html)

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="content_{domain_name}.zip"'},
    )

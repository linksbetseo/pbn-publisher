import asyncio
import json as _json
import logging
import os
import random
import time
import uuid as _uuid
import aiosqlite
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from typing import List, Optional
from config import DB_PATH
from services.openai_service import generate_article, generate_image, describe_image_and_generate
from services.gemini_image_service import generate_image_gemini
from services.freepik_service import generate_image_freepik
from services.freepik_generate_service import generate_image_zimage, generate_image_flux
from services.wordpress_service import publish_post
from api.autopilot_helpers import fetch_image as _fetch_image_helper

# In-memory job store for SSE progress (max 200 entries — auto-evict oldest finished)
_publish_jobs: dict = {}
_PUBLISH_JOBS_MAX = 200

logger = logging.getLogger(__name__)
DFS_LOGIN = os.getenv("DATAFORSEO_LOGIN", "")
DFS_PASSWORD = os.getenv("DATAFORSEO_PASSWORD", "")

VARIATION_HINTS_PL = [
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
    "ekologiczne i nowoczesne podejście",
    "porady specjalistów i ekspertów",
]

VARIATION_HINTS_EN = [
    "practical step-by-step guide",
    "comparison of available options",
    "common mistakes and how to avoid them",
    "expert industry perspective",
    "real-world benefits and applications",
    "case studies and examples",
    "latest trends and innovations",
    "beginner's guide",
    "advanced techniques and strategies",
    "key facts and myths debunked",
    "how to choose the best solution",
    "saving time and money",
    "safety tips and what to watch out for",
    "eco-friendly and modern approach",
    "expert tips and professional advice",
]

router = APIRouter(prefix="/api/publish", tags=["publish"])


def _job_init(job_id: str, total: int):
    _publish_jobs[job_id] = {
        "total": total,
        "done": 0,
        "published": 0,
        "failed": 0,
        "results": [],
        "finished": False,
    }


def _job_append(job_id: str, result: dict, status: str):
    if job_id not in _publish_jobs:
        return
    j = _publish_jobs[job_id]
    j["results"].append(result)
    j["done"] += 1
    if status == "published":
        j["published"] += 1
    else:
        j["failed"] += 1


def _job_finish(job_id: str):
    if job_id in _publish_jobs:
        _publish_jobs[job_id]["finished"] = True
        _publish_jobs[job_id]["_finished_at"] = time.time()
    # Evict old finished jobs if over limit
    if len(_publish_jobs) > _PUBLISH_JOBS_MAX:
        finished = [(k, v.get("_finished_at", 0)) for k, v in _publish_jobs.items() if v.get("finished")]
        finished.sort(key=lambda x: x[1])
        for k, _ in finished[:len(_publish_jobs) - _PUBLISH_JOBS_MAX]:
            _publish_jobs.pop(k, None)


class GenerateRequest(BaseModel):
    topic: str
    client_domain: str
    anchor_text: str
    language: str = "pl"
    anchor_text2: str = ""
    anchor_url2: str = ""
    anchor_text3: str = ""
    anchor_url3: str = ""
    custom_prompt: str = ""
    variation_hint: str = ""
    pillar_page_url: str = ""
    pillar_page_anchor: str = ""
    tone_of_voice: str = "ekspert"
    use_serp_scrape: bool = True

    @field_validator("topic")
    @classmethod
    def topic_length(cls, v):
        if len(v) > 200:
            raise ValueError("Topic max 200 characters")
        import re as _re  # local import — re not imported at module level in publish.py
        return _re.sub(r'\s+', ' ', v.strip())

    @field_validator("custom_prompt")
    @classmethod
    def custom_prompt_length(cls, v):
        if len(v) > 2000:
            raise ValueError("Custom prompt max 2000 characters")
        return v

    @field_validator("anchor_url2", "anchor_url3")
    @classmethod
    def anchor_url_safe(cls, v):
        if not v:
            return v
        v = v.strip()
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("anchor URL musi zaczynać się od http:// lub https://")
        return v


class RegenerateImageRequest(BaseModel):
    topic: str
    static_image_b64: Optional[str] = None


class PublishRequest(BaseModel):
    title: str
    content: str
    image_b64: Optional[str] = None
    my_domain_ids: List[int] = []
    client_id: Optional[int] = None
    client_domain: str = ""
    # Params for per-domain unique article generation
    topic: str = ""
    anchor_text: str = ""
    anchor_text2: str = ""
    anchor_url2: str = ""
    anchor_text3: str = ""
    anchor_url3: str = ""
    custom_prompt: str = ""
    language: str = "pl"
    unique_per_domain: bool = True
    batch_tag: str = ""
    image_source: str = "none"  # none | gemini | freepik_stock | freepik_zimage | freepik_flux | dalle
    drip_delay: bool = True  # anti-footprint: random delays between domains

    @field_validator("image_source")
    @classmethod
    def image_source_valid(cls, v):
        allowed = {"none", "gemini", "freepik_stock", "freepik_zimage", "freepik_flux", "dalle"}
        if v not in allowed:
            raise ValueError(f"image_source must be one of: {', '.join(sorted(allowed))}")
        return v

    @field_validator("anchor_url2", "anchor_url3")
    @classmethod
    def anchor_url_safe(cls, v):
        if not v:
            return v
        v = v.strip()
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("anchor URL musi zaczynać się od http:// lub https://")
        return v


def _drip_delay_range(domain_count: int) -> tuple[int, int]:
    """Return (min_seconds, max_seconds) delay range based on number of domains. SEO #79: wider ranges."""
    if domain_count <= 5:
        return (30, 90)
    elif domain_count <= 15:
        return (60, 180)
    else:
        return (90, 300)


@router.get("/check-duplicate")
async def check_duplicate(topic: str, domain_id: int):
    """Check if a keyword/topic has already been published to a specific domain."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Escape LIKE wildcards in user input to prevent LIKE injection
        _escaped = topic.lower()[:50].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        async with db.execute(
            """SELECT id, title, wp_post_url, created_at FROM posts
               WHERE my_domain_id = ? AND LOWER(title) LIKE ? ESCAPE '\\' AND status = 'published'
               ORDER BY created_at DESC LIMIT 5""",
            (domain_id, f"%{_escaped}%"),
        ) as cur:
            rows = await cur.fetchall()
    if rows:
        return {
            "duplicate": True,
            "matches": [{"id": r[0], "title": r[1], "url": r[2], "date": r[3]} for r in rows],
        }
    return {"duplicate": False, "matches": []}


@router.post("/generate")
async def generate_content(body: GenerateRequest):
    # Build custom_prompt incorporating tone_of_voice
    effective_prompt = body.custom_prompt or ""
    tone_map = {
        "ekspert": "Pisz w tonie eksperckim, autorytatywnym.",
        "przyjazny": "Pisz w tonie przyjaznym, doradczym.",
        "formalny": "Pisz w tonie formalnym, biznesowym.",
        "poradnikowy": "Pisz w tonie poradnikowym, krok po kroku.",
    }
    if body.tone_of_voice and body.tone_of_voice in tone_map:
        effective_prompt = f"{tone_map[body.tone_of_voice]} {effective_prompt}".strip()

    article = await generate_article(
        body.topic, body.client_domain, body.anchor_text, body.language,
        anchor_text2=body.anchor_text2, anchor_url2=body.anchor_url2,
        anchor_text3=body.anchor_text3, anchor_url3=body.anchor_url3,
        custom_prompt=effective_prompt,
        variation_hint=body.variation_hint,
        pillar_page_url=body.pillar_page_url,
        pillar_page_anchor=body.pillar_page_anchor,
        dfs_login=DFS_LOGIN if body.use_serp_scrape else "",
        dfs_password=DFS_PASSWORD if body.use_serp_scrape else "",
    )
    image_b64 = None
    image_provider = "none"
    img_prompt = _build_image_prompt(body.topic, article["title"], provider="freepik_flux")
    # AI-only fallback chain — no stock photos
    for _prov, _fn in [
        ("freepik_flux", lambda: generate_image_flux(img_prompt)),
        ("freepik_zimage", lambda: generate_image_zimage(img_prompt)),
        ("gemini", lambda: generate_image_gemini(img_prompt)),
    ]:
        try:
            image_b64 = await _fn()
            image_provider = _prov
            break
        except Exception as e:
            logger.warning(f"[Image] {_prov} failed: {e}")
            continue

    return {
        "title": article["title"],
        "content": article["content"],
        "excerpt": article.get("excerpt", ""),
        "lsi_tags": article.get("lsi_tags", []),
        "category": article.get("category", ""),
        "image_b64": image_b64,
        "image_provider": image_provider,
    }


@router.post("/regenerate-image")
async def regenerate_image(body: RegenerateImageRequest):
    image_b64 = None
    try:
        if body.static_image_b64:
            image_b64 = await describe_image_and_generate(body.static_image_b64, body.topic)
        else:
            image_b64 = await generate_image(f"SEO article illustration for: {body.topic}")
    except Exception as e:
        return {"error": str(e), "image_b64": None}
    return {"image_b64": image_b64}


def _build_image_prompt(topic: str, title: str = "", provider: str = "") -> str:
    """Build a descriptive scene-based image prompt tied to the article topic.

    Creates a concrete visual scene instead of abstract topic description.
    AI image generators produce much better results with specific scene prompts.
    """
    subject = title.strip() if title and title.strip() else topic
    # All AI generators render text poorly — always forbid it
    no_text = (
        "Absolutely NO text, NO letters, NO words, NO numbers, NO captions, NO labels, "
        "NO watermarks, NO logos anywhere in the image. Pure visual only."
    )
    return (
        f"A photorealistic scene that visually represents the concept of: {subject}. "
        f"Show a concrete, specific moment or setting — real objects, real environment, real people if relevant. "
        f"NOT an abstract illustration. Think editorial photography for a professional magazine. "
        f"Natural lighting, shallow depth of field, 16:9 landscape composition. "
        f"{no_text}"
    )


async def _fetch_image(topic: str, image_source: str, title: str = "") -> Optional[str]:
    """Fetch image — delegates to shared autopilot_helpers.fetch_image to avoid duplication."""
    img_prompt = _build_image_prompt(topic, title, provider=image_source)
    img, _ = await _fetch_image_helper(image_source, topic, title, img_prompt)
    return img


@router.post("/post")
async def publish_posts(body: PublishRequest):
    if not body.my_domain_ids:
        return {"results": [], "published": 0, "failed": 0}

    # FIX #59: deduplicate domain IDs (UI can send duplicates via multi-select)
    deduped_ids = list(dict.fromkeys(body.my_domain_ids))

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" * len(deduped_ids))
        async with db.execute(
            f"SELECT * FROM my_domains WHERE id IN ({placeholders}) AND active = 1",
            deduped_ids,
        ) as cursor:
            domains = await cursor.fetchall()

    logger.info(f"[PUBLISH] topic='{body.topic}' unique={body.unique_per_domain} domains={len(domains)}")
    variation_pool = VARIATION_HINTS_PL if body.language == "pl" else VARIATION_HINTS_EN
    used_variations = []
    results = []
    published_count = 0
    failed_count = 0
    # Accumulate DB rows to insert in a single batch at the end
    db_rows = []  # (client_id, client_domain, domain_id, title, content, wp_url, status, batch_tag)

    use_drip = body.drip_delay and len(domains) > 2
    drip_min, drip_max = _drip_delay_range(len(domains))

    for idx, dom in enumerate(domains):
        d = dict(dom)
        try:
            if body.unique_per_domain and body.topic:
                available = [v for v in variation_pool if v not in used_variations]
                if not available:
                    used_variations.clear()
                    available = variation_pool[:]
                variation = random.choice(available)
                used_variations.append(variation)

                # Fetch published posts for this domain (interlinking)
                _published_posts = []
                try:
                    async with aiosqlite.connect(DB_PATH) as _db:
                        async with _db.execute(
                            """SELECT title, COALESCE(NULLIF(keyword,''), title) AS keyword, wp_post_url AS url FROM posts
                               WHERE my_domain_id = ? AND status = 'published' AND wp_post_url != ''
                               ORDER BY created_at DESC LIMIT 30""",
                            (d["id"],)
                        ) as _cur:
                            _published_posts = [{"title": r[0] or "", "keyword": r[1] or "", "url": r[2]} for r in await _cur.fetchall()]
                except Exception as e:
                    logger.warning(f"[PUBLISH] Failed to fetch published posts for interlinking on domain {d['id']}: {e}")

                article = await generate_article(
                    body.topic, body.client_domain, body.anchor_text, body.language,
                    anchor_text2=body.anchor_text2, anchor_url2=body.anchor_url2,
                    anchor_text3=body.anchor_text3, anchor_url3=body.anchor_url3,
                    custom_prompt=body.custom_prompt,
                    variation_hint=variation,
                    dfs_login=DFS_LOGIN,
                    dfs_password=DFS_PASSWORD,
                    published_posts=_published_posts,
                    pbn_domain=d.get("domain", ""),
                )
                title = article["title"]
                content = article["content"]
                excerpt = article.get("excerpt", "")
                lsi_tags = article.get("lsi_tags", [])
                if title.strip().lower() == body.topic.strip().lower():
                    title = f"{title} — {variation}"
            else:
                title = body.title
                content = body.content
                excerpt = ""
                lsi_tags = []

            # Determine image to use
            image_b64 = body.image_b64
            if not image_b64 and body.image_source and body.image_source != "none":
                image_b64 = await _fetch_image(body.topic, body.image_source, title)

            # Resolve category name to WP category ID
            _category_name = article.get("category", "") if (body.unique_per_domain and body.topic) else ""
            _category_id = None
            if _category_name:
                try:
                    from services.wordpress_service import get_or_create_category
                    _category_id = await get_or_create_category(
                        domain=d["domain"], wp_login=d["wp_login"], wp_pass=d["wp_pass"],
                        name=_category_name,
                        http_user=d.get("http_user", "") or "",
                        http_pass=d.get("http_pass", "") or "",
                    )
                except Exception:
                    pass

            result = await publish_post(
                domain=d["domain"],
                wp_login=d["wp_login"],
                wp_pass=d["wp_pass"],
                title=title,
                content=content,
                image_b64=image_b64,
                category_id=_category_id,
                excerpt=excerpt,
                keyword=body.topic or None,
                tags=lsi_tags or None,
                http_user=d.get("http_user", "") or "",
                http_pass=d.get("http_pass", "") or "",
            )
            status = "published" if result.get("success") else "failed"
            wp_url = result.get("url", "")
            if status == "published":
                published_count += 1
            else:
                failed_count += 1

            db_rows.append((body.client_id, body.client_domain, d["id"], title, content, wp_url, status, body.batch_tag, body.topic or ""))
            results.append({
                "domain": d["domain"],
                "url": wp_url,
                "status": status,
                "error": result.get("error"),
            })
        except Exception as e:
            failed_count += 1
            db_rows.append((body.client_id, body.client_domain, d["id"], body.title, body.content, "", "failed", body.batch_tag, body.topic or ""))
            results.append({"domain": d["domain"], "url": "", "status": "failed", "error": str(e)})

        # Drip delay between domains (anti-footprint)
        if use_drip and idx < len(domains) - 1:
            delay = random.randint(drip_min, drip_max)
            logger.info(f"[DRIP] Waiting {delay}s before next domain ({idx + 2}/{len(domains)})")
            await asyncio.sleep(delay)

    # Single DB write for all domains — much faster than one connection per domain
    if db_rows:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executemany(
                """INSERT INTO posts (client_id, client_domain, my_domain_id, title, content,
                   wp_post_url, status, batch_tag, keyword) VALUES (?,?,?,?,?,?,?,?,?)""",
                db_rows,
            )
            await db.commit()

    return {"results": results, "published": published_count, "failed": failed_count}


# ── Async publish with SSE progress ──────────────────────────────────────────

_ARTICLE_TIMEOUT = 300  # 5 minutes max per domain (article gen + WP publish)


_PUBLISH_SEM = asyncio.Semaphore(3)  # max 3 concurrent article generations


async def _process_one_domain(
    d: dict,
    body: "PublishRequest",
    variation: str,
    job_id: str,
) -> tuple[dict, tuple]:
    """Generate article + publish to one domain. Returns (sse_item, db_row)."""
    try:
        if body.unique_per_domain and body.topic:
            # Fetch published posts for interlinking
            _published_posts = []
            try:
                async with aiosqlite.connect(DB_PATH) as _db:
                    async with _db.execute(
                        """SELECT title, COALESCE(NULLIF(keyword,''), title) AS keyword, wp_post_url AS url FROM posts
                           WHERE my_domain_id = ? AND status = 'published' AND wp_post_url != ''
                           ORDER BY created_at DESC LIMIT 30""",
                        (d["id"],)
                    ) as _cur:
                        _published_posts = [{"title": r[0] or "", "keyword": r[1] or "", "url": r[2]} for r in await _cur.fetchall()]
            except Exception as e:
                logger.warning(f"[PUBLISH-ASYNC] Failed to fetch published posts for domain {d['id']}: {e}")

            async with _PUBLISH_SEM:
                article = await asyncio.wait_for(
                    generate_article(
                        body.topic, body.client_domain, body.anchor_text, body.language,
                        anchor_text2=body.anchor_text2, anchor_url2=body.anchor_url2,
                        anchor_text3=body.anchor_text3, anchor_url3=body.anchor_url3,
                        custom_prompt=body.custom_prompt,
                        variation_hint=variation,
                        dfs_login=DFS_LOGIN,
                        dfs_password=DFS_PASSWORD,
                        published_posts=_published_posts,
                        pbn_domain=d.get("domain", ""),
                    ),
                    timeout=_ARTICLE_TIMEOUT,
                )
            title = article["title"]
            content = article["content"]
            excerpt = article.get("excerpt", "")
            lsi_tags = article.get("lsi_tags", [])
            if title.strip().lower() == body.topic.strip().lower():
                title = f"{title} — {variation}"
        else:
            title = body.title
            content = body.content
            excerpt = ""
            lsi_tags = []

        image_b64 = body.image_b64
        if not image_b64 and body.image_source and body.image_source != "none":
            image_b64 = await _fetch_image(body.topic, body.image_source, title)

        # Resolve category name to WP category ID
        _category_name = article.get("category", "") if (body.unique_per_domain and body.topic) else ""
        _category_id = None
        if _category_name:
            try:
                from services.wordpress_service import get_or_create_category
                _category_id = await get_or_create_category(
                    domain=d["domain"], wp_login=d["wp_login"], wp_pass=d["wp_pass"],
                    name=_category_name,
                    http_user=d.get("http_user", "") or "",
                    http_pass=d.get("http_pass", "") or "",
                )
            except Exception:
                pass

        result = await publish_post(
            domain=d["domain"],
            wp_login=d["wp_login"],
            wp_pass=d["wp_pass"],
            title=title,
            content=content,
            image_b64=image_b64,
            category_id=_category_id,
            excerpt=excerpt,
            keyword=body.topic or None,
            tags=lsi_tags or None,
            http_user=d.get("http_user", "") or "",
            http_pass=d.get("http_pass", "") or "",
        )
        status = "published" if result.get("success") else "failed"
        wp_url = result.get("url", "")
        item = {"domain": d["domain"], "url": wp_url, "status": status, "error": result.get("error")}
        db_row = (body.client_id, body.client_domain, d["id"], title, content, wp_url, status, body.batch_tag, body.topic or "")
        return item, db_row
    except asyncio.TimeoutError:
        item = {"domain": d["domain"], "url": "", "status": "failed", "error": "Timeout (>5 min)"}
        db_row = (body.client_id, body.client_domain, d["id"], body.title, body.content, "", "failed", body.batch_tag, body.topic or "")
        return item, db_row
    except Exception as e:
        item = {"domain": d["domain"], "url": "", "status": "failed", "error": str(e)}
        db_row = (body.client_id, body.client_domain, d["id"], body.title, body.content, "", "failed", body.batch_tag, body.topic or "")
        return item, db_row


async def _run_publish_job(job_id: str, body: "PublishRequest", domains: list):
    """Background task: publish to all domains in parallel (max 3 concurrent via semaphore)."""
    variation_pool = VARIATION_HINTS_PL if body.language == "pl" else VARIATION_HINTS_EN
    # Pre-assign unique variations to each domain
    variations = []
    used: list = []
    for _ in domains:
        available = [v for v in variation_pool if v not in used]
        if not available:
            used.clear()
            available = variation_pool[:]
        v = random.choice(available)
        used.append(v)
        variations.append(v)

    db_rows = []

    async def _run_one(d, variation):
        if job_id not in _publish_jobs:
            return
        item, db_row = await _process_one_domain(d, body, variation, job_id)
        _job_append(job_id, item, item["status"])
        db_rows.append(db_row)

    await asyncio.gather(*[_run_one(d, v) for d, v in zip(domains, variations)])

    if db_rows:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executemany(
                """INSERT INTO posts (client_id, client_domain, my_domain_id, title, content,
                   wp_post_url, status, batch_tag, keyword) VALUES (?,?,?,?,?,?,?,?,?)""",
                db_rows,
            )
            await db.commit()

    _job_finish(job_id)

    # Send Telegram notification with publish summary (respects user preference)
    try:
        from api.notifications import should_notify
        if await should_notify("notify_bulk_publish_done"):
            from services.telegram_service import send_telegram
            job = _publish_jobs.get(job_id, {})
            pub_count = job.get("published", 0)
            fail_count = job.get("failed", 0)
            total = job.get("total", 0)
            topic_label = body.topic[:60] if body.topic else "manual"
            msg = (
                f"<b>PBN Publish done</b>\n\n"
                f"Topic: {topic_label}\n"
                f"Published: <b>{pub_count}/{total}</b>\n"
            )
            if fail_count:
                msg += f"Failed: <b>{fail_count}</b>\n"
            await send_telegram(msg)
    except Exception:
        pass


@router.post("/post-async")
async def publish_posts_async(body: "PublishRequest", background_tasks: BackgroundTasks):
    """Start async bulk publish job. Returns job_id. Poll /post-progress/{job_id} via SSE."""
    if not body.my_domain_ids:
        return {"job_id": None, "error": "No domains"}

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" * len(body.my_domain_ids))
        async with db.execute(
            f"SELECT * FROM my_domains WHERE id IN ({placeholders}) AND active = 1",
            body.my_domain_ids,
        ) as cursor:
            domains = [dict(r) for r in await cursor.fetchall()]

    job_id = str(_uuid.uuid4())
    _job_init(job_id, len(domains))
    background_tasks.add_task(_run_publish_job, job_id, body, domains)
    return {"job_id": job_id, "total": len(domains)}


@router.get("/job-status/{job_id}")
async def publish_job_status(job_id: str):
    """Simple HTTP poll endpoint — returns current job state. Proxy-safe alternative to SSE."""
    job = _publish_jobs.get(job_id)
    if job is None:
        return {"error": "Job not found", "finished": True, "done": 0, "total": 0, "results": []}
    return {
        "done": job["done"],
        "total": job["total"],
        "published": job["published"],
        "failed": job["failed"],
        "finished": job["finished"],
        "results": job["results"],
        "new_results_since": 0,  # client tracks its own offset
    }


@router.get("/post-progress/{job_id}")
async def publish_progress_sse(job_id: str, request: Request):
    """SSE stream: emits progress events until publish job is done."""
    async def event_generator():
        last_done = -1
        last_results_len = 0
        _sse_start = time.time()
        _SSE_MAX_DURATION = 1800  # 30 minutes
        _last_ping = time.time()
        # Initial padding to force proxy buffer flush (Railway/nginx buffer ~4KB)
        yield f": {' ' * 2048}\n\n"
        yield f"retry: 3000\n\n"
        while True:
            # Detect client disconnect
            if await request.is_disconnected():
                if job_id in _publish_jobs:
                    _publish_jobs[job_id]["_client_gone"] = True
                return

            # Timeout guard
            if time.time() - _sse_start > _SSE_MAX_DURATION:
                yield f"data: {_json.dumps({'error': 'SSE timeout (30min)'})}\n\n"
                return

            job = _publish_jobs.get(job_id)
            if job is None:
                yield f"data: {_json.dumps({'error': 'Job not found'})}\n\n"
                return

            drip_wait = job.get("drip_wait")
            results_len = len(job["results"])
            # Send event when new result arrives OR done count changes OR drip_wait
            if job["done"] != last_done or results_len != last_results_len or drip_wait:
                last_done = job["done"]
                last_results_len = results_len
                _last_ping = time.time()
                latest = job["results"][-1] if job["results"] else None
                payload = {
                    "done": job["done"],
                    "total": job["total"],
                    "published": job["published"],
                    "failed": job["failed"],
                    "latest": latest,
                    "finished": job["finished"],
                }
                if drip_wait:
                    payload["drip_wait"] = drip_wait
                yield f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n"
            elif time.time() - _last_ping >= 15:
                # Keepalive ping every 15s to prevent proxy from closing idle connection
                _last_ping = time.time()
                yield f": ping\n\n"

            if job["finished"]:
                await asyncio.sleep(2)
                _publish_jobs.pop(job_id, None)
                return
            await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── #12 Sitemap ping after publication ────────────────────────────────────────

@router.post("/ping-sitemap")
async def ping_sitemap(body: dict):
    """Ping Bing with sitemap URL after publication."""
    import httpx
    domain = body.get("domain", "")
    if not domain:
        return {"error": "No domain"}
    # FIX #60: strip trailing slash from domain before building sitemap URL
    domain = domain.rstrip("/")
    sitemap_url = f"https://{domain}/sitemap.xml" if not domain.startswith("http") else f"{domain}/sitemap.xml"
    results = {}
    async with httpx.AsyncClient(timeout=10, verify=False) as client:
        for name, url in [
            # Google sitemap ping deprecated 2023 — removed
            ("bing", f"https://www.bing.com/ping?sitemap={sitemap_url}"),
        ]:
            try:
                r = await client.get(url)
                results[name] = {"status": r.status_code, "ok": r.status_code < 400}
            except Exception as e:
                results[name] = {"status": 0, "ok": False, "error": str(e)}
    return {"domain": domain, "sitemap": sitemap_url, "results": results}


# ── #11 Interlinking — get other published posts on same domain ───────────────

@router.get("/domain-posts/{domain_id}")
async def get_domain_posts(domain_id: int, limit: int = 20):
    """Return published posts on a domain for interlinking suggestions."""
    limit = min(max(1, limit), 200)  # clamp 1–200
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, title, wp_post_url, created_at
               FROM posts WHERE my_domain_id = ? AND status = 'published' AND wp_post_url != ''
               ORDER BY created_at DESC LIMIT ?""",
            (domain_id, limit),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]

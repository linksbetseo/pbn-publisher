import asyncio
import json as _json
import logging
import os
import random
import uuid as _uuid
import aiosqlite
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional
from config import DB_PATH
from services.openai_service import generate_article, generate_image, describe_image_and_generate
from services.gemini_image_service import generate_image_gemini
from services.freepik_service import generate_image_freepik
from services.freepik_generate_service import generate_image_zimage, generate_image_flux
from services.wordpress_service import publish_post

# In-memory job store for SSE progress
_publish_jobs: dict = {}

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


def _drip_delay_range(domain_count: int) -> tuple[int, int]:
    """Return (min_seconds, max_seconds) delay range based on number of domains."""
    if domain_count <= 5:
        return (20, 60)
    elif domain_count <= 15:
        return (45, 120)
    else:
        return (60, 180)


@router.get("/check-duplicate")
async def check_duplicate(topic: str, domain_id: int):
    """Check if a keyword/topic has already been published to a specific domain."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT id, title, wp_post_url, created_at FROM posts
               WHERE my_domain_id = ? AND LOWER(title) LIKE ? AND status = 'published'
               ORDER BY created_at DESC LIMIT 5""",
            (domain_id, f"%{topic.lower()[:50]}%"),
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
    img_prompt = (
        f"High-quality professional photo for an article titled '{article['title']}'. "
        f"Topic: {body.topic}. Realistic scene, natural lighting, no text overlays, no watermarks, "
        f"16:9 aspect, clean modern aesthetic."
    )
    try:
        image_b64 = await generate_image_gemini(img_prompt)
        image_provider = "gemini"
    except Exception:
        try:
            image_b64 = await generate_image_freepik(body.topic)
            image_provider = "freepik"
        except Exception:
            try:
                image_b64 = await generate_image(f"Professional photo for blog post about: {body.topic}. Clean, no text.")
                image_provider = "dalle"
            except Exception as e:
                logger.warning(f"All image providers failed: {e}")

    return {
        "title": article["title"],
        "content": article["content"],
        "excerpt": article.get("excerpt", ""),
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


def _build_image_prompt(topic: str, title: str = "") -> str:
    """Build a descriptive image prompt tied to the article topic."""
    subject = title if title and title.strip() else topic
    return (
        f"High-quality professional photograph directly related to: {subject}. "
        f"The image must clearly illustrate the topic '{topic}'. "
        f"Realistic scene, natural lighting, sharp focus, no text overlays, no watermarks, "
        f"no logos, 16:9 landscape format, clean modern aesthetic. "
        f"Do NOT show random objects unrelated to the topic."
    )


async def _fetch_image(topic: str, image_source: str, title: str = "") -> Optional[str]:
    """Fetch image based on image_source setting. Tries primary provider, then freepik_stock fallback."""
    if image_source == "none":
        return None
    img_prompt = _build_image_prompt(topic, title)

    # Provider chains: primary → fallback(s)
    _chains = {
        "freepik_zimage": [
            ("freepik_zimage", lambda: generate_image_zimage(img_prompt)),
            ("freepik_stock", lambda: generate_image_freepik(topic)),
        ],
        "freepik_flux": [
            ("freepik_flux", lambda: generate_image_flux(img_prompt)),
            ("freepik_stock", lambda: generate_image_freepik(topic)),
        ],
        "freepik_stock": [
            ("freepik_stock", lambda: generate_image_freepik(topic)),
        ],
        "dalle": [
            ("dalle", lambda: generate_image(f"Professional illustration for article about: {topic}. Clean, no text, no watermarks.")),
            ("freepik_stock", lambda: generate_image_freepik(topic)),
        ],
        "gemini": [
            ("gemini", lambda: generate_image_gemini(img_prompt)),
            ("freepik_stock", lambda: generate_image_freepik(topic)),
        ],
    }
    chain = _chains.get(image_source, _chains.get("freepik_stock", []))

    for provider_name, provider_fn in chain:
        try:
            return await provider_fn()
        except Exception as e:
            logger.warning(f"[Image] {provider_name} failed for '{topic}': {e}")
    return None


@router.post("/post")
async def publish_posts(body: PublishRequest):
    if not body.my_domain_ids:
        return {"results": [], "published": 0, "failed": 0}

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" * len(body.my_domain_ids))
        async with db.execute(
            f"SELECT * FROM my_domains WHERE id IN ({placeholders}) AND active = 1",
            body.my_domain_ids,
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

            result = await publish_post(
                domain=d["domain"],
                wp_login=d["wp_login"],
                wp_pass=d["wp_pass"],
                title=title,
                content=content,
                image_b64=image_b64,
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

        result = await publish_post(
            domain=d["domain"],
            wp_login=d["wp_login"],
            wp_pass=d["wp_pass"],
            title=title,
            content=content,
            image_b64=image_b64,
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
    """Background task: publish to all domains sequentially with drip delays (anti-footprint)."""
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

    use_drip = body.drip_delay and len(domains) > 2
    drip_min, drip_max = _drip_delay_range(len(domains))
    db_rows = []

    for idx, (d, variation) in enumerate(zip(domains, variations)):
        if job_id not in _publish_jobs:
            break

        item, db_row = await _process_one_domain(d, body, variation, job_id)
        _job_append(job_id, item, item["status"])
        db_rows.append(db_row)

        # Drip delay between domains (anti-footprint)
        if use_drip and idx < len(domains) - 1:
            delay = random.randint(drip_min, drip_max)
            logger.info(f"[DRIP] job={job_id[:8]} waiting {delay}s before domain {idx + 2}/{len(domains)}")
            # Store drip info so SSE can report it
            if job_id in _publish_jobs:
                _publish_jobs[job_id]["drip_wait"] = delay
            await asyncio.sleep(delay)
            if job_id in _publish_jobs:
                _publish_jobs[job_id].pop("drip_wait", None)

    if db_rows:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executemany(
                """INSERT INTO posts (client_id, client_domain, my_domain_id, title, content,
                   wp_post_url, status, batch_tag, keyword) VALUES (?,?,?,?,?,?,?,?,?)""",
                db_rows,
            )
            await db.commit()

    _job_finish(job_id)

    # Send Telegram notification with publish summary
    try:
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


@router.get("/post-progress/{job_id}")
async def publish_progress_sse(job_id: str, request: Request):
    """SSE stream: emits progress events until publish job is done."""
    async def event_generator():
        last_done = -1
        while True:
            # Detect client disconnect — prevents memory leak from orphaned jobs
            if await request.is_disconnected():
                # Mark job for cleanup but let background task finish saving to DB
                if job_id in _publish_jobs:
                    _publish_jobs[job_id]["_client_gone"] = True
                return

            job = _publish_jobs.get(job_id)
            if job is None:
                yield f"data: {_json.dumps({'error': 'Job not found'})}\n\n"
                return
            drip_wait = job.get("drip_wait")
            if job["done"] != last_done or drip_wait:
                last_done = job["done"]
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
            if job["finished"]:
                # Clean up after a short delay to allow client to read final event
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
    """Ping Google & Bing with sitemap URL after publication."""
    import httpx
    domain = body.get("domain", "")
    if not domain:
        return {"error": "No domain"}
    sitemap_url = f"https://{domain}/sitemap.xml" if not domain.startswith("http") else f"{domain}/sitemap.xml"
    results = {}
    async with httpx.AsyncClient(timeout=10, verify=False) as client:
        for name, url in [
            ("google", f"https://www.google.com/ping?sitemap={sitemap_url}"),
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

import json
import asyncio
import random
import aiosqlite
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional
from config import DB_PATH
from services.openai_service import generate_article, generate_image, describe_image_and_generate
from services.wordpress_service import publish_post

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


@router.post("/generate")
async def generate_content(body: GenerateRequest):
    article = await generate_article(
        body.topic, body.client_domain, body.anchor_text, body.language,
        anchor_text2=body.anchor_text2, anchor_url2=body.anchor_url2,
        anchor_text3=body.anchor_text3, anchor_url3=body.anchor_url3,
        custom_prompt=body.custom_prompt,
    )
    image_b64 = None
    image_prompt = f"SEO article illustration for: {body.topic}"
    try:
        image_b64 = await generate_image(image_prompt)
    except Exception as e:
        print(f"Image generation failed: {e}")

    return {
        "title": article["title"],
        "content": article["content"],
        "image_b64": image_b64,
        "image_prompt": image_prompt,
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


@router.post("/post")
async def publish_posts(body: PublishRequest):
    if not body.my_domain_ids:
        return []

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" * len(body.my_domain_ids))
        async with db.execute(
            f"SELECT * FROM my_domains WHERE id IN ({placeholders}) AND active = 1",
            body.my_domain_ids,
        ) as cursor:
            domains = await cursor.fetchall()

    variation_pool = VARIATION_HINTS_PL if body.language == "pl" else VARIATION_HINTS_EN
    used_variations = []

    async def generate_events():
        results = []
        for dom in domains:
            d = dict(dom)
            try:
                # Generate unique article per domain if topic provided
                if body.unique_per_domain and body.topic:
                    available = [v for v in variation_pool if v not in used_variations]
                    if not available:
                        used_variations.clear()
                        available = variation_pool[:]
                    variation = random.choice(available)
                    used_variations.append(variation)

                    article = await generate_article(
                        body.topic, body.client_domain, body.anchor_text, body.language,
                        anchor_text2=body.anchor_text2, anchor_url2=body.anchor_url2,
                        anchor_text3=body.anchor_text3, anchor_url3=body.anchor_url3,
                        custom_prompt=body.custom_prompt,
                        variation_hint=variation,
                    )
                    title = article["title"]
                    content = article["content"]
                else:
                    title = body.title
                    content = body.content

                result = await publish_post(
                    domain=d["domain"],
                    wp_login=d["wp_login"],
                    wp_pass=d["wp_pass"],
                    title=title,
                    content=content,
                    image_b64=body.image_b64,
                )
                status = "published" if result.get("success") else "failed"
                wp_url = result.get("url", "")

                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        """INSERT INTO posts (client_id, client_domain, my_domain_id, title, content,
                           image_url, wp_post_url, status) VALUES (?,?,?,?,?,?,?,?)""",
                        (
                            body.client_id,
                            body.client_domain,
                            d["id"],
                            title,
                            content,
                            wp_url,
                            wp_url,
                            status,
                        ),
                    )
                    await db.commit()

                item = {
                    "domain": d["domain"],
                    "url": wp_url,
                    "status": status,
                    "error": result.get("error"),
                }
            except Exception as e:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        """INSERT INTO posts (client_id, client_domain, my_domain_id, title, content,
                           status) VALUES (?,?,?,?,?,?)""",
                        (body.client_id, body.client_domain, d["id"],
                         body.title, body.content, "failed"),
                    )
                    await db.commit()

                item = {
                    "domain": d["domain"],
                    "url": "",
                    "status": "failed",
                    "error": str(e),
                }

            results.append(item)
            yield f"data: {json.dumps(item)}\n\n"
            await asyncio.sleep(0.05)

        yield f"data: {json.dumps({'done': True, 'total': len(results)})}\n\n"

    return StreamingResponse(
        generate_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

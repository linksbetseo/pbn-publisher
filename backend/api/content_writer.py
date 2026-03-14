"""
Content Writer API — SEO article generation with SERP top10 analysis.
"""
import asyncio
import time as _time
import uuid
import aiosqlite
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from config import DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD, DB_PATH
from services.content_writer_service import generate_seo_article

router = APIRouter(prefix="/api/content-writer", tags=["content-writer"])

DFS_LOGIN = DATAFORSEO_LOGIN
DFS_PASSWORD = DATAFORSEO_PASSWORD

# Limit concurrent article generation to avoid overloading OpenAI / DataForSEO
_generate_sem = asyncio.Semaphore(3)

# In-memory progress tracker for article generation tasks
_progress: dict[str, dict] = {}


class ContentWriterRequest(BaseModel):
    keyword: str
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
    supporting_page_urls: List[str] = []
    tone_of_voice: str = "ekspert"
    use_serp_scrape: bool = True


@router.post("/generate")
async def generate_content(req: ContentWriterRequest):
    """Generate SEO article based on keyword + SERP top10 analysis."""
    task_id = str(uuid.uuid4())[:8]

    # Clean stale progress entries (older than 10 minutes)
    _now = _time.time()
    stale = [k for k, v in _progress.items() if v.get("_ts", 0) < _now - 600]
    for k in stale:
        _progress.pop(k, None)

    _progress[task_id] = {"step": 0, "phase": "init", "label": "Rozpoczynam...", "total_steps": 5, "done": False, "_ts": _now}

    def on_step(step_num, phase, label):
        _progress[task_id] = {"step": step_num, "phase": phase, "label": label, "total_steps": 5, "done": phase == "done", "_ts": _time.time()}

    try:
        async with _generate_sem:
            result = await generate_seo_article(
                keyword=req.keyword,
                client_domain=req.client_domain,
                anchor_text=req.anchor_text,
                language=req.language,
                anchor_text2=req.anchor_text2,
                anchor_url2=req.anchor_url2,
                anchor_text3=req.anchor_text3,
                anchor_url3=req.anchor_url3,
                custom_prompt=req.custom_prompt,
                variation_hint=req.variation_hint,
                pillar_page_url=req.pillar_page_url,
                pillar_page_anchor=req.pillar_page_anchor,
                supporting_page_urls=req.supporting_page_urls,
                tone_of_voice=req.tone_of_voice,
                dfs_login=DFS_LOGIN,
                dfs_password=DFS_PASSWORD,
                use_serp_scrape=req.use_serp_scrape,
                on_step=on_step,
            )
    except Exception as exc:
        _progress[task_id] = {"step": 5, "phase": "error", "label": f"Błąd: {str(exc)[:100]}", "total_steps": 5, "done": True, "error": str(exc)[:200], "_ts": _time.time()}
        raise
    finally:
        # Clean up progress after a delay
        async def _cleanup():
            await asyncio.sleep(60)
            _progress.pop(task_id, None)
        asyncio.create_task(_cleanup())

    result["task_id"] = task_id
    return result


@router.get("/progress/{task_id}")
async def get_progress(task_id: str):
    """Poll generation progress for a task."""
    info = _progress.get(task_id)
    if not info:
        return {"step": 5, "phase": "done", "label": "Gotowe!", "total_steps": 5, "done": True}
    return info


class SaveArticleRequest(BaseModel):
    keyword: str
    title: str
    content: str
    client_domain: str = ""
    meta_description: str = ""
    my_domain_id: Optional[int] = None


@router.post("/save")
async def save_article(req: SaveArticleRequest):
    """Save generated article to posts history table."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO posts (client_id, client_domain, my_domain_id, title, content, status, keyword)
               VALUES (?,?,?,?,?,?,?)""",
            (None, req.client_domain, req.my_domain_id, req.title, req.content, "draft", getattr(req, 'keyword', '') or "")
        )
        await db.commit()
        async with db.execute("SELECT last_insert_rowid()") as cur:
            post_id = (await cur.fetchone())[0]
    return {"id": post_id, "status": "draft", "message": "Artykuł zapisany w historii"}


# ── #9 SERP cache info ──────────────────────────────────────────────────────

@router.get("/serp-cache-info")
async def serp_cache_info(keyword: str):
    """Check if SERP data for keyword is cached and when it expires."""
    import hashlib
    import time as _time
    cache_key = hashlib.sha256(keyword.strip().lower().encode()).hexdigest()[:32]
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT expires_at FROM serp_cache WHERE cache_key=?", (cache_key,)
            ) as cur:
                row = await cur.fetchone()
        if row:
            expires_at = row[0]
            now = _time.time()
            remaining = max(0, expires_at - now)
            return {
                "cached": remaining > 0,
                "expires_in_hours": round(remaining / 3600, 1),
                "expires_in_minutes": round(remaining / 60),
            }
    except Exception:
        pass
    return {"cached": False, "expires_in_hours": 0, "expires_in_minutes": 0}


# ── #14 Send article to autopilot ───────────────────────────────────────────

class SendToAutopilotRequest(BaseModel):
    keyword: str
    title: str
    content: str
    my_domain_id: int
    client_domain: str = ""
    anchor_text: str = ""


@router.post("/send-to-autopilot")
async def send_to_autopilot(req: SendToAutopilotRequest):
    """Save generated article as draft post on selected domain and add keyword to autopilot queue."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Find autopilot schedule for the domain
        async with db.execute(
            "SELECT id FROM domain_schedules WHERE my_domain_id = ? LIMIT 1",
            (req.my_domain_id,)
        ) as cur:
            sched = await cur.fetchone()
        schedule_id = None
        if sched:
            schedule_id = sched[0]
            # Add keyword to autopilot queue
            await db.execute(
                """INSERT INTO domain_keywords (schedule_id, my_domain_id, keyword, keyword_type, status, search_volume, title)
                   VALUES (?, ?, ?, 'supporting', 'pending', 0, ?)""",
                (schedule_id, req.my_domain_id, req.keyword, req.title)
            )
        # Always save article as draft in posts
        await db.execute(
            """INSERT INTO posts (client_id, client_domain, my_domain_id, title, content, status, keyword)
               VALUES (?, ?, ?, ?, ?, 'draft', ?)""",
            (None, req.client_domain, req.my_domain_id, req.title, req.content, req.keyword or "")
        )
        await db.commit()
    msg = f"Artykuł zapisany jako draft na domenie #{req.my_domain_id}"
    if schedule_id:
        msg += f" + dodany do kolejki autopilota (schedule #{schedule_id})"
    else:
        msg += " (brak harmonogramu autopilota — tylko draft)"
    return {"ok": True, "schedule_id": schedule_id, "message": msg}

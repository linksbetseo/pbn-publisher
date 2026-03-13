"""
Autopilot helper functions — extracted from autopilot.py to reduce file size.
Contains: job management, image fetching, retry logic, pillar URL lookup.
"""
import asyncio
import json as _json
import logging
import random
from datetime import datetime
from typing import Optional

import aiosqlite
from config import DB_PATH

logger = logging.getLogger(__name__)

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


async def job_create(job_id: str, schedule_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO run_jobs (job_id, schedule_id, status, published, failed, total, results_json, done) VALUES (?,?,?,?,?,?,?,?)",
            (job_id, schedule_id, 'running', 0, 0, 0, '[]', 0)
        )
        await db.commit()


async def job_update(job_id: str, **kwargs):
    if 'results' in kwargs:
        kwargs['results_json'] = _json.dumps(kwargs.pop('results'), ensure_ascii=False)
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [job_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE run_jobs SET {sets} WHERE job_id=?", vals)
        await db.commit()


async def job_get(job_id: str) -> Optional[dict]:
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


async def with_retry(coro_fn, max_attempts: int = 3, base_delay: float = 2.0):
    """Retry async coroutine with exponential backoff + jitter."""
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return await coro_fn()
        except Exception as e:
            last_exc = e
            if attempt < max_attempts - 1:
                # FIX #62: add jitter to exponential backoff (prevents thundering herd on shared GPT rate limits)
                wait = base_delay * (2 ** attempt) + random.uniform(0, base_delay)
                logger.warning(f"[Retry] attempt {attempt+1}/{max_attempts} failed: {e} — retrying in {wait:.1f}s")
                await asyncio.sleep(wait)
    raise last_exc


async def get_pillar_url(schedule_id: int, my_domain_id: int, pillar_anchor: str) -> tuple[str, str]:
    """Returns (pillar_wp_url, pillar_keyword) for internal linking."""
    if not pillar_anchor:
        return "", ""
    async with aiosqlite.connect(DB_PATH) as db:
        # FIX #63: also match by pillar_label (some schedules use label not anchor for linking)
        async with db.execute(
            """SELECT wp_post_url, keyword FROM domain_keywords
               WHERE schedule_id=? AND my_domain_id=? AND (pillar_anchor=? OR pillar_label=?)
                 AND keyword_type='pillar' AND status='published' AND wp_post_url!=''
               LIMIT 1""",
            (schedule_id, my_domain_id, pillar_anchor, pillar_anchor)
        ) as cur:
            row = await cur.fetchone()
    if row:
        return row[0], row[1]
    return "", ""


async def fetch_image(image_source: str, keyword: str, title: str, img_prompt: str) -> tuple[str | None, str]:
    """Fetch image based on image_source setting."""
    from services.openai_service import generate_image
    from services.freepik_service import generate_image_freepik
    from services.freepik_generate_service import generate_image_zimage, generate_image_flux

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
            ("dalle", lambda: generate_image(img_prompt)),
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
    return None, "none"

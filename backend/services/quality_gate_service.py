"""
Quality Gate Service
=====================
Runs a CEO scoring loop before autopilot publishes articles for a domain.
CEO = Claude Opus 4.6 (or GPT-4o fallback) scores articles 0-100.
Gate passes when CEO score >= QUALITY_THRESHOLD (default 80).

Flow:
1. quality_gate_check(domain) → True if domain already passed gate
2. quality_gate_run(domain, schedule_id) → iterates until pass or max_iter
3. quality_gate_status(domain) → returns current gate state

Gate state is persisted in `quality_gate_state` table (SQLite).
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import httpx

from config import DB_PATH

logger = logging.getLogger(__name__)

QUALITY_THRESHOLD = int(os.getenv("QUALITY_THRESHOLD", "80"))
QUALITY_MAX_ITER = int(os.getenv("QUALITY_MAX_ITER", "8"))
QUALITY_JOB_TIMEOUT = int(os.getenv("QUALITY_JOB_TIMEOUT", "600"))
QUALITY_POLL_INTERVAL = 15

RAILWAY_URL = os.getenv("RAILWAY_PUBLIC_URL", "https://pbn-publisher-production.up.railway.app")

CEO_SYSTEM = """Jesteś CEO firmy SEO przeprowadzającym audyt jakości treści.
Oceniasz artykuł według 10 kryteriów (każde 0-10 pkt, max 100).

KRYTERIA:
1. CSI — Czy artykuł w pełni odpowiada na zapytanie? (0=nie odpowiada, 10=pełna odpowiedź)
2. Information Density — Gęstość wartościowych informacji (0=ogólniki, 10=każde zdanie wnosi wartość)
3. EAV Richness — Bogactwo encji: marki, badania, instytucje (0=brak, 10=różnorodne nowe encje per sekcja)
4. BLUF Structure — Bezpośrednia odpowiedź w pierwszym zdaniu (0=zakopana, 10=definicja w zdaniu 1)
5. E-E-A-T — Sygnały autorytetu i doświadczenia (0=brak, 10=konkretne badania, obserwacje eksperta)
6. AI Footprints — Ślady AI: "Warto zauważyć", "Nie ulega wątpliwości" (0=pełno śladów, 10=brak)
7. Keyword Stuffing — Naturalne słowa kluczowe (0=20+ powtórzeń, 10=4-7 naturalnych)
8. Duplicate Content — Powtarzające się fakty w sekcjach (0=te same treści, 10=każda sekcja unikalna)
9. Bold Abuse — Pogrubienia (0=chaos, 10=max 8 pogrubień, tylko kluczowe terminy)
10. Hallucination Risk — Ryzyko zmyślonych statystyk (0=konkretne zmyślone %, 10=ogólne lub z SERP)

FORMAT ODPOWIEDZI (ścisły JSON):
```json
{
  "scores": {
    "csi": <0-10>, "information_density": <0-10>, "eav_richness": <0-10>,
    "bluf_structure": <0-10>, "eeat_signals": <0-10>, "ai_footprints": <0-10>,
    "keyword_stuffing": <0-10>, "duplicate_content": <0-10>,
    "bold_abuse": <0-10>, "hallucination_risk": <0-10>
  },
  "total": <0-100>,
  "verdict": "PASS" lub "FAIL",
  "top_issues": ["problem 1", "problem 2", "problem 3"],
  "prompt_fixes": ["konkretna zmiana do promptu 1", "konkretna zmiana 2"]
}
```

verdict = "PASS" tylko gdy total >= 80. Bądź surowy — 80/100 to wysoki standard.
"""


async def _ensure_tables():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS quality_gate_state (
                domain TEXT PRIMARY KEY,
                status TEXT DEFAULT 'pending',
                score INTEGER DEFAULT 0,
                iterations INTEGER DEFAULT 0,
                last_article_url TEXT DEFAULT '',
                last_run_at TEXT DEFAULT '',
                error TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            )
        """)
        await db.commit()


async def quality_gate_check(domain: str) -> bool:
    """Return True if domain has already passed the quality gate (score >= threshold)."""
    await _ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT status, score FROM quality_gate_state WHERE domain = ?",
            (domain,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return False
    return row[0] == "passed" and row[1] >= QUALITY_THRESHOLD


async def quality_gate_get_status(domain: str) -> dict:
    """Return quality gate state for a domain."""
    await _ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT * FROM quality_gate_state WHERE domain = ?",
            (domain,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return {"domain": domain, "status": "pending", "score": 0}
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


async def _update_gate_state(domain: str, **kwargs):
    await _ensure_tables()
    kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [domain]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"INSERT INTO quality_gate_state (domain) VALUES (?) ON CONFLICT(domain) DO NOTHING",
            (domain,)
        )
        await db.execute(
            f"UPDATE quality_gate_state SET {sets} WHERE domain = ?",
            vals
        )
        await db.commit()


async def _trigger_article(schedule_id: int, domain: str) -> Optional[str]:
    """Trigger one article via publish-one endpoint. Returns job_id."""
    from api.autopilot import _run_job, ensure_tables, RunNowRequest
    from api.autopilot_helpers import job_create as _job_create
    import uuid

    await ensure_tables()
    job_id = f"qg-{uuid.uuid4().hex[:8]}"
    await _job_create(job_id, schedule_id)

    # Run in background task
    asyncio.create_task(_run_job(job_id, schedule_id, RunNowRequest(schedule_id=schedule_id, limit=1)))
    return job_id


async def _wait_for_job(job_id: str) -> Optional[str]:
    """Poll job status until done. Returns published URL or None."""
    elapsed = 0
    async with aiosqlite.connect(DB_PATH) as _:
        pass  # ensure DB accessible

    while elapsed < QUALITY_JOB_TIMEOUT:
        await asyncio.sleep(QUALITY_POLL_INTERVAL)
        elapsed += QUALITY_POLL_INTERVAL
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT status, done, results_json, published FROM run_jobs WHERE job_id = ?",
                    (job_id,)
                ) as cur:
                    row = await cur.fetchone()

            if not row:
                continue

            status, done, results_json, published = row
            if done or status in ("done", "error", "completed"):
                try:
                    results = json.loads(results_json or "[]")
                    for r in results:
                        if r.get("status") == "published" and r.get("url"):
                            return r["url"]
                except Exception:
                    pass
                logger.warning(f"[QualityGate] Job {job_id} done but no URL. published={published}")
                return None
        except Exception as e:
            logger.warning(f"[QualityGate] Poll error: {e}")

    logger.warning(f"[QualityGate] Job {job_id} timed out after {QUALITY_JOB_TIMEOUT}s")
    return None


async def _fetch_article_text(url: str) -> str:
    """Fetch article via WP REST API then fallback to scrape."""
    from urllib.parse import urlparse
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("[QualityGate] beautifulsoup4 not installed — install it for text extraction")
        return ""

    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    slug = parsed.path.strip("/").split("/")[-1]

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        # WP REST API first
        try:
            api_url = f"{base}/wp-json/wp/v2/posts?slug={slug}&_fields=title,content"
            resp = await client.get(api_url)
            data = resp.json()
            if isinstance(data, list) and data:
                rendered = data[0].get("content", {}).get("rendered", "")
                if rendered:
                    soup = BeautifulSoup(rendered, "html.parser")
                    text = soup.get_text(separator="\n", strip=True)
                    if len(text.split()) > 100:
                        return text
        except Exception:
            pass

        # Fallback scrape
        try:
            resp = await client.get(url)
            soup = BeautifulSoup(resp.text, "html.parser")
            article = (
                soup.find("article") or
                soup.find(class_=re.compile(r"entry-content|post-content|article-content")) or
                soup.find("main")
            )
            if article:
                for el in article.find_all(["nav", "aside", "footer", "script", "style"]):
                    el.decompose()
                text = article.get_text(separator="\n", strip=True)
                if len(text.split()) > 50:
                    return text
        except Exception as e:
            logger.warning(f"[QualityGate] Fetch failed: {e}")

    return ""


def _score_with_ceo(article_text: str) -> dict:
    """Score article with Claude Opus or GPT-4o fallback. Synchronous."""
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    raw = None

    if anthropic_key:
        try:
            import anthropic
            ceo = anthropic.Anthropic(api_key=anthropic_key)
            msg = ceo.messages.create(
                model="claude-opus-4-6",
                max_tokens=1500,
                system=CEO_SYSTEM,
                messages=[{"role": "user", "content": f"Oceń artykuł:\n\n{article_text[:7000]}"}]
            )
            raw = msg.content[0].text
            logger.info("[QualityGate] CEO scored via claude-opus-4-6")
        except Exception as e:
            logger.warning(f"[QualityGate] Anthropic error: {e}")

    if not raw and openai_key:
        try:
            from openai import OpenAI
            oai = OpenAI(api_key=openai_key)
            resp = oai.chat.completions.create(
                model="gpt-4o",
                max_tokens=1500,
                messages=[
                    {"role": "system", "content": CEO_SYSTEM},
                    {"role": "user", "content": f"Oceń artykuł:\n\n{article_text[:7000]}"}
                ]
            )
            raw = resp.choices[0].message.content
            logger.info("[QualityGate] CEO scored via gpt-4o (fallback)")
        except Exception as e:
            logger.warning(f"[QualityGate] OpenAI fallback error: {e}")

    if not raw:
        logger.error("[QualityGate] No CEO model available — set ANTHROPIC_API_KEY or OPENAI_API_KEY")
        return {"total": 0, "verdict": "FAIL", "top_issues": ["No CEO API key"], "prompt_fixes": []}

    json_match = re.search(r'```(?:json)?\s*(\{[\s\S]+?\})\s*```', raw)
    if not json_match:
        json_match = re.search(r'(\{[\s\S]+\})', raw)
    if not json_match:
        return {"total": 0, "verdict": "FAIL", "top_issues": [raw[:200]], "prompt_fixes": []}

    try:
        return json.loads(json_match.group(1))
    except json.JSONDecodeError:
        return {"total": 0, "verdict": "FAIL", "top_issues": ["JSON parse failed"], "prompt_fixes": []}


def _apply_prompt_fixes_with_ai(fixes: list, top_issues: list) -> bool:
    """
    Use Claude Opus (or GPT-4o fallback) to apply prompt fixes to openai_service.py.
    Returns True if changes were applied.
    """
    if not fixes and not top_issues:
        return False

    import pathlib
    service_path = pathlib.Path(__file__).parent / "openai_service.py"
    if not service_path.exists():
        logger.error(f"[QualityGate] openai_service.py not found at {service_path}")
        return False

    current_code = service_path.read_text(encoding="utf-8")
    issue_desc = "\n".join(f"- {i}" for i in top_issues)
    fix_desc = "\n".join(f"- {f}" for f in fixes)

    prompt = f"""Masz za zadanie zmodyfikować prompty GPT w pliku Python.

PROBLEMY W ARTYKULE:
{issue_desc}

POPRAWKI DO WDROŻENIA:
{fix_desc}

INSTRUKCJE:
1. Wygeneruj JSON listę zmian, max 3 zmiany
2. Zmieniaj TYLKO fragmenty promptów/stringów — nie logikę Pythona
3. old_string musi być DOKŁADNY fragment z kodu (case-sensitive)
4. Każda zmiana musi być konkretna i dotyczyć wykrytych problemów

FORMAT:
```json
[
  {{
    "description": "krótki opis",
    "old_string": "dokładny fragment do zastąpienia",
    "new_string": "nowy fragment"
  }}
]
```

KOD (fragment z promptami):
{current_code[38000:70000]}
"""

    raw = None
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    if anthropic_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=anthropic_key)
            msg = client.messages.create(
                model="claude-opus-4-6",
                max_tokens=3000,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = msg.content[0].text
        except Exception as e:
            logger.warning(f"[QualityGate] Fix agent Anthropic error: {e}")

    if not raw and openai_key:
        try:
            from openai import OpenAI
            oai = OpenAI(api_key=openai_key)
            resp = oai.chat.completions.create(
                model="gpt-4o",
                max_tokens=3000,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = resp.choices[0].message.content
        except Exception as e:
            logger.warning(f"[QualityGate] Fix agent OpenAI error: {e}")

    if not raw:
        return False

    json_match = re.search(r'```(?:json)?\s*(\[[\s\S]+?\])\s*```', raw)
    if not json_match:
        json_match = re.search(r'(\[[\s\S]+\])', raw)
    if not json_match:
        return False

    try:
        changes = json.loads(json_match.group(1))
    except json.JSONDecodeError:
        return False

    applied = 0
    for change in changes:
        old = change.get("old_string", "")
        new = change.get("new_string", "")
        if not old or not new or old == new:
            continue
        if old in current_code:
            current_code = current_code.replace(old, new, 1)
            applied += 1
            logger.info(f"[QualityGate] Applied fix: {change.get('description', '')[:60]}")

    if applied > 0:
        service_path.write_text(current_code, encoding="utf-8")
        logger.info(f"[QualityGate] Wrote {applied} prompt fix(es) to openai_service.py")
        return True

    return False


async def quality_gate_run(domain: str, schedule_id: int) -> dict:
    """
    Run quality gate loop for a domain.
    Iterates: generate article → score → if <threshold, fix prompts → repeat.
    Returns final gate state dict.
    NOTE: This does NOT commit/push to git — prompt fixes are in-process only.
    Railway keeps the service running between requests so in-process changes persist.
    For permanent fixes, use the /api/quality-gate/trigger endpoint which also persists.
    """
    await _ensure_tables()
    await _update_gate_state(
        domain, status="running", iterations=0, last_run_at=datetime.now(timezone.utc).isoformat()
    )

    logger.info(f"[QualityGate] Starting gate for {domain} (threshold={QUALITY_THRESHOLD}, max_iter={QUALITY_MAX_ITER})")

    best_score = 0
    last_url = ""

    for iteration in range(1, QUALITY_MAX_ITER + 1):
        logger.info(f"[QualityGate] {domain} — iteration {iteration}/{QUALITY_MAX_ITER}")
        await _update_gate_state(domain, iterations=iteration)

        # 1. Generate article
        job_id = await _trigger_article(schedule_id, domain)
        if not job_id:
            logger.error(f"[QualityGate] Failed to trigger article for {domain}")
            await _update_gate_state(domain, status="error", error="Failed to trigger article")
            return await quality_gate_get_status(domain)

        # 2. Wait for article
        url = await _wait_for_job(job_id)
        if not url:
            logger.warning(f"[QualityGate] {domain} iter {iteration}: no URL, skipping")
            continue

        last_url = url
        logger.info(f"[QualityGate] {domain} iter {iteration}: published {url}")

        # 3. Fetch article text
        article_text = await _fetch_article_text(url)
        if not article_text or len(article_text.split()) < 100:
            logger.warning(f"[QualityGate] {domain} iter {iteration}: too short ({len(article_text.split())} words)")
            continue

        # 4. Score (blocking call — run in thread pool)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _score_with_ceo, article_text)

        total = result.get("total", 0)
        verdict = result.get("verdict", "FAIL")
        issues = result.get("top_issues", [])
        fixes = result.get("prompt_fixes", [])
        scores = result.get("scores", {})

        if total > best_score:
            best_score = total

        logger.info(
            f"[QualityGate] {domain} iter {iteration}: score={total}/100 ({verdict}) "
            f"best={best_score} url={url}\n"
            f"  Issues: {'; '.join(issues[:2])}\n"
            f"  Scores: {scores}"
        )

        await _update_gate_state(
            domain, score=total, last_article_url=url,
            status="running" if verdict != "PASS" else "passed"
        )

        # 5. Check pass
        if total >= QUALITY_THRESHOLD or verdict == "PASS":
            logger.info(f"[QualityGate] {domain} PASSED at iter {iteration} with score {total}/100")
            await _update_gate_state(domain, status="passed", score=total)
            return await quality_gate_get_status(domain)

        # 6. Apply prompt fixes (in-process — takes effect for next article)
        if iteration < QUALITY_MAX_ITER:
            changed = await loop.run_in_executor(None, _apply_prompt_fixes_with_ai, fixes, issues)
            if changed:
                logger.info(f"[QualityGate] {domain} prompt fixes applied for iter {iteration + 1}")

    # Max iterations reached
    logger.warning(f"[QualityGate] {domain} max iterations reached. Best score: {best_score}/100")
    final_status = "passed" if best_score >= QUALITY_THRESHOLD else "failed"
    await _update_gate_state(domain, status=final_status, score=best_score, last_article_url=last_url)
    return await quality_gate_get_status(domain)


async def quality_gate_reset(domain: str):
    """Reset gate state — domain will re-run quality check on next autopilot run."""
    await _ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM quality_gate_state WHERE domain = ?",
            (domain,)
        )
        await db.commit()
    logger.info(f"[QualityGate] Reset gate for {domain}")

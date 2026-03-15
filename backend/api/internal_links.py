"""
Internal Links API — semantic internal linking for published WP posts.

Flow:
1. POST /api/internal-links/analyze   — fetch all posts from domain, cluster by topic,
                                        generate link suggestions (GPT semantic matching)
2. POST /api/internal-links/apply     — apply approved suggestions via WP API editPost
3. GET  /api/internal-links/jobs      — list recent analyze jobs for a domain
4. GET  /api/internal-links/jobs/{id} — get job detail (suggestions list)

Semantic approach:
- GPT clusters articles into topic groups (not keyword matching)
- GPT finds natural insertion points in paragraph context
- Only suggests links between thematically related articles
- Max 3-5 new links per article to avoid over-linking
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from config import DB_PATH
from services.openai_service import get_openai_client
from services.crypto_service import get_plain_password

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/internal-links", tags=["internal-links"])

# Max concurrent GPT calls during suggestion generation
_GPT_SEM = asyncio.Semaphore(4)
# Max new links injected per article (anti-spam guard)
MAX_LINKS_PER_POST = 5


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _ensure_tables():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS il_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                my_domain_id INTEGER NOT NULL,
                domain TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                posts_total INTEGER DEFAULT 0,
                suggestions_total INTEGER DEFAULT 0,
                approved INTEGER DEFAULT 0,
                applied INTEGER DEFAULT 0,
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS il_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES il_jobs(id) ON DELETE CASCADE,
                source_post_id INTEGER NOT NULL,
                source_title TEXT,
                source_url TEXT,
                target_post_id INTEGER NOT NULL,
                target_title TEXT,
                target_url TEXT,
                anchor_text TEXT NOT NULL,
                context_before TEXT,
                context_after TEXT,
                paragraph_index INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                applied_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_il_job ON il_suggestions(job_id)")
        await db.commit()


async def _get_domain_creds(my_domain_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM my_domains WHERE id = ?",
            (my_domain_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            keys = row.keys()
            return {
                "domain": row["domain"],
                "wp_login": row["wp_login"],
                "wp_pass": get_plain_password(row["wp_pass"]),
                "http_user": (row["http_user"] if "http_user" in keys else "") or "",
                "http_pass": (row["http_pass"] if "http_pass" in keys else "") or "",
            }


# ---------------------------------------------------------------------------
# WordPress helpers
# ---------------------------------------------------------------------------

def _wp_base(domain: str) -> str:
    if domain.startswith("http"):
        return domain.rstrip("/")
    return f"https://{domain}"


def _wp_auth(wp_login: str, wp_pass: str) -> str:
    import base64
    return "Basic " + base64.b64encode(f"{wp_login}:{wp_pass}".encode()).decode()


async def _fetch_all_posts(domain: str, wp_login: str, wp_pass: str,
                           http_user: str = "", http_pass: str = "") -> list[dict]:
    """Fetch all published posts from WP API (paginated, 100/page)."""
    base = _wp_base(domain)
    auth = _wp_auth(wp_login, wp_pass)
    headers = {"Authorization": auth}
    site_auth = (http_user, http_pass) if http_user and http_pass else None

    posts = []
    page = 1
    async with httpx.AsyncClient(verify=False, timeout=30, auth=site_auth) as client:
        while True:
            try:
                resp = await client.get(
                    f"{base}/wp-json/wp/v2/posts",
                    headers=headers,
                    params={"status": "publish", "per_page": 100, "page": page,
                            "_fields": "id,title,link,excerpt,content"},
                )
                if resp.status_code == 400:
                    break  # no more pages
                if resp.status_code != 200:
                    logger.warning(f"[IL] WP posts fetch HTTP {resp.status_code} on page {page}")
                    break
                batch = resp.json()
                if not batch:
                    break
                for p in batch:
                    raw_content = p.get("content", {}).get("rendered", "") or ""
                    # Strip HTML for GPT context, keep limited excerpt
                    plain = re.sub(r"<[^>]+>", " ", raw_content)
                    plain = re.sub(r"\s+", " ", plain).strip()
                    posts.append({
                        "id": p["id"],
                        "title": p.get("title", {}).get("rendered", ""),
                        "url": p.get("link", ""),
                        "excerpt": plain[:600],  # first 600 chars for clustering
                        "content_html": raw_content,  # full HTML for link injection
                    })
                total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
                if page >= total_pages:
                    break
                page += 1
            except Exception as e:
                logger.warning(f"[IL] WP fetch error page {page}: {e}")
                break
    return posts


async def _update_post_content(domain: str, wp_login: str, wp_pass: str,
                               post_id: int, new_html: str,
                               http_user: str = "", http_pass: str = "") -> bool:
    """Update WP post content via REST API."""
    base = _wp_base(domain)
    auth = _wp_auth(wp_login, wp_pass)
    headers = {"Authorization": auth, "Content-Type": "application/json"}
    site_auth = (http_user, http_pass) if http_user and http_pass else None

    async with httpx.AsyncClient(verify=False, timeout=30, auth=site_auth) as client:
        try:
            resp = await client.post(
                f"{base}/wp-json/wp/v2/posts/{post_id}",
                headers=headers,
                json={"content": new_html},
            )
            return resp.status_code in (200, 201)
        except Exception as e:
            logger.warning(f"[IL] WP update post {post_id} error: {e}")
            return False


# ---------------------------------------------------------------------------
# GPT helpers
# ---------------------------------------------------------------------------

async def _gpt(system: str, user: str, temperature: float = 0.3,
               max_tokens: int = 2000) -> str:
    async with _GPT_SEM:
        client, model, _is_custom = await get_openai_client()
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
                    response_format={"type": "json_object"},
                )
                return resp.choices[0].message.content.strip()
            except Exception as e:
                if attempt == 2:
                    raise
                wait = 2 ** attempt
                logger.warning(f"[IL-GPT] attempt {attempt+1} failed: {e} — retry in {wait}s")
                await asyncio.sleep(wait)
    return "{}"


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

async def _cluster_posts(posts: list[dict], lang: str = "pl") -> list[list[int]]:
    """
    Ask GPT to group posts into topic clusters.
    Returns list of clusters, each cluster is a list of post indexes (0-based).
    """
    if len(posts) <= 2:
        return [list(range(len(posts)))]

    # Build compact post list for GPT
    post_list = "\n".join(
        f"{i}. [{p['title']}] — {p['excerpt'][:200]}"
        for i, p in enumerate(posts)
    )

    system = (
        "You are an SEO specialist. Group articles into thematic clusters "
        "so each cluster contains semantically related content. "
        "Articles in the same cluster should naturally link to each other. "
        "Return JSON: {\"clusters\": [[0,1,3], [2,4,5], ...]}"
    )
    user = (
        f"Language: {lang}\n\n"
        f"Articles (index. [title] — excerpt):\n{post_list}\n\n"
        "Group into topic clusters. Each article must appear in exactly one cluster. "
        "Minimum 2 articles per cluster. If an article doesn't fit anywhere, "
        "put it in the smallest existing cluster."
    )

    try:
        raw = await _gpt(system, user, temperature=0.1, max_tokens=1000)
        data = json.loads(raw)
        clusters = data.get("clusters", [])
        if clusters:
            return clusters
    except Exception as e:
        logger.warning(f"[IL] Clustering failed: {e}")

    # Fallback: single cluster
    return [list(range(len(posts)))]


async def _find_link_insertions(
    source: dict,
    candidates: list[dict],
    lang: str = "pl",
) -> list[dict]:
    """
    Ask GPT to find natural link insertion points in source article.
    Returns list of {target_idx, anchor_text, context_before, context_after, paragraph_index}.
    """
    if not candidates:
        return []

    # Split source into paragraphs (strip HTML tags for readability)
    paragraphs_html = re.findall(r"<p[^>]*>(.*?)</p>", source["content_html"], re.DOTALL | re.IGNORECASE)
    # Fall back to splitting on double newline if no <p> tags
    if not paragraphs_html:
        paragraphs_html = source["content_html"].split("\n\n")

    # Plain text paragraphs for GPT
    paragraphs_plain = [re.sub(r"<[^>]+>", " ", p).strip() for p in paragraphs_html]
    # Only send first 20 paragraphs to keep token usage reasonable
    para_list = "\n".join(
        f"P{i}: {p[:300]}" for i, p in enumerate(paragraphs_plain[:20]) if len(p) > 80
    )

    cand_list = "\n".join(
        f"{i}. [{c['title']}] — URL: {c['url']} — {c['excerpt'][:150]}"
        for i, c in enumerate(candidates)
    )

    system = (
        "You are an SEO specialist adding internal links to blog articles. "
        "Your job: find natural places in the source article where a link to "
        "one of the candidate articles would fit contextually. "
        "Rules:\n"
        "- Link must fit naturally in the paragraph — do NOT force it\n"
        "- Anchor text must be a natural phrase from the paragraph (2-5 words)\n"
        "- Maximum 3 suggestions total\n"
        "- Each candidate can be suggested at most once\n"
        "- Skip short or unrelated paragraphs\n"
        f"- Write anchor_text in the article's language ({lang})\n"
        "Return JSON: {\"suggestions\": [{\"target_idx\": 0, \"paragraph_index\": 3, "
        "\"anchor_text\": \"natural phrase\", \"context_before\": \"5 words before\", "
        "\"context_after\": \"5 words after\"}]}"
    )
    user = (
        f"Source article: [{source['title']}]\n\n"
        f"Paragraphs:\n{para_list}\n\n"
        f"Candidate articles to link to:\n{cand_list}\n\n"
        "Find up to 3 natural link insertion points."
    )

    try:
        raw = await _gpt(system, user, temperature=0.2, max_tokens=600)
        data = json.loads(raw)
        return data.get("suggestions", [])
    except Exception as e:
        logger.warning(f"[IL] Link insertion GPT failed for '{source['title']}': {e}")
        return []


def _inject_link_into_html(html: str, anchor_text: str, target_url: str,
                            paragraph_index: int) -> Optional[str]:
    """
    Inject <a href="target_url">anchor_text</a> into the paragraph at paragraph_index.
    Only injects if anchor_text appears verbatim in the paragraph.
    Returns modified HTML or None if injection was not possible.
    """
    paragraphs = re.findall(r"(<p[^>]*>)(.*?)(</p>)", html, re.DOTALL | re.IGNORECASE)
    if paragraph_index >= len(paragraphs):
        # Try to find the paragraph by anchor text across all paragraphs
        paragraph_index = None
        for i, (_, content, _) in enumerate(paragraphs):
            plain = re.sub(r"<[^>]+>", "", content)
            if anchor_text.lower() in plain.lower():
                paragraph_index = i
                break
        if paragraph_index is None:
            return None

    open_tag, content, close_tag = paragraphs[paragraph_index]

    # Check anchor text exists in plain content (case-insensitive)
    plain_content = re.sub(r"<[^>]+>", "", content)
    if anchor_text.lower() not in plain_content.lower():
        return None

    # Don't double-link — skip if anchor is already a link
    if f'href=' in content and anchor_text.lower() in content.lower():
        # Rough check: if anchor is inside an existing <a> tag, skip
        existing_links = re.findall(r'<a[^>]*>(.*?)</a>', content, re.DOTALL | re.IGNORECASE)
        for link_text in existing_links:
            if anchor_text.lower() in re.sub(r"<[^>]+>", "", link_text).lower():
                return None

    # Find exact case match first, fall back to case-insensitive
    idx = plain_content.find(anchor_text)
    if idx == -1:
        idx = plain_content.lower().find(anchor_text.lower())
        if idx == -1:
            return None
        anchor_text = plain_content[idx:idx + len(anchor_text)]

    # Replace first occurrence in content (HTML-aware: replace in plain text mapping)
    link_html = f'<a href="{target_url}">{anchor_text}</a>'
    new_content = content.replace(anchor_text, link_html, 1)

    new_paragraph = open_tag + new_content + close_tag

    # Replace the specific paragraph in the original HTML
    original_paragraph = open_tag + content + close_tag
    new_html = html.replace(original_paragraph, new_paragraph, 1)
    return new_html if new_html != html else None


# ---------------------------------------------------------------------------
# Background job runner
# ---------------------------------------------------------------------------

_running_jobs: dict[int, bool] = {}


async def _run_analyze_job(job_id: int, my_domain_id: int, lang: str = "pl"):
    """Full analysis pipeline — runs in background."""
    _running_jobs[job_id] = True
    try:
        creds = await _get_domain_creds(my_domain_id)
        if not creds:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE il_jobs SET status='error', error='Domain not found' WHERE id=?",
                    (job_id,)
                )
                await db.commit()
            return

        domain = creds["domain"]
        logger.info(f"[IL] Job {job_id}: fetching posts from {domain}")

        # Step 1: fetch all posts
        posts = await _fetch_all_posts(
            domain, creds["wp_login"], creds["wp_pass"],
            creds["http_user"], creds["http_pass"]
        )

        if not posts:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE il_jobs SET status='done', posts_total=0, "
                    "suggestions_total=0, finished_at=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(), job_id)
                )
                await db.commit()
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE il_jobs SET status='clustering', posts_total=? WHERE id=?",
                (len(posts), job_id)
            )
            await db.commit()

        logger.info(f"[IL] Job {job_id}: {len(posts)} posts, clustering...")

        # Step 2: cluster posts by topic
        clusters = await _cluster_posts(posts, lang)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE il_jobs SET status='analyzing' WHERE id=?", (job_id,)
            )
            await db.commit()

        # Step 3: for each post, find link insertion opportunities within its cluster
        suggestions_total = 0

        async def _process_post(post_idx: int, cluster: list[int]):
            nonlocal suggestions_total
            source = posts[post_idx]

            # Candidates = other posts in the same cluster
            candidates = [
                {"idx_in_cluster": j, "post_idx": c, **posts[c]}
                for j, c in enumerate(cluster)
                if c != post_idx
            ]

            if not candidates:
                return

            # Check how many internal links the post already has
            existing_link_count = len(re.findall(r'<a\s[^>]*href=', source["content_html"]))
            if existing_link_count >= 20:
                # Already heavily linked — skip
                return

            insertions = await _find_link_insertions(source, candidates, lang)

            # Store suggestions (max MAX_LINKS_PER_POST per source)
            count = 0
            async with aiosqlite.connect(DB_PATH) as db:
                for s in insertions:
                    if count >= MAX_LINKS_PER_POST:
                        break
                    tidx = s.get("target_idx")
                    if tidx is None or tidx >= len(candidates):
                        continue
                    target = candidates[tidx]
                    anchor = (s.get("anchor_text") or "").strip()
                    if not anchor or len(anchor) < 3:
                        continue

                    await db.execute(
                        """INSERT INTO il_suggestions
                           (job_id, source_post_id, source_title, source_url,
                            target_post_id, target_title, target_url,
                            anchor_text, context_before, context_after, paragraph_index, status)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending')""",
                        (
                            job_id,
                            source["id"], source["title"], source["url"],
                            target["id"], target["title"], target["url"],
                            anchor,
                            s.get("context_before", ""),
                            s.get("context_after", ""),
                            s.get("paragraph_index", 0),
                        )
                    )
                    count += 1
                    suggestions_total += 1
                await db.commit()

        # Process all posts concurrently within semaphore limits
        tasks = []
        for cluster in clusters:
            for post_idx in cluster:
                if post_idx < len(posts):
                    tasks.append(_process_post(post_idx, cluster))

        await asyncio.gather(*tasks, return_exceptions=True)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE il_jobs SET status='done', suggestions_total=?, finished_at=? WHERE id=?",
                (suggestions_total, datetime.now(timezone.utc).isoformat(), job_id)
            )
            await db.commit()

        logger.info(f"[IL] Job {job_id}: done — {suggestions_total} suggestions")

    except Exception as e:
        logger.error(f"[IL] Job {job_id} failed: {e}")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE il_jobs SET status='error', error=? WHERE id=?",
                (str(e)[:500], job_id)
            )
            await db.commit()
    finally:
        _running_jobs.pop(job_id, None)


# ---------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    my_domain_id: int
    lang: str = "pl"


class ApplyRequest(BaseModel):
    suggestion_ids: list[int]


class RejectRequest(BaseModel):
    suggestion_ids: list[int]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/analyze")
async def analyze_domain(body: AnalyzeRequest):
    """Start a new internal link analysis job for a domain."""
    await _ensure_tables()

    creds = await _get_domain_creds(body.my_domain_id)
    if not creds:
        raise HTTPException(404, "Domain not found")

    # Check if a job is already running for this domain
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id FROM il_jobs WHERE my_domain_id=? AND status IN ('pending','clustering','analyzing')",
            (body.my_domain_id,)
        ) as cur:
            running = await cur.fetchone()
        if running:
            raise HTTPException(409, f"Analysis already running (job #{running['id']})")

        async with db.execute(
            """INSERT INTO il_jobs (my_domain_id, domain, status)
               VALUES (?, ?, 'pending')""",
            (body.my_domain_id, creds["domain"])
        ) as cur:
            job_id = cur.lastrowid
        await db.commit()

    # Launch background task
    asyncio.create_task(_run_analyze_job(job_id, body.my_domain_id, body.lang))

    return {"job_id": job_id, "domain": creds["domain"], "status": "pending"}


@router.get("/jobs")
async def list_jobs(my_domain_id: int = Query(...), limit: int = Query(10, ge=1, le=50)):
    """List recent analysis jobs for a domain."""
    await _ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM il_jobs WHERE my_domain_id=?
               ORDER BY created_at DESC LIMIT ?""",
            (my_domain_id, limit)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return rows


@router.get("/jobs/{job_id}")
async def get_job(job_id: int, status_filter: str = Query("pending")):
    """Get job details with suggestions. status_filter: pending|approved|rejected|applied|all"""
    await _ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM il_jobs WHERE id=?", (job_id,)) as cur:
            job = await cur.fetchone()
        if not job:
            raise HTTPException(404, "Job not found")

        if status_filter == "all":
            q = "SELECT * FROM il_suggestions WHERE job_id=? ORDER BY source_title, id"
            params = (job_id,)
        else:
            q = "SELECT * FROM il_suggestions WHERE job_id=? AND status=? ORDER BY source_title, id"
            params = (job_id, status_filter)

        async with db.execute(q, params) as cur:
            suggestions = [dict(r) for r in await cur.fetchall()]

    return {**dict(job), "suggestions": suggestions}


@router.get("/jobs/{job_id}/status")
async def get_job_status(job_id: int):
    """Lightweight status poll."""
    await _ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, status, posts_total, suggestions_total, approved, applied, error, finished_at FROM il_jobs WHERE id=?",
            (job_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Job not found")
    return dict(row)


@router.post("/suggestions/approve")
async def approve_suggestions(body: ApplyRequest):
    """Mark suggestions as approved (ready to apply)."""
    await _ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        for sid in body.suggestion_ids:
            await db.execute(
                "UPDATE il_suggestions SET status='approved' WHERE id=? AND status='pending'",
                (sid,)
            )
        await db.commit()
    return {"ok": True, "approved": len(body.suggestion_ids)}


@router.post("/suggestions/reject")
async def reject_suggestions(body: RejectRequest):
    """Reject suggestions."""
    await _ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        for sid in body.suggestion_ids:
            await db.execute(
                "UPDATE il_suggestions SET status='rejected' WHERE id=?", (sid,)
            )
        await db.commit()
    return {"ok": True}


@router.post("/apply")
async def apply_suggestions(body: ApplyRequest):
    """
    Apply approved suggestions to WP posts.
    Fetches current post content from WP, injects links, saves back.
    """
    await _ensure_tables()

    if not body.suggestion_ids:
        raise HTTPException(400, "No suggestion IDs provided")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" * len(body.suggestion_ids))
        async with db.execute(
            f"""SELECT s.*, j.my_domain_id FROM il_suggestions s
                JOIN il_jobs j ON j.id = s.job_id
                WHERE s.id IN ({placeholders}) AND s.status IN ('approved','pending')""",
            body.suggestion_ids
        ) as cur:
            suggestions = [dict(r) for r in await cur.fetchall()]

    if not suggestions:
        raise HTTPException(404, "No applicable suggestions found")

    # Group suggestions by (domain, source_post_id) to batch edits
    from collections import defaultdict
    grouped: dict[tuple, list] = defaultdict(list)
    for s in suggestions:
        grouped[(s["my_domain_id"], s["source_post_id"])].append(s)

    applied = []
    failed = []

    for (domain_id, post_id), post_suggestions in grouped.items():
        creds = await _get_domain_creds(domain_id)
        if not creds:
            for s in post_suggestions:
                failed.append({"id": s["id"], "error": "Domain not found"})
            continue

        # Fetch current post HTML
        base = _wp_base(creds["domain"])
        auth = _wp_auth(creds["wp_login"], creds["wp_pass"])
        site_auth = (creds["http_user"], creds["http_pass"]) if creds["http_user"] else None

        try:
            async with httpx.AsyncClient(verify=False, timeout=30, auth=site_auth) as client:
                resp = await client.get(
                    f"{base}/wp-json/wp/v2/posts/{post_id}",
                    headers={"Authorization": auth},
                    params={"_fields": "id,content"},
                )
                if resp.status_code != 200:
                    for s in post_suggestions:
                        failed.append({"id": s["id"], "error": f"WP fetch HTTP {resp.status_code}"})
                    continue
                current_html = resp.json().get("content", {}).get("rendered", "")
        except Exception as e:
            for s in post_suggestions:
                failed.append({"id": s["id"], "error": str(e)})
            continue

        # Apply each suggestion sequentially into the HTML
        modified_html = current_html
        applied_in_post = []
        for s in post_suggestions:
            result = _inject_link_into_html(
                modified_html,
                s["anchor_text"],
                s["target_url"],
                s["paragraph_index"],
            )
            if result:
                modified_html = result
                applied_in_post.append(s["id"])
            else:
                failed.append({"id": s["id"], "error": "Anchor text not found in paragraph"})

        if not applied_in_post:
            continue

        # Save back to WP
        ok = await _update_post_content(
            creds["domain"], creds["wp_login"], creds["wp_pass"],
            post_id, modified_html,
            creds["http_user"], creds["http_pass"]
        )

        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            if ok:
                for sid in applied_in_post:
                    await db.execute(
                        "UPDATE il_suggestions SET status='applied', applied_at=? WHERE id=?",
                        (now, sid)
                    )
                applied.extend(applied_in_post)
            else:
                for sid in applied_in_post:
                    failed.append({"id": sid, "error": "WP update failed"})
            await db.commit()

        # Update job counters
        if applied_in_post:
            async with aiosqlite.connect(DB_PATH) as db:
                job_id = post_suggestions[0]["job_id"]
                await db.execute(
                    "UPDATE il_jobs SET applied = applied + ? WHERE id=?",
                    (len(applied_in_post), job_id)
                )
                await db.commit()

    return {
        "applied": len(applied),
        "failed": len(failed),
        "applied_ids": applied,
        "failed_details": failed,
    }

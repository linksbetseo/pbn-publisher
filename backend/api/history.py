import aiosqlite
from fastapi import APIRouter, Query, HTTPException
from typing import Optional
from config import DB_PATH

router = APIRouter(prefix="/api/history", tags=["history"])


@router.post("/indexer/submit")
async def submit_to_indexer(body: dict):
    """Proxy to RocketIndexer — keeps API token server-side."""
    import httpx
    import os
    token = os.getenv("ROCKET_INDEXER_TOKEN", "")
    urls = body.get("urls", [])
    if not urls:
        return {"success": False, "message": "No URLs provided"}
    if not token:
        return {"success": False, "message": "ROCKET_INDEXER_TOKEN not configured"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://rocketindexer.com/api/index.php?token={token}&endpoint=submit",
                json={"urls": urls},
                headers={"Content-Type": "application/json"},
            )
            return resp.json()
    except Exception as e:
        return {"success": False, "message": str(e)}


@router.post("/indexer/submit-telegram")
async def submit_to_telegram_indexer(body: dict):
    """Proxy to Telegram Indexer (link-indexing-bot.com) — keeps API key server-side."""
    import httpx
    import os
    api_key = os.getenv("TELEGRAM_INDEXER_TOKEN", "")
    user_id = os.getenv("TELEGRAM_INDEXER_USER_ID", "")
    urls = body.get("urls", [])
    se_type = body.get("se_type", "normal")
    if not urls:
        return {"success": False, "message": "No URLs provided"}
    if not api_key:
        return {"success": False, "message": "TELEGRAM_INDEXER_TOKEN not configured"}
    if not user_id:
        return {"success": False, "message": "TELEGRAM_INDEXER_USER_ID not configured"}
    if se_type not in ("normal", "hard"):
        se_type = "normal"
    links_str = "\n".join(urls)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://link-indexing-bot.com/api/tasks/new",
                data={
                    "api_key": api_key,
                    "user_id": user_id,
                    "links": links_str,
                    "searchengine": "google",
                    "se_type": se_type,
                },
            )
            data = resp.json()
            if resp.status_code in (200, 201):
                return {"success": True, "data": data, "submitted": len(urls)}
            return {"success": False, "message": data.get("message") or f"HTTP {resp.status_code}", "data": data}
    except Exception as e:
        return {"success": False, "message": str(e)}


@router.get("")
async def list_history(
    client_id: Optional[int] = Query(None),
    my_domain_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    batch_tag: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    limit: int = Query(100, le=5000),
    offset: int = Query(0, ge=0),
):
    conditions = []
    params = []

    if client_id:
        conditions.append("p.client_id = ?")
        params.append(client_id)
    if my_domain_id:
        conditions.append("p.my_domain_id = ?")
        params.append(my_domain_id)
    if status:
        conditions.append("p.status = ?")
        params.append(status)
    if batch_tag:
        conditions.append("p.batch_tag = ?")
        params.append(batch_tag)
    if date_from:
        conditions.append("DATE(p.created_at) >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("DATE(p.created_at) <= ?")
        params.append(date_to)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        SELECT p.*,
               c.name as client_name,
               md.domain as my_domain
        FROM posts p
        LEFT JOIN clients c ON c.id = p.client_id
        LEFT JOIN my_domains md ON md.id = p.my_domain_id
        {where}
        ORDER BY p.created_at DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        count_query = f"SELECT COUNT(*) FROM posts p {where}"
        async with db.execute(count_query, params[:-2]) as cursor:
            total = (await cursor.fetchone())[0]

    import re as _re
    posts_out = []
    for r in rows:
        d = dict(r)
        # Compute word count from content (strip HTML tags)
        plain = _re.sub(r'<[^>]+>', ' ', d.get("content") or "")
        d["word_count"] = len(plain.split())
        posts_out.append(d)
    return {"total": total, "offset": offset, "limit": limit, "posts": posts_out}


@router.get("/batches")
async def list_batches():
    """Return distinct non-empty batch_tags from posts table."""
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            async with db.execute(
                "SELECT DISTINCT batch_tag FROM posts WHERE batch_tag IS NOT NULL AND batch_tag != '' ORDER BY batch_tag DESC"
            ) as cursor:
                rows = await cursor.fetchall()
        except Exception:
            rows = []
    return [r[0] for r in rows]


@router.get("/stats")
async def history_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT status, COUNT(*) as count FROM posts GROUP BY status"
        ) as cursor:
            rows = await cursor.fetchall()
    return {r[0]: r[1] for r in rows}


@router.delete("/batch/{batch_tag}")
async def delete_batch(batch_tag: str):
    """Delete all posts belonging to a batch_tag."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM posts WHERE batch_tag = ?", (batch_tag,)) as cur:
            count = (await cur.fetchone())[0]
        await db.execute("DELETE FROM posts WHERE batch_tag = ?", (batch_tag,))
        await db.commit()
    return {"deleted": count, "batch_tag": batch_tag}


@router.get("/domain-stats")
async def history_domain_stats():
    """Returns published count per domain (for analytics)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT md.domain, COUNT(p.id) as count,
                      MAX(p.created_at) as last_published
               FROM posts p
               JOIN my_domains md ON md.id = p.my_domain_id
               WHERE p.status='published'
               GROUP BY md.domain ORDER BY count DESC LIMIT 50"""
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


@router.get("/{post_id}/preview")
async def post_preview(post_id: int):
    """Returns full content of a post for preview."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT p.id, p.title, p.content, p.created_at, p.status,
                      p.wp_post_url, p.batch_tag,
                      md.domain, c.name as client_name
               FROM posts p
               LEFT JOIN my_domains md ON md.id = p.my_domain_id
               LEFT JOIN clients c ON c.id = p.client_id
               WHERE p.id=?""",
            (post_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Post not found")
    return dict(row)


@router.delete("/{post_id}")
async def delete_post(post_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM posts WHERE id = ?", (post_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "Post not found")
        await db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        await db.commit()
    return {"deleted": post_id}


@router.post("/retry-all-failed")
async def retry_all_failed():
    """Re-publish ALL failed posts to WordPress."""
    from services.wordpress_service import publish_post as _wp_publish
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT p.*, md.domain, md.wp_login, md.wp_pass,
                      COALESCE(md.http_user,'') as http_user,
                      COALESCE(md.http_pass,'') as http_pass
               FROM posts p
               LEFT JOIN my_domains md ON md.id = p.my_domain_id
               WHERE p.status = 'failed' AND md.domain IS NOT NULL AND md.wp_login IS NOT NULL
               LIMIT 100"""
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        return {"retried": 0, "succeeded": 0, "failed": 0, "message": "Brak postów do ponowienia"}
    succeeded = 0
    failed = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for post in rows:
            try:
                result = await _wp_publish(
                    domain=post["domain"],
                    wp_login=post["wp_login"],
                    wp_pass=post["wp_pass"],
                    title=post["title"],
                    content=post["content"],
                    keyword=post["title"],
                    http_user=post.get("http_user", ""),
                    http_pass=post.get("http_pass", ""),
                )
                if result.get("success"):
                    wp_url = result.get("url", "")
                    await db.execute(
                        "UPDATE posts SET status='published', wp_post_url=? WHERE id=?",
                        (wp_url, post["id"])
                    )
                    await db.commit()
                    succeeded += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
    return {"retried": len(rows), "succeeded": succeeded, "failed": failed}


@router.post("/{post_id}/retry-status")
async def retry_failed_keyword(post_id: int):
    """Re-publish a failed/pending post to WordPress using the stored content."""
    from services.wordpress_service import publish_post
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT p.*, md.domain, md.wp_login, md.wp_pass,
                      COALESCE(md.http_user,'') as http_user,
                      COALESCE(md.http_pass,'') as http_pass
               FROM posts p
               LEFT JOIN my_domains md ON md.id = p.my_domain_id
               WHERE p.id = ?""",
            (post_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "Post not found")
        post = dict(row)

    if not post.get("domain") or not post.get("wp_login"):
        raise HTTPException(400, "Brak danych domeny — nie można ponownie opublikować")

    try:
        result = await publish_post(
            domain=post["domain"],
            wp_login=post["wp_login"],
            wp_pass=post["wp_pass"],
            title=post["title"],
            content=post["content"],
            keyword=post["title"],
            http_user=post.get("http_user", ""),
            http_pass=post.get("http_pass", ""),
        )
    except Exception as e:
        raise HTTPException(502, f"Błąd publikacji: {e}")

    if result.get("success"):
        wp_url = result.get("url", "")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE posts SET status='published', wp_post_url=? WHERE id=?",
                (wp_url, post_id)
            )
            await db.commit()
        return {"updated": post_id, "status": "published", "url": wp_url}
    else:
        raise HTTPException(502, result.get("error", "WP publish failed"))


@router.get("/failed-count")
async def failed_count():
    """Return count of failed posts (for sidebar badge)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM posts WHERE status='failed'") as cur:
            count = (await cur.fetchone())[0]
    return {"count": count}

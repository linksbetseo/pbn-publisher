import aiosqlite
from fastapi import APIRouter, Query, HTTPException
from typing import Optional
from config import DB_PATH

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("")
async def list_history(
    client_id: Optional[int] = Query(None),
    my_domain_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
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

    return {"total": total, "offset": offset, "limit": limit, "posts": [dict(r) for r in rows]}


@router.get("/stats")
async def history_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT status, COUNT(*) as count FROM posts GROUP BY status"
        ) as cursor:
            rows = await cursor.fetchall()
    return {r[0]: r[1] for r in rows}


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


@router.post("/{post_id}/retry-status")
async def retry_failed_keyword(post_id: int):
    """Reset a failed keyword back to pending so autopilot retries it."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM posts WHERE id = ?", (post_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "Post not found")
        await db.execute("UPDATE posts SET status='pending' WHERE id = ?", (post_id,))
        await db.commit()
    return {"updated": post_id, "status": "pending"}

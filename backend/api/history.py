import aiosqlite
from fastapi import APIRouter, Query
from typing import Optional
from config import DB_PATH

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("")
async def list_history(
    client_id: Optional[int] = Query(None),
    my_domain_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
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
        LIMIT 500
    """

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@router.get("/stats")
async def history_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT status, COUNT(*) as count FROM posts GROUP BY status"
        ) as cursor:
            rows = await cursor.fetchall()
    return {r[0]: r[1] for r in rows}

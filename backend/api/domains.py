import aiosqlite
from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional, List
from config import DB_PATH

router = APIRouter(prefix="/api/domains", tags=["domains"])


class DomainToggle(BaseModel):
    active: Optional[int] = None
    wp_ok: Optional[int] = None


class DomainImportItem(BaseModel):
    domain: str
    wp_login: str
    wp_pass: str
    server: str
    active: int = 1
    wp_ok: Optional[int] = None


@router.post("/bulk-import")
async def bulk_import_domains(items: List[DomainImportItem]):
    """Import multiple domains at once. Skips duplicates by domain name."""
    async with aiosqlite.connect(DB_PATH) as db:
        inserted = 0
        skipped = 0
        for item in items:
            async with db.execute(
                "SELECT id FROM my_domains WHERE domain = ?", (item.domain,)
            ) as cursor:
                existing = await cursor.fetchone()
            if existing:
                skipped += 1
                continue
            await db.execute(
                "INSERT INTO my_domains (domain, wp_login, wp_pass, server, active, wp_ok) VALUES (?,?,?,?,?,?)",
                (item.domain, item.wp_login, item.wp_pass, item.server, item.active, item.wp_ok),
            )
            inserted += 1
        await db.commit()
    return {"inserted": inserted, "skipped": skipped, "total": len(items)}


@router.get("/servers")
async def list_servers():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT DISTINCT server FROM my_domains WHERE server != '' ORDER BY server"
        ) as cursor:
            rows = await cursor.fetchall()
    return [r[0] for r in rows]


@router.get("")
async def list_domains(
    server: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    active: Optional[int] = Query(None),
):
    conditions = []
    params = []

    if server and server != "all":
        conditions.append("server = ?")
        params.append(server)
    if search:
        conditions.append("domain LIKE ?")
        params.append(f"%{search}%")
    if active is not None:
        conditions.append("active = ?")
        params.append(active)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"SELECT * FROM my_domains {where} ORDER BY server, domain"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@router.get("/used")
async def used_domains(client_id: Optional[int] = Query(None)):
    """Return IDs of my_domains already used for a given client."""
    if not client_id:
        return []
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT DISTINCT my_domain_id FROM posts WHERE client_id = ? AND status = 'published'",
            (client_id,)
        ) as cursor:
            rows = await cursor.fetchall()
    return [r[0] for r in rows if r[0] is not None]


@router.delete("/bulk-delete")
async def bulk_delete_domains(domains: List[str]):
    """Delete multiple domains by name."""
    async with aiosqlite.connect(DB_PATH) as db:
        placeholders = ",".join(["?" for _ in domains])
        await db.execute(f"DELETE FROM my_domains WHERE domain IN ({placeholders})", domains)
        await db.commit()
    return {"deleted": len(domains)}


@router.delete("/{domain_id}")
async def delete_domain(domain_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM my_domains WHERE id = ?", (domain_id,))
        await db.commit()
    return {"deleted": domain_id}


@router.patch("/{domain_id}")
async def toggle_domain(domain_id: int, body: DomainToggle):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM my_domains WHERE id = ?", (domain_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Domain not found")

        new_active = body.active if body.active is not None else (0 if row["active"] else 1)
        if body.wp_ok is not None:
            await db.execute(
                "UPDATE my_domains SET active = ?, wp_ok = ? WHERE id = ?", (new_active, body.wp_ok, domain_id)
            )
        else:
            await db.execute(
                "UPDATE my_domains SET active = ? WHERE id = ?", (new_active, domain_id)
            )
        await db.commit()

        async with db.execute(
            "SELECT * FROM my_domains WHERE id = ?", (domain_id,)
        ) as cursor:
            updated = await cursor.fetchone()
    return dict(updated)

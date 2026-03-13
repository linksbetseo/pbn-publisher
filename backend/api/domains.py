import aiosqlite
from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional, List
from config import DB_PATH
from services.crypto_service import encrypt_password

router = APIRouter(prefix="/api/domains", tags=["domains"])


_columns_ensured = False


async def _ensure_http_auth_columns():
    """Migration: add http_user / http_pass / project_id / batch_tag columns if missing."""
    global _columns_ensured
    if _columns_ensured:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        for col in [
            ("http_user", "TEXT DEFAULT ''"),
            ("http_pass", "TEXT DEFAULT ''"),
            ("project_id", "INTEGER DEFAULT NULL"),
            ("batch_tag", "TEXT DEFAULT ''"),
        ]:
            try:
                await db.execute(f"ALTER TABLE my_domains ADD COLUMN {col[0]} {col[1]}")
            except Exception:
                pass
        await db.commit()
    _columns_ensured = True


class DomainToggle(BaseModel):
    active: Optional[int] = None
    wp_ok: Optional[int] = None
    http_user: Optional[str] = None
    http_pass: Optional[str] = None


class DomainImportItem(BaseModel):
    domain: str
    wp_login: str
    wp_pass: str
    server: str
    active: int = 1
    wp_ok: Optional[int] = None
    http_user: Optional[str] = None
    http_pass: Optional[str] = None


class BatchImportRequest(BaseModel):
    lines: str
    batch_tag: str
    project_id: Optional[int] = None
    server: str = ""


@router.get("/batches")
async def list_batches():
    """List distinct batch_tags with domain count and earliest created_at."""
    await _ensure_http_auth_columns()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT d.batch_tag, COUNT(*) as count, d.project_id,
                   p.name as project_name
            FROM my_domains d
            LEFT JOIN projects p ON p.id = d.project_id
            WHERE d.batch_tag != '' AND d.batch_tag IS NOT NULL
            GROUP BY d.batch_tag, d.project_id
            ORDER BY d.batch_tag DESC
            """
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@router.post("/batch-import")
async def batch_import(body: BatchImportRequest):
    """Parse domain;login;password lines and insert, skipping duplicates."""
    await _ensure_http_auth_columns()
    inserted = 0
    skipped = 0
    errors = []
    async with aiosqlite.connect(DB_PATH) as db:
        for raw in body.lines.splitlines():
            line = raw.strip()
            if not line:
                continue
            parts = line.split(";")
            if len(parts) < 3:
                errors.append(f"Pominięto (zły format): {line}")
                continue
            domain = parts[0].strip()
            wp_login = parts[1].strip()
            wp_pass = parts[2].strip()
            server = parts[3].strip() if len(parts) >= 4 else body.server
            if not domain:
                errors.append(f"Pominięto (brak domeny): {line}")
                continue
            async with db.execute(
                "SELECT id FROM my_domains WHERE domain = ?", (domain,)
            ) as cursor:
                existing = await cursor.fetchone()
            if existing:
                skipped += 1
                continue
            encrypted_pass = encrypt_password(wp_pass)
            await db.execute(
                """INSERT INTO my_domains
                   (domain, wp_login, wp_pass, server, active, http_user, http_pass, batch_tag, project_id)
                   VALUES (?,?,?,?,1,'','',?,?)""",
                (domain, wp_login, encrypted_pass, server, body.batch_tag, body.project_id),
            )
            inserted += 1
        await db.commit()
    return {"inserted": inserted, "skipped": skipped, "errors": errors}


@router.post("/bulk-import")
async def bulk_import_domains(items: List[DomainImportItem]):
    """Import multiple domains at once. Skips duplicates by domain name."""
    await _ensure_http_auth_columns()
    async with aiosqlite.connect(DB_PATH) as db:
        # Single query to find all existing domains — avoids N+1
        domain_names = [item.domain for item in items]
        placeholders = ",".join("?" * len(domain_names))
        async with db.execute(
            f"SELECT domain FROM my_domains WHERE domain IN ({placeholders})", domain_names
        ) as cursor:
            existing_set = {r[0] for r in await cursor.fetchall()}

        inserted = 0
        skipped = 0
        for item in items:
            if item.domain in existing_set:
                skipped += 1
                continue
            encrypted_pass = encrypt_password(item.wp_pass)
            await db.execute(
                "INSERT INTO my_domains (domain, wp_login, wp_pass, server, active, wp_ok, http_user, http_pass) VALUES (?,?,?,?,?,?,?,?)",
                (item.domain, item.wp_login, encrypted_pass, item.server, item.active, item.wp_ok,
                 item.http_user or "", item.http_pass or ""),
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
    batch_tag: Optional[str] = Query(None),
):
    conditions = []
    params = []

    if server and server != "all":
        conditions.append("server = ?")
        params.append(server)
    if search:
        conditions.append("domain LIKE ? ESCAPE '\\'")
        _escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{_escaped}%")
    if active is not None:
        conditions.append("active = ?")
        params.append(active)
    if batch_tag is not None:
        conditions.append("batch_tag = ?")
        params.append(batch_tag)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = (
        f"SELECT id, domain, server, active, wp_ok, http_user, batch_tag, project_id "
        f"FROM my_domains {where} ORDER BY server, domain"
    )

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

        # Only toggle active if explicitly sent; otherwise preserve current value
        if body.active is not None:
            new_active = body.active
        else:
            new_active = row["active"]
        updates = ["active = ?"]
        params = [new_active]
        if body.wp_ok is not None:
            updates.append("wp_ok = ?")
            params.append(body.wp_ok)
        if body.http_user is not None:
            updates.append("http_user = ?")
            params.append(body.http_user)
        if body.http_pass is not None:
            updates.append("http_pass = ?")
            params.append(body.http_pass)
        params.append(domain_id)
        await db.execute(f"UPDATE my_domains SET {', '.join(updates)} WHERE id = ?", params)
        await db.commit()

        async with db.execute(
            "SELECT * FROM my_domains WHERE id = ?", (domain_id,)
        ) as cursor:
            updated = await cursor.fetchone()
    return dict(updated)

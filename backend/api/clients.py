import aiosqlite
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
from config import DB_PATH

router = APIRouter(prefix="/api/clients", tags=["clients"])


class ClientCreate(BaseModel):
    project_id: int
    name: str


class DomainAdd(BaseModel):
    domain: str


@router.get("")
async def list_clients(project_id: Optional[int] = Query(None)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if project_id:
            async with db.execute(
                "SELECT * FROM clients WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            ) as cursor:
                clients = await cursor.fetchall()
        else:
            async with db.execute(
                "SELECT * FROM clients ORDER BY created_at DESC"
            ) as cursor:
                clients = await cursor.fetchall()

        result = []
        for c in clients:
            async with db.execute(
                "SELECT id, domain FROM client_domains WHERE client_id = ?", (c["id"],)
            ) as cursor:
                domains = await cursor.fetchall()
            result.append({
                **dict(c),
                "domains": [dict(d) for d in domains],
            })
    return result


@router.post("", status_code=201)
async def create_client(body: ClientCreate):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM projects WHERE id = ?", (body.project_id,)
        ) as cursor:
            if not await cursor.fetchone():
                raise HTTPException(status_code=404, detail="Project not found")
        cursor = await db.execute(
            "INSERT INTO clients (project_id, name) VALUES (?, ?)",
            (body.project_id, body.name),
        )
        await db.commit()
        client_id = cursor.lastrowid
    return {"id": client_id, "name": body.name, "project_id": body.project_id, "domains": []}


@router.delete("/{client_id}", status_code=204)
async def delete_client(client_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM clients WHERE id = ?", (client_id,)
        ) as cursor:
            if not await cursor.fetchone():
                raise HTTPException(status_code=404, detail="Client not found")
        await db.execute("DELETE FROM clients WHERE id = ?", (client_id,))
        await db.commit()
    return None


@router.post("/{client_id}/domains", status_code=201)
async def add_domain(client_id: int, body: DomainAdd):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM clients WHERE id = ?", (client_id,)
        ) as cursor:
            if not await cursor.fetchone():
                raise HTTPException(status_code=404, detail="Client not found")
        cursor = await db.execute(
            "INSERT INTO client_domains (client_id, domain) VALUES (?, ?)",
            (client_id, body.domain),
        )
        await db.commit()
        domain_id = cursor.lastrowid
    return {"id": domain_id, "client_id": client_id, "domain": body.domain}


@router.delete("/{client_id}/domains/{domain_id}", status_code=204)
async def delete_domain(client_id: int, domain_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM client_domains WHERE id = ? AND client_id = ?",
            (domain_id, client_id),
        ) as cursor:
            if not await cursor.fetchone():
                raise HTTPException(status_code=404, detail="Domain not found")
        await db.execute(
            "DELETE FROM client_domains WHERE id = ?", (domain_id,)
        )
        await db.commit()
    return None

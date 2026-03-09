import aiosqlite
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from config import DB_PATH

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    name: str


@router.get("")
async def list_projects():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT p.id, p.name, p.created_at,
                   COUNT(c.id) as client_count
            FROM projects p
            LEFT JOIN clients c ON c.project_id = p.id
            GROUP BY p.id
            ORDER BY p.created_at DESC
            """
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@router.post("", status_code=201)
async def create_project(body: ProjectCreate):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO projects (name) VALUES (?)", (body.name,)
        )
        await db.commit()
        project_id = cursor.lastrowid
    return {"id": project_id, "name": body.name}


@router.delete("/{project_id}", status_code=204)
async def delete_project(project_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM projects WHERE id = ?", (project_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Project not found")
        await db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        await db.commit()
    return None

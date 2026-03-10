"""
Topical Map API — generate pillar + supporting page structure.
"""
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

from config import DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD
from services.topical_map_service import generate_topical_map

router = APIRouter(prefix="/api/topical-map", tags=["topical-map"])

DFS_LOGIN = DATAFORSEO_LOGIN
DFS_PASSWORD = DATAFORSEO_PASSWORD


class TopicalMapRequest(BaseModel):
    seed: str
    location_code: int = 2616
    language_code: str = "pl"
    min_volume: int = 50
    max_clusters: int = 8


@router.post("")
async def build_topical_map(req: TopicalMapRequest):
    """Generate topical map with pillar + supporting pages from seed keyword."""
    result = await generate_topical_map(
        seed=req.seed,
        location_code=req.location_code,
        language_code=req.language_code,
        min_volume=req.min_volume,
        max_clusters=req.max_clusters,
        dfs_login=DFS_LOGIN,
        dfs_password=DFS_PASSWORD,
    )
    return result

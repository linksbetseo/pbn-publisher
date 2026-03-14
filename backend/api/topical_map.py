"""
Topical Map API — generate pillar + supporting page structure.
FIX #25: force_refresh exposed in API
FIX #26: min_coherence exposed in API
FIX #27: seed validation
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator
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
    force_refresh: bool = False
    min_coherence: float = 0.15
    competitor_domain: str = ""  # FIX #17: check what domain already ranks for
    domain_url: str = ""
    site_description: str = ""

    # FIX #27: validate seed
    @field_validator("seed")
    @classmethod
    def validate_seed(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Seed nie może być pusty")
        if len(v) > 200:
            raise ValueError("Seed nie może mieć więcej niż 200 znaków")
        return v

    @field_validator("max_clusters")
    @classmethod
    def validate_clusters(cls, v: int) -> int:
        return max(2, min(15, v))

    @field_validator("min_coherence")
    @classmethod
    def validate_coherence(cls, v: float) -> float:
        return max(0.0, min(0.5, v))


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
        force_refresh=req.force_refresh,
        min_coherence=req.min_coherence,
        competitor_domain=req.competitor_domain.strip(),
        domain_url=req.domain_url.strip(),
        site_description=req.site_description.strip(),
    )
    return result

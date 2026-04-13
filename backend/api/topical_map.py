"""
Topical Map API — generate pillar + supporting page structure.
FIX #25: force_refresh exposed in API
FIX #26: min_coherence exposed in API
FIX #27: seed validation
STRATEGY: named presets — breadth, depth, competitor_gap, quick_wins, topical_authority
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator
from typing import Literal, Optional

from config import DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD
from services.topical_map_service import generate_topical_map, STRATEGY_PRESETS

router = APIRouter(prefix="/api/topical-map", tags=["topical-map"])

DFS_LOGIN = DATAFORSEO_LOGIN
DFS_PASSWORD = DATAFORSEO_PASSWORD

_VALID_STRATEGIES = {"default", *STRATEGY_PRESETS.keys()}


class TopicalMapRequest(BaseModel):
    seed: str
    location_code: int = 2616
    language_code: str = "pl"
    # None = let strategy preset decide; explicit value overrides preset
    min_volume: int | None = None
    max_clusters: int | None = None
    force_refresh: bool = False
    min_coherence: float | None = None
    competitor_domain: str = ""
    domain_url: str = ""
    site_description: str = ""
    # Strategy preset — overrides min_volume / max_clusters / min_coherence when not set
    # 'default'           — fallback defaults: min_volume=10, max_clusters=8, min_coherence=0.0
    # 'breadth'           — wide map, many pillars, loose semantics
    # 'depth'             — few pillars, deep supporting
    # 'competitor_gap'    — gap analysis vs competitor_domain
    # 'quick_wins'        — low-KD frazy only (KD < 30)
    # 'topical_authority' — full topic coverage, strict semantics
    strategy: str = "default"

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
    def validate_clusters(cls, v):
        if v is None:
            return v
        return max(2, min(15, v))

    @field_validator("min_coherence")
    @classmethod
    def validate_coherence(cls, v):
        if v is None:
            return v
        return max(0.0, min(0.5, v))

    @field_validator("strategy")
    @classmethod
    def validate_strategy(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in _VALID_STRATEGIES:
            raise ValueError(f"Nieznana strategia '{v}'. Dostępne: {sorted(_VALID_STRATEGIES)}")
        return v


@router.get("/strategies")
async def list_strategies():
    """List all available topical map strategies with descriptions."""
    return {
        "strategies": [
            {
                "name": "default",
                "description": "Domyślna mapa bez presetu — użyj własnych parametrów.",
                "params": {},
            }
        ] + [
            {
                "name": name,
                "description": preset.get("_description", ""),
                "params": {k: v for k, v in preset.items() if not k.startswith("_")},
            }
            for name, preset in STRATEGY_PRESETS.items()
        ]
    }


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
        strategy=req.strategy,
    )
    return result

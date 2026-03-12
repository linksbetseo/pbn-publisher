"""
Content Writer API — SEO article generation with SERP top10 analysis.
"""
import asyncio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from config import DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD
from services.content_writer_service import generate_seo_article

router = APIRouter(prefix="/api/content-writer", tags=["content-writer"])

DFS_LOGIN = DATAFORSEO_LOGIN
DFS_PASSWORD = DATAFORSEO_PASSWORD

# Limit concurrent article generation to avoid overloading OpenAI / DataForSEO
_generate_sem = asyncio.Semaphore(3)


class ContentWriterRequest(BaseModel):
    keyword: str
    client_domain: str
    anchor_text: str
    language: str = "pl"
    anchor_text2: str = ""
    anchor_url2: str = ""
    anchor_text3: str = ""
    anchor_url3: str = ""
    custom_prompt: str = ""
    variation_hint: str = ""
    pillar_page_url: str = ""
    pillar_page_anchor: str = ""
    supporting_page_urls: List[str] = []
    tone_of_voice: str = "ekspert"
    use_serp_scrape: bool = True


@router.post("/generate")
async def generate_content(req: ContentWriterRequest):
    """Generate SEO article based on keyword + SERP top10 analysis."""
    async with _generate_sem:
        result = await generate_seo_article(
            keyword=req.keyword,
            client_domain=req.client_domain,
            anchor_text=req.anchor_text,
            language=req.language,
            anchor_text2=req.anchor_text2,
            anchor_url2=req.anchor_url2,
            anchor_text3=req.anchor_text3,
            anchor_url3=req.anchor_url3,
            custom_prompt=req.custom_prompt,
            variation_hint=req.variation_hint,
            pillar_page_url=req.pillar_page_url,
            pillar_page_anchor=req.pillar_page_anchor,
            supporting_page_urls=req.supporting_page_urls,
            tone_of_voice=req.tone_of_voice,
            dfs_login=DFS_LOGIN,
            dfs_password=DFS_PASSWORD,
            use_serp_scrape=req.use_serp_scrape,
        )
    return result

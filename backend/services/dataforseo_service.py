"""
DataForSEO integration for PBN Publisher.
Provides SERP top10 scraping and keyword research.
"""
import base64
import httpx
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class DataForSEOClient:
    BASE_URL = "https://api.dataforseo.com/v3"

    def __init__(self, login: str, password: str):
        self.login = login
        self.password = password
        creds = base64.b64encode(f"{login}:{password}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/json",
        }

    async def request(self, endpoint: str, payload: list) -> dict:
        url = f"{self.BASE_URL}/{endpoint}"
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload, headers=self.headers)
            resp.raise_for_status()
            return resp.json()

    async def serp_top10(self, keyword: str, location_code: int = 2616, language_code: str = "pl") -> list[dict]:
        """Fetch top 10 organic SERP results for a keyword."""
        data = await self.request(
            "serp/google/organic/live/advanced",
            [{
                "keyword": keyword,
                "location_code": location_code,
                "language_code": language_code,
                "device": "desktop",
                "depth": 10,
            }]
        )
        results = []
        for task in data.get("tasks", []):
            for result in task.get("result", []):
                for item in result.get("items", []):
                    if item.get("type") == "organic":
                        results.append({
                            "rank": item.get("rank_absolute"),
                            "url": item.get("url"),
                            "title": item.get("title"),
                            "description": item.get("description", ""),
                        })
        return results[:10]

    async def page_content(self, url: str) -> str:
        """Fetch page content via DataForSEO on_page."""
        try:
            data = await self.request(
                "on_page/instant_pages",
                [{
                    "url": url,
                    "enable_javascript": False,
                    "load_resources": False,
                    "enable_browser_rendering": False,
                }]
            )
            for task in data.get("tasks", []):
                for result in task.get("result", []):
                    for item in result.get("items", []):
                        content = item.get("page_content", "") or ""
                        if content:
                            return content[:8000]
        except Exception as e:
            logger.warning(f"Failed to fetch content for {url}: {e}")
        return ""

    async def keyword_suggestions(self, seed: str, location_code: int = 2616, language_code: str = "pl", limit: int = 200) -> list[dict]:
        """Get keyword suggestions from DataForSEO."""
        data = await self.request(
            "dataforseo_labs/google/keyword_suggestions/live",
            [{
                "keyword": seed,
                "location_code": location_code,
                "language_code": language_code,
                "limit": limit,
                "include_seed_keyword": True,
            }]
        )
        keywords = []
        for task in data.get("tasks", []):
            for result in task.get("result", []):
                for item in result.get("items", []):
                    kw = item.get("keyword", "")
                    ki = item.get("keyword_info", {})
                    kp = item.get("keyword_properties", {})
                    if kw:
                        keywords.append({
                            "keyword": kw,
                            "search_volume": ki.get("search_volume", 0) or 0,
                            "keyword_difficulty": kp.get("keyword_difficulty", 0) or 0,
                            "cpc": ki.get("cpc", 0) or 0,
                        })
        return keywords

    async def keyword_ideas(self, seed: str, location_code: int = 2616, language_code: str = "pl", limit: int = 200) -> list[dict]:
        """Get keyword ideas from DataForSEO."""
        data = await self.request(
            "dataforseo_labs/google/keyword_ideas/live",
            [{
                "keywords": [seed],
                "location_code": location_code,
                "language_code": language_code,
                "limit": limit,
            }]
        )
        keywords = []
        for task in data.get("tasks", []):
            for result in task.get("result", []):
                for item in result.get("items", []):
                    kw = item.get("keyword", "")
                    ki = item.get("keyword_info", {})
                    kp = item.get("keyword_properties", {})
                    if kw:
                        keywords.append({
                            "keyword": kw,
                            "search_volume": ki.get("search_volume", 0) or 0,
                            "keyword_difficulty": kp.get("keyword_difficulty", 0) or 0,
                            "cpc": ki.get("cpc", 0) or 0,
                        })
        return keywords

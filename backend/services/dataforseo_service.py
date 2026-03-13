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
            "User-Agent": "PBN-Publisher/1.0",
        }

    async def request(self, endpoint: str, payload: list, _client: httpx.AsyncClient = None) -> dict:
        url = f"{self.BASE_URL}/{endpoint}"
        if _client:
            resp = await _client.post(url, json=payload, headers=self.headers)
            resp.raise_for_status()
            return resp.json()
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

    async def page_content(self, url: str, _client: httpx.AsyncClient = None) -> str:
        """Fetch page content via DataForSEO on_page. SEO #76: respects robots via DataForSEO."""
        try:
            data = await self.request(
                "on_page/instant_pages",
                [{
                    "url": url,
                    "enable_javascript": False,
                    "load_resources": False,
                    "enable_browser_rendering": False,
                }],
                _client=_client,
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
                            "intent": kp.get("search_intent", "informational") or "informational",
                        })
        return keywords

    async def serp_top10_full(self, keyword: str, location_code: int = 2616, language_code: str = "pl", _client: httpx.AsyncClient = None) -> dict:
        """Fetch SERP with both organic results and PAA (People Also Ask) questions."""
        data = await self.request(
            "serp/google/organic/live/advanced",
            [{
                "keyword": keyword,
                "location_code": location_code,
                "language_code": language_code,
                "device": "desktop",
                "depth": 10,
            }],
            _client=_client,
        )
        organic = []
        paa = []
        for task in data.get("tasks", []):
            for result in task.get("result", []):
                for item in result.get("items", []):
                    if item.get("type") == "organic":
                        organic.append({
                            "rank": item.get("rank_absolute"),
                            "url": item.get("url"),
                            "title": item.get("title"),
                            "description": item.get("description", ""),
                        })
                    elif item.get("type") == "people_also_ask":
                        for paa_item in item.get("items", []):
                            q = paa_item.get("title") or paa_item.get("question", "")
                            if q:
                                paa.append(q)
        # SEO #75: deduplicate organic results by URL (featured snippets can duplicate)
        _seen_urls: set = set()
        _deduped_organic = []
        for item in organic:
            _u = item.get("url", "").rstrip("/")
            if _u and _u not in _seen_urls:
                _seen_urls.add(_u)
                _deduped_organic.append(item)
        return {"organic": _deduped_organic[:10], "paa": list(dict.fromkeys(paa))[:8]}

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
                            "intent": kp.get("search_intent", "informational") or "informational",
                        })
        return keywords

    async def related_keywords(self, seed: str, location_code: int = 2616, language_code: str = "pl", limit: int = 200) -> list[dict]:
        """Get semantically related keywords from DataForSEO."""
        data = await self.request(
            "dataforseo_labs/google/related_keywords/live",
            [{
                "keyword": seed,
                "location_code": location_code,
                "language_code": language_code,
                "limit": limit,
                "depth": 1,
            }]
        )
        keywords = []
        for task in data.get("tasks", []):
            for result in task.get("result", []):
                for item in result.get("items", []):
                    kw = item.get("keyword_data", {})
                    keyword_text = kw.get("keyword", "")
                    ki = kw.get("keyword_info", {})
                    kp = kw.get("keyword_properties", {})
                    if keyword_text:
                        keywords.append({
                            "keyword": keyword_text,
                            "search_volume": ki.get("search_volume", 0) or 0,
                            "keyword_difficulty": kp.get("keyword_difficulty", 0) or 0,
                            "cpc": ki.get("cpc", 0) or 0,
                            "intent": kp.get("search_intent", "informational") or "informational",
                        })
        return keywords

    async def keywords_for_site(self, target: str, location_code: int = 2616, language_code: str = "pl", limit: int = 100) -> list[dict]:
        """Get keywords a domain currently ranks for in Google (DataForSEO Labs)."""
        clean = target.replace("https://", "").replace("http://", "").rstrip("/")
        data = await self.request(
            "dataforseo_labs/google/keywords_for_site/live",
            [{
                "target": clean,
                "location_code": location_code,
                "language_code": language_code,
                "limit": limit,
                "filters": [
                    ["keyword_data.keyword_info.search_volume", ">=", 10],
                ],
                "order_by": ["keyword_data.keyword_info.search_volume,desc"],
            }]
        )
        keywords = []
        for task in data.get("tasks", []):
            for result in task.get("result", []):
                for item in result.get("items", []):
                    kd = item.get("keyword_data", {})
                    kw = kd.get("keyword", "")
                    ki = kd.get("keyword_info", {})
                    kp = kd.get("keyword_properties", {})
                    if kw:
                        keywords.append({
                            "keyword": kw,
                            "search_volume": ki.get("search_volume", 0) or 0,
                            "keyword_difficulty": kp.get("keyword_difficulty", 0) or 0,
                            "cpc": ki.get("cpc", 0) or 0,
                            "intent": kp.get("search_intent", "informational") or "informational",
                            "position": item.get("ranked_serp_element", {}).get("serp_item", {}).get("rank_absolute", 0),
                        })
        return keywords

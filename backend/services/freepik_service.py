"""
Freepik stock photo search service.
Searches /v1/resources by keyword, downloads first result, returns base64.
"""
import base64
import logging
import os

import httpx

logger = logging.getLogger(__name__)

FREEPIK_API_KEY = os.getenv("FREEPIK_API_KEY", "")
SEARCH_URL = "https://api.freepik.com/v1/resources"


def _headers() -> dict:
    return {
        "x-freepik-api-key": FREEPIK_API_KEY,
        "Accept": "application/json",
    }


async def generate_image_freepik(prompt: str, use_pro: bool = False) -> str:
    """
    Search Freepik for a stock photo matching the prompt keyword.
    Returns base64 JPEG string, or raises on failure.
    use_pro is ignored (kept for API compatibility).
    """
    if not FREEPIK_API_KEY:
        raise RuntimeError("FREEPIK_API_KEY not set")

    # Search for stock photos
    params = {
        "term": prompt,
        "limit": 1,
        "order": "relevance",
        "filters[content_type][photo]": 1,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(SEARCH_URL, params=params, headers=_headers())
        resp.raise_for_status()
        result = resp.json()

    logger.info(f"[Freepik] raw response keys: {list(result.keys())}, first item keys: {list(result.get('data', [{}])[0].keys()) if result.get('data') else 'no data'}")
    items = result.get("data", [])
    if not items:
        raise RuntimeError(f"No Freepik results for prompt: {prompt}")

    # Extract image URL — try all known response shapes
    image_url = None
    for item in items:
        image_url = (
            (item.get("image") or {}).get("source", {}).get("url")
            or (item.get("image") or {}).get("url")
            or (item.get("preview") or {}).get("url")
            or item.get("url")
            or item.get("preview_url")
        )
        # Also check thumbnails array
        if not image_url:
            thumbs = item.get("thumbnails") or []
            if thumbs:
                image_url = thumbs[-1].get("url") or thumbs[0].get("url")
        if image_url:
            break

    if not image_url:
        raise RuntimeError(f"No image URL in Freepik result: {items[0]}")

    logger.info(f"[Freepik] image_url={image_url}")

    # Download the image
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(image_url)
        resp.raise_for_status()
        return base64.b64encode(resp.content).decode()

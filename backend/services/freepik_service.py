"""
Freepik AI Image Generation service.
Uses FLUX.2 Klein (fast, cheap) with fallback to FLUX Pro 1.1.
Async task polling: POST → task_id → GET until COMPLETED.
"""
import asyncio
import base64
import logging
import os

import httpx

logger = logging.getLogger(__name__)

FREEPIK_API_KEY = os.getenv("FREEPIK_API_KEY", "")
BASE = "https://api.freepik.com/v1/ai/text-to-image"


def _headers() -> dict:
    return {
        "x-freepik-api-key": FREEPIK_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def _poll_task(client: httpx.AsyncClient, endpoint: str, task_id: str, max_wait: int = 120) -> dict:
    """Poll GET /{endpoint}/{task_id} until COMPLETED or FAILED."""
    url = f"{BASE}/{endpoint}/{task_id}"
    for _ in range(max_wait // 3):
        await asyncio.sleep(3)
        resp = await client.get(url, headers=_headers(), timeout=15)
        if resp.status_code != 200:
            continue
        data = resp.json()
        # Response may be nested under "data"
        item = data.get("data", data)
        status = item.get("status", "")
        if status == "COMPLETED":
            return item
        if status == "FAILED":
            raise RuntimeError(f"Freepik task failed: {item}")
    raise TimeoutError(f"Freepik task {task_id} did not complete in {max_wait}s")


async def _download_as_b64(url: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return base64.b64encode(resp.content).decode()


async def generate_image_freepik(prompt: str, use_pro: bool = False) -> str:
    """
    Generate image via Freepik AI and return base64 string.
    use_pro=False → FLUX.2 Klein (faster/cheaper)
    use_pro=True  → FLUX Pro 1.1 (higher quality)
    Returns base64 JPEG string, or raises on failure.
    """
    if not FREEPIK_API_KEY:
        raise RuntimeError("FREEPIK_API_KEY not set")

    endpoint = "flux-pro-v1-1" if use_pro else "flux-2-klein"
    payload = {
        "prompt": prompt,
        "aspect_ratio": "square_1_1",
        "output_format": "jpeg",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{BASE}/{endpoint}", json=payload, headers=_headers())
        resp.raise_for_status()
        result = resp.json()

    data = result.get("data", result)
    task_id = data.get("task_id") or data.get("id")

    if not task_id:
        raise RuntimeError(f"No task_id in Freepik response: {result}")

    logger.info(f"[Freepik] task_id={task_id} endpoint={endpoint}")

    # Poll for completion
    async with httpx.AsyncClient(timeout=15) as client:
        completed = await _poll_task(client, endpoint, task_id)

    # Extract image URL — try common response shapes
    image_url = (
        completed.get("image_url")
        or completed.get("url")
        or (completed.get("images") or [{}])[0].get("url")
        or (completed.get("result") or {}).get("url")
    )

    if not image_url:
        # Maybe base64 is directly in response
        b64 = (
            completed.get("base64")
            or (completed.get("images") or [{}])[0].get("base64")
        )
        if b64:
            return b64
        raise RuntimeError(f"No image URL in completed Freepik task: {completed}")

    logger.info(f"[Freepik] image_url={image_url}")
    return await _download_as_b64(image_url)

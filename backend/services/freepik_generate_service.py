"""
Freepik AI text-to-image generation service.
Supports Z-Image Turbo and Flux Pro 1.1 models.
Both use async polling: POST → task_id → GET until COMPLETED.
Returns base64 JPEG string.
"""
import asyncio
import base64
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.freepik.com/v1/ai/text-to-image"
_POLL_INTERVAL = 3  # seconds between status checks
_MAX_POLLS = 30    # max 90 seconds


def _headers() -> dict:
    api_key = os.getenv("FREEPIK_API_KEY", "")
    return {
        "x-freepik-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _extract_image_url(data: dict) -> str | None:
    """
    Extract image URL from completed task response.
    Tries multiple known response shapes.
    """
    # Common patterns in Freepik API responses
    generated = data.get("generated") or data.get("data", {}).get("generated") or []
    if isinstance(generated, list) and generated:
        item = generated[0]
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            return item.get("url") or item.get("source", {}).get("url")

    # Try top-level url
    for key in ("url", "image_url", "result_url"):
        val = data.get(key) or data.get("data", {}).get(key)
        if val:
            return val

    # Try nested result
    result = data.get("result") or data.get("data", {}).get("result")
    if isinstance(result, dict):
        return result.get("url")
    if isinstance(result, list) and result:
        return result[0].get("url") if isinstance(result[0], dict) else result[0]

    return None


async def _poll_task(client: httpx.AsyncClient, endpoint: str, task_id: str) -> str:
    """Poll task status until COMPLETED, return image URL."""
    url = f"{endpoint}/{task_id}"
    for attempt in range(_MAX_POLLS):
        await asyncio.sleep(_POLL_INTERVAL)
        resp = await client.get(url, headers=_headers())
        if resp.status_code != 200:
            logger.warning(f"[Freepik] poll {task_id} status={resp.status_code}")
            continue

        data = resp.json()
        logger.debug(f"[Freepik] poll attempt={attempt} data={data}")

        # Extract status from various response shapes
        status = (
            data.get("status")
            or data.get("data", {}).get("status")
            or (data.get("data") or [{}])[0].get("status") if isinstance(data.get("data"), list) else None
        )

        if status == "COMPLETED":
            image_url = _extract_image_url(data)
            if image_url:
                return image_url
            logger.error(f"[Freepik] COMPLETED but no image URL. Full response: {data}")
            raise RuntimeError(f"Freepik task {task_id} completed but no image URL found")

        if status == "FAILED":
            raise RuntimeError(f"Freepik task {task_id} FAILED: {data}")

        logger.info(f"[Freepik] task {task_id} status={status}, waiting...")

    raise RuntimeError(f"Freepik task {task_id} timed out after {_MAX_POLLS * _POLL_INTERVAL}s")


async def _download_image(url: str) -> str:
    """Download image from URL and return base64 string."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return base64.b64encode(resp.content).decode()


async def generate_image_zimage(prompt: str) -> str:
    """
    Generate image using Freepik Z-Image Turbo.
    Returns base64 JPEG string.
    """
    api_key = os.getenv("FREEPIK_API_KEY", "")
    if not api_key:
        raise RuntimeError("FREEPIK_API_KEY not set")

    endpoint = f"{_BASE}/z-image"
    payload = {
        "prompt": prompt[:4096],
        "image_size": "landscape_4_3",
        "output_format": "jpeg",
        "num_inference_steps": 8,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(endpoint, headers=_headers(), json=payload)
        logger.info(f"[Freepik Z-Image] POST status={resp.status_code} body={resp.text[:500]}")
        resp.raise_for_status()
        data = resp.json()

    task_id = (
        data.get("task_id")
        or data.get("data", {}).get("task_id")
    )
    if not task_id:
        raise RuntimeError(f"Freepik Z-Image: no task_id in response: {data}")

    logger.info(f"[Freepik Z-Image] task_id={task_id}, polling...")

    async with httpx.AsyncClient(timeout=60) as client:
        image_url = await _poll_task(client, endpoint, task_id)

    logger.info(f"[Freepik Z-Image] got image_url={image_url}")
    return await _download_image(image_url)


async def generate_image_flux(prompt: str) -> str:
    """
    Generate image using Freepik Flux Pro 1.1.
    Returns base64 JPEG string.
    """
    api_key = os.getenv("FREEPIK_API_KEY", "")
    if not api_key:
        raise RuntimeError("FREEPIK_API_KEY not set")

    endpoint = f"{_BASE}/flux-pro-v1-1"
    payload = {
        "prompt": prompt[:4096],
        "aspect_ratio": "widescreen_16_9",
        "output_format": "jpeg",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(endpoint, headers=_headers(), json=payload)
        logger.info(f"[Freepik Flux] POST status={resp.status_code} body={resp.text[:500]}")
        resp.raise_for_status()
        data = resp.json()

    task_id = (
        data.get("task_id")
        or data.get("data", {}).get("task_id")
    )
    if not task_id:
        raise RuntimeError(f"Freepik Flux: no task_id in response: {data}")

    logger.info(f"[Freepik Flux] task_id={task_id}, polling...")

    async with httpx.AsyncClient(timeout=120) as client:
        image_url = await _poll_task(client, endpoint, task_id)

    logger.info(f"[Freepik Flux] got image_url={image_url}")
    return await _download_image(image_url)

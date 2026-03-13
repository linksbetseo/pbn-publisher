"""
Freepik AI text-to-image generation service.
Supports Z-Image Turbo and Flux Pro 1.1 models.

API response shape (both models, GET /{task-id}):
  {"data": {"task_id": "...", "status": "COMPLETED", "generated": ["https://..."]}}

POST response shape:
  {"data": {"task_id": "...", "status": "IN_PROGRESS", "generated": []}}
"""
import asyncio
import base64
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.freepik.com/v1/ai/text-to-image"
_POLL_INTERVAL = 4   # seconds between status checks
_MAX_POLLS = 25      # max 100 seconds


def _headers() -> dict:
    api_key = os.getenv("FREEPIK_API_KEY", "")
    return {
        "x-freepik-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def _poll_task(client: httpx.AsyncClient, endpoint: str, task_id: str) -> str:
    """Poll GET /{task-id} until COMPLETED. Returns image URL."""
    url = f"{endpoint}/{task_id}"
    for attempt in range(_MAX_POLLS):
        await asyncio.sleep(_POLL_INTERVAL)
        resp = await client.get(url, headers=_headers())
        if resp.status_code != 200:
            logger.warning(f"[Freepik] poll {task_id} http={resp.status_code} body={resp.text[:200]}")
            continue

        body = resp.json()
        # Response: {"data": {"task_id": "...", "status": "...", "generated": [...]}}
        task_data = body.get("data", {})
        status = task_data.get("status", "")
        logger.info(f"[Freepik] poll attempt={attempt} task_id={task_id} status={status}")

        if status == "COMPLETED":
            generated = task_data.get("generated", [])
            if generated and isinstance(generated, list) and generated[0]:
                return generated[0]
            logger.error(f"[Freepik] COMPLETED but generated empty: {body}")
            raise RuntimeError(f"Freepik task {task_id} completed but no image URL in generated[]")

        if status in ("FAILED", "CANCELLED", "ERROR"):
            raise RuntimeError(f"Freepik task {task_id} {status}: {body}")

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

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(endpoint, headers=_headers(), json=payload)
        logger.info(f"[Freepik Z-Image] POST status={resp.status_code} body={resp.text[:300]}")
        resp.raise_for_status()
        body = resp.json()

    # POST response: {"data": {"task_id": "...", "status": "IN_PROGRESS", "generated": []}}
    task_id = body.get("data", {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"Freepik Z-Image: no task_id in response: {body}")

    logger.info(f"[Freepik Z-Image] task_id={task_id}, polling...")

    async with httpx.AsyncClient(timeout=120) as client:
        image_url = await _poll_task(client, endpoint, task_id)

    logger.info(f"[Freepik Z-Image] image_url={image_url}")
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

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(endpoint, headers=_headers(), json=payload)
        logger.info(f"[Freepik Flux] POST status={resp.status_code} body={resp.text[:300]}")
        resp.raise_for_status()
        body = resp.json()

    task_id = body.get("data", {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"Freepik Flux: no task_id in response: {body}")

    logger.info(f"[Freepik Flux] task_id={task_id}, polling...")

    async with httpx.AsyncClient(timeout=150) as client:
        image_url = await _poll_task(client, endpoint, task_id)

    logger.info(f"[Freepik Flux] image_url={image_url}")
    return await _download_image(image_url)

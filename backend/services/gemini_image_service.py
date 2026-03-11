"""
Gemini image generation service.
Tries multiple models in order until one works.
Returns base64 JPEG string.
"""
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_GEMINI_MODELS = [
    "gemini-2.0-flash-preview-image-generation",
    "gemini-2.0-flash-exp",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
]
_BASE = "https://generativelanguage.googleapis.com/v1beta/models/"


async def generate_image_gemini(prompt: str) -> str:
    """
    Generate an image with Gemini. Tries multiple models in order.
    Returns base64-encoded JPEG string, or raises on failure.
    """
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }

    last_error = ""
    async with httpx.AsyncClient(timeout=60) as client:
        for model in _GEMINI_MODELS:
            url = f"{_BASE}{model}:generateContent"
            resp = await client.post(url, params={"key": api_key}, json=payload)
            if resp.status_code != 200:
                last_error = f"{model}: {resp.status_code}"
                logger.warning(f"[Gemini] {last_error}")
                continue

            data = resp.json()
            for candidate in data.get("candidates", []):
                for part in candidate.get("content", {}).get("parts", []):
                    inline = part.get("inlineData")
                    if inline:
                        b64_data = inline.get("data", "")
                        if b64_data:
                            logger.info(f"[Gemini] image OK model={model}, size={len(b64_data)}")
                            return b64_data
            last_error = f"{model}: no image in response"

    raise RuntimeError(f"Gemini image failed all models. Last: {last_error}")

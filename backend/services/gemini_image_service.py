"""
Gemini image generation service.
Uses gemini-2.0-flash-exp (cheapest option with native image output).
Returns base64 JPEG string.
"""
import logging
import os

import httpx

logger = logging.getLogger(__name__)
GEMINI_IMAGE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "imagen-3.0-generate-002:predict"
)


async def generate_image_gemini(prompt: str) -> str:
    """
    Generate an image with Gemini 2.0 Flash.
    Returns base64-encoded JPEG string, or raises on failure.
    """
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    payload = {
        "instances": [{"prompt": prompt}],
        "parameters": {"sampleCount": 1, "aspectRatio": "16:9"},
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            GEMINI_IMAGE_URL,
            params={"key": api_key},
            json=payload,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Gemini image API error {resp.status_code}: {resp.text[:300]}")

        data = resp.json()

    # Extract base64 image from Imagen response
    for prediction in data.get("predictions", []):
        b64_data = prediction.get("bytesBase64Encoded", "")
        if b64_data:
            logger.info(f"[Gemini/Imagen] image generated, size={len(b64_data)} chars")
            return b64_data

    raise RuntimeError(f"No image in Imagen response: {str(data)[:300]}")

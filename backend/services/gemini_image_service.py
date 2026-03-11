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
    "gemini-2.0-flash-preview-image-generation:generateContent"
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
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
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

    # Extract inline image data from response
    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            inline = part.get("inlineData")
            if inline:
                mime = inline.get("mimeType", "image/jpeg")
                b64_data = inline.get("data", "")
                if b64_data:
                    logger.info(f"[Gemini] image generated, mime={mime}, size={len(b64_data)} chars")
                    # Return as-is if already jpeg, else return raw b64
                    return b64_data

    raise RuntimeError(f"No image in Gemini response: {str(data)[:300]}")

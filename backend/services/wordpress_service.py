import base64
import io
from typing import Optional
import httpx


def _auth_header(wp_login: str, wp_pass: str) -> str:
    token = base64.b64encode(f"{wp_login}:{wp_pass}".encode()).decode()
    return f"Basic {token}"


async def _upload_image(
    client: httpx.AsyncClient,
    base_url: str,
    auth: str,
    image_b64: str,
    filename: str = "featured.jpg",
) -> Optional[int]:
    image_data = base64.b64decode(image_b64)
    headers = {
        "Authorization": auth,
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": "image/jpeg",
    }
    resp = await client.post(
        f"{base_url}/wp-json/wp/v2/media",
        content=image_data,
        headers=headers,
        timeout=60,
    )
    if resp.status_code in (200, 201):
        return resp.json().get("id")
    return None


async def publish_post(
    domain: str,
    wp_login: str,
    wp_pass: str,
    title: str,
    content: str,
    image_b64: Optional[str] = None,
) -> dict:
    auth = _auth_header(wp_login, wp_pass)
    headers = {
        "Authorization": auth,
        "Content-Type": "application/json",
    }

    urls_to_try = []
    if domain.startswith("http"):
        urls_to_try.append(domain.rstrip("/"))
    else:
        urls_to_try.append(f"https://{domain}")
        urls_to_try.append(f"http://{domain}")

    last_error = None
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        for base_url in urls_to_try:
            try:
                media_id = None
                if image_b64:
                    try:
                        media_id = await _upload_image(client, base_url, auth, image_b64)
                    except Exception as img_err:
                        print(f"Image upload failed for {base_url}: {img_err}")

                post_data = {
                    "title": title,
                    "content": content,
                    "status": "publish",
                }
                if media_id:
                    post_data["featured_media"] = media_id

                resp = await client.post(
                    f"{base_url}/wp-json/wp/v2/posts",
                    json=post_data,
                    headers=headers,
                    timeout=30,
                )

                if resp.status_code in (200, 201):
                    data = resp.json()
                    return {
                        "success": True,
                        "url": data.get("link", ""),
                        "post_id": data.get("id"),
                    }
                else:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except httpx.ConnectError as e:
                last_error = f"Connection error: {e}"
                continue
            except Exception as e:
                last_error = str(e)
                continue

    return {"success": False, "url": "", "post_id": None, "error": last_error}

import base64
import io
import re
from typing import Optional
import httpx


def _auth_header(wp_login: str, wp_pass: str) -> str:
    token = base64.b64encode(f"{wp_login}:{wp_pass}".encode()).decode()
    return f"Basic {token}"


def _base_url(domain: str) -> list[str]:
    if domain.startswith("http"):
        return [domain.rstrip("/")]
    return [f"https://{domain}", f"http://{domain}"]


async def get_or_create_category(
    domain: str,
    wp_login: str,
    wp_pass: str,
    name: str,
    slug: str = "",
) -> Optional[int]:
    """
    Pobiera ID istniejącej kategorii WP lub tworzy nową.
    Zwraca category_id lub None przy błędzie.
    """
    auth = _auth_header(wp_login, wp_pass)
    headers = {"Authorization": auth, "Content-Type": "application/json"}

    # Generuj slug z nazwy jeśli nie podano
    if not slug:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower().strip()).strip("-")

    async with httpx.AsyncClient(verify=False, timeout=20) as client:
        for base in _base_url(domain):
            try:
                # Sprawdź czy kategoria już istnieje (po slug)
                resp = await client.get(
                    f"{base}/wp-json/wp/v2/categories",
                    params={"slug": slug, "per_page": 1},
                    headers=headers,
                )
                if resp.status_code == 200:
                    existing = resp.json()
                    if existing:
                        return existing[0]["id"]

                # Utwórz nową kategorię
                resp = await client.post(
                    f"{base}/wp-json/wp/v2/categories",
                    json={"name": name, "slug": slug},
                    headers=headers,
                )
                if resp.status_code in (200, 201):
                    return resp.json().get("id")
            except Exception:
                continue
    return None


async def get_categories(domain: str, wp_login: str, wp_pass: str) -> list[dict]:
    """Pobiera listę wszystkich kategorii z WP."""
    auth = _auth_header(wp_login, wp_pass)
    headers = {"Authorization": auth}
    result = []
    async with httpx.AsyncClient(verify=False, timeout=20) as client:
        for base in _base_url(domain):
            try:
                resp = await client.get(
                    f"{base}/wp-json/wp/v2/categories",
                    params={"per_page": 100},
                    headers=headers,
                )
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                continue
    return result


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
    category_id: Optional[int] = None,
    excerpt: Optional[str] = None,
) -> dict:
    auth = _auth_header(wp_login, wp_pass)
    headers = {
        "Authorization": auth,
        "Content-Type": "application/json",
    }

    urls_to_try = _base_url(domain)

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
                if category_id:
                    post_data["categories"] = [category_id]
                if excerpt:
                    post_data["excerpt"] = excerpt

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

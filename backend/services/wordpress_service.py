import base64
import re
import unicodedata
from typing import Optional
import httpx


def _auth_header(wp_login: str, wp_pass: str) -> str:
    token = base64.b64encode(f"{wp_login}:{wp_pass}".encode()).decode()
    return f"Basic {token}"


def _base_url(domain: str) -> list[str]:
    if domain.startswith("http"):
        return [domain.rstrip("/")]
    return [f"https://{domain}", f"http://{domain}"]


def _http_auth(http_user: str, http_pass: str):
    """Return httpx BasicAuth tuple if htpasswd credentials provided."""
    if http_user and http_pass:
        return (http_user, http_pass)
    return None


async def get_or_create_category(
    domain: str,
    wp_login: str,
    wp_pass: str,
    name: str,
    slug: str = "",
    http_user: str = "",
    http_pass: str = "",
) -> Optional[int]:
    """
    Pobiera ID istniejącej kategorii WP lub tworzy nową.
    Zwraca category_id lub None przy błędzie.
    """
    auth = _auth_header(wp_login, wp_pass)
    headers = {"Authorization": auth, "Content-Type": "application/json"}
    site_auth = _http_auth(http_user, http_pass)

    # Generuj slug z nazwy jeśli nie podano
    if not slug:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower().strip()).strip("-")

    async with httpx.AsyncClient(verify=False, timeout=20, auth=site_auth) as client:
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


async def get_categories(domain: str, wp_login: str, wp_pass: str, http_user: str = "", http_pass: str = "") -> list[dict]:
    """Pobiera listę wszystkich kategorii z WP."""
    auth = _auth_header(wp_login, wp_pass)
    headers = {"Authorization": auth}
    site_auth = _http_auth(http_user, http_pass)
    result = []
    async with httpx.AsyncClient(verify=False, timeout=20, auth=site_auth) as client:
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


def _keyword_to_slug(keyword: str) -> str:
    """Convert keyword to SEO-friendly WP slug."""
    nfkd = unicodedata.normalize("NFKD", keyword.lower())
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_str).strip("-")
    return slug[:80]


async def _upload_image(
    client: httpx.AsyncClient,
    base_url: str,
    auth: str,
    image_b64: str,
    filename: str = "featured.jpg",
    alt_text: str = "",
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
        media_id = resp.json().get("id")
        # Set alt text, title, caption for SEO
        if media_id and alt_text:
            try:
                await client.post(
                    f"{base_url}/wp-json/wp/v2/media/{media_id}",
                    json={"alt_text": alt_text, "title": alt_text, "caption": alt_text},
                    headers={"Authorization": auth, "Content-Type": "application/json"},
                    timeout=10,
                )
            except Exception:
                pass
        return media_id
    return None


async def _ping_sitemaps(base_url: str, site_auth) -> None:
    """Ping Google and Bing with updated sitemap after publishing."""
    sitemap_url = f"{base_url}/sitemap.xml"
    ping_urls = [
        f"https://www.google.com/ping?sitemap={sitemap_url}",
        f"https://www.bing.com/ping?sitemap={sitemap_url}",
    ]
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as ping_client:
            for url in ping_urls:
                try:
                    await ping_client.get(url)
                except Exception:
                    pass
    except Exception:
        pass


async def publish_post(
    domain: str,
    wp_login: str,
    wp_pass: str,
    title: str,
    content: str,
    image_b64: Optional[str] = None,
    category_id: Optional[int] = None,
    excerpt: Optional[str] = None,
    keyword: Optional[str] = None,
    tags: Optional[list] = None,
    http_user: str = "",
    http_pass: str = "",
) -> dict:
    auth = _auth_header(wp_login, wp_pass)
    headers = {
        "Authorization": auth,
        "Content-Type": "application/json",
    }
    site_auth = _http_auth(http_user, http_pass)

    # SEO slug from keyword
    slug = _keyword_to_slug(keyword) if keyword else _keyword_to_slug(title)
    alt_text = keyword or title
    image_filename = f"{slug[:50]}.jpg" if slug else "featured.jpg"

    urls_to_try = _base_url(domain)

    last_error = None
    async with httpx.AsyncClient(verify=False, timeout=30, auth=site_auth) as client:
        for base_url in urls_to_try:
            try:
                media_id = None
                if image_b64:
                    try:
                        media_id = await _upload_image(
                            client, base_url, auth, image_b64,
                            filename=image_filename,
                            alt_text=alt_text,
                        )
                    except Exception as img_err:
                        print(f"Image upload failed for {base_url}: {img_err}")

                post_data = {
                    "title": title,
                    "content": content,
                    "status": "publish",
                    "slug": slug,
                }
                if media_id:
                    post_data["featured_media"] = media_id
                if category_id:
                    post_data["categories"] = [category_id]
                if excerpt:
                    post_data["excerpt"] = excerpt
                if tags:
                    tag_ids = await _get_or_create_tags(client, base_url, auth, tags)
                    if tag_ids:
                        post_data["tags"] = tag_ids
                # SEO meta — Yoast, RankMath, All-in-One SEO compatible
                meta = {}
                if excerpt:
                    meta.update({
                        "_yoast_wpseo_metadesc": excerpt[:160],
                        "_yoast_wpseo_title": title,
                        "rank_math_description": excerpt[:160],
                        "rank_math_title": title,
                        "_aioseop_description": excerpt[:160],
                        "_aioseop_title": title,
                        # Open Graph
                        "_yoast_wpseo_opengraph-title": title,
                        "_yoast_wpseo_opengraph-description": excerpt[:200],
                        "rank_math_facebook_title": title,
                        "rank_math_facebook_description": excerpt[:200],
                        # Twitter Card
                        "_yoast_wpseo_twitter-title": title,
                        "_yoast_wpseo_twitter-description": excerpt[:200],
                    })
                if keyword:
                    meta["_yoast_wpseo_focuskw"] = keyword
                    # RankMath supports comma-separated focus keywords
                    kw_with_lsi = keyword
                    if tags:
                        kw_with_lsi = ",".join([keyword] + list(tags[:4]))
                    meta["rank_math_focus_keyword"] = kw_with_lsi
                if meta:
                    post_data["meta"] = meta

                resp = await client.post(
                    f"{base_url}/wp-json/wp/v2/posts",
                    json=post_data,
                    headers=headers,
                    timeout=30,
                )

                if resp.status_code in (200, 201):
                    data = resp.json()
                    post_url = data.get("link", "")
                    post_id = data.get("id")
                    # Set canonical after publish — use actual WP post URL (correct scheme)
                    if post_url and post_id and meta is not None:
                        canonical = post_url.rstrip("/") + "/"
                        try:
                            await client.post(
                                f"{base_url}/wp-json/wp/v2/posts/{post_id}",
                                json={"meta": {
                                    "_yoast_wpseo_canonical": canonical,
                                    "rank_math_canonical_url": canonical,
                                }},
                                headers=headers,
                                timeout=10,
                            )
                        except Exception:
                            pass
                    # Ping sitemaps asynchronously (non-blocking)
                    import asyncio as _asyncio
                    _asyncio.create_task(_ping_sitemaps(base_url, site_auth))
                    return {
                        "success": True,
                        "url": post_url,
                        "post_id": post_id,
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


async def _get_or_create_tags(
    client: httpx.AsyncClient,
    base_url: str,
    auth: str,
    tag_names: list[str],
) -> list[int]:
    """Get or create WP tags, return list of IDs. Uses one bulk GET to minimize API calls."""
    if not tag_names:
        return []
    tags = tag_names[:5]
    headers = {"Authorization": auth, "Content-Type": "application/json"}
    slugs = [_keyword_to_slug(n) for n in tags]

    # Single GET for all slugs at once
    existing: dict[str, int] = {}
    try:
        resp = await client.get(
            f"{base_url}/wp-json/wp/v2/tags",
            params={"slug": ",".join(slugs), "per_page": 10},
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 200:
            for t in resp.json():
                existing[t.get("slug", "")] = t["id"]
    except Exception:
        pass

    ids = []
    for name, slug in zip(tags, slugs):
        if slug in existing:
            ids.append(existing[slug])
            continue
        try:
            resp2 = await client.post(
                f"{base_url}/wp-json/wp/v2/tags",
                json={"name": name, "slug": slug},
                headers=headers,
                timeout=10,
            )
            if resp2.status_code in (200, 201):
                ids.append(resp2.json().get("id"))
        except Exception:
            pass
    return [i for i in ids if i]


async def check_wp_credentials(domain: str, wp_login: str, wp_pass: str, http_user: str = "", http_pass: str = "") -> bool:
    """Quick WP REST API credentials check. Returns True if valid."""
    auth = _auth_header(wp_login, wp_pass)
    site_auth = _http_auth(http_user, http_pass)
    async with httpx.AsyncClient(verify=False, timeout=10, auth=site_auth) as client:
        for base in _base_url(domain):
            try:
                resp = await client.get(
                    f"{base}/wp-json/wp/v2/users/me",
                    headers={"Authorization": auth},
                )
                if resp.status_code == 200:
                    return True
            except Exception:
                continue
    return False

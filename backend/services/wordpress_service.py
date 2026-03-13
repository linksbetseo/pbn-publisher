import asyncio
import base64
import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Optional, Tuple
import httpx

logger = logging.getLogger(__name__)

# NOTE: verify=False is used for WP connections because many PBN domains use
# self-signed or misconfigured SSL. This is intentional for this use case.
logger.info("[WP] SSL verification disabled for WordPress connections (verify=False).")


def _get_plain_pass(wp_pass: str) -> str:
    """Transparently decrypt wp_pass if it is Fernet-encrypted, otherwise return as-is."""
    from services.crypto_service import get_plain_password
    return get_plain_password(wp_pass)


def _auth_header(wp_login: str, wp_pass: str) -> str:
    plain = _get_plain_pass(wp_pass)
    token = base64.b64encode(f"{wp_login}:{plain}".encode()).decode()
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
    """Convert keyword to SEO-friendly WP slug. SEO #16: max 5 words, not 80 chars."""
    nfkd = unicodedata.normalize("NFKD", keyword.lower())
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_str).strip("-")
    # SEO #16: limit to 5 words (Google prefers short keyword-focused slugs)
    parts = slug.split("-")
    if len(parts) > 5:
        slug = "-".join(parts[:5])
    # Still cap at 80 as safety net
    slug = slug[:80]
    if len(slug) == 80 and "-" in slug:
        slug = slug.rsplit("-", 1)[0]
    return slug


async def _upload_image(
    client: httpx.AsyncClient,
    base_url: str,
    auth: str,
    image_b64: str,
    filename: str = "featured.jpg",
    alt_text: str = "",
) -> Tuple[Optional[int], str]:
    """FIX #43: return type now correctly Tuple[Optional[int], str] (was Optional[int])."""
    image_data = base64.b64decode(image_b64)
    # SEO #35: convert to WebP if Pillow available (30% smaller → better CWV)
    _content_type = "image/jpeg"
    try:
        from PIL import Image
        import io as _io
        _img = Image.open(_io.BytesIO(image_data))
        _webp_buf = _io.BytesIO()
        _img.save(_webp_buf, format="WEBP", quality=82, method=4)
        image_data = _webp_buf.getvalue()
        _content_type = "image/webp"
        if not filename.endswith(".webp"):
            filename = filename.rsplit(".", 1)[0] + ".webp"
    except Exception:
        pass  # Pillow not installed or conversion failed — use original JPEG
    headers = {
        "Authorization": auth,
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": _content_type,
    }
    resp = await client.post(
        f"{base_url}/wp-json/wp/v2/media",
        content=image_data,
        headers=headers,
        timeout=60,
    )
    if resp.status_code in (200, 201):
        media_json = resp.json()
        media_id = media_json.get("id")
        source_url = media_json.get("source_url", "")
        # FIX #72: set alt text, title, caption for SEO — use language-aware suffix
        if media_id and alt_text:
            try:
                descriptive_alt = alt_text
                # SEO #90: set image dimensions for CLS prevention
                _img_meta = {"alt_text": descriptive_alt, "title": alt_text[:60], "caption": ""}
                try:
                    from PIL import Image as _PILImg
                    import io as _imgIO
                    _pil = _PILImg.open(_imgIO.BytesIO(image_data))
                    _img_meta["width"] = _pil.width
                    _img_meta["height"] = _pil.height
                except Exception:
                    pass
                await client.post(
                    f"{base_url}/wp-json/wp/v2/media/{media_id}",
                    json=_img_meta,
                    headers={"Authorization": auth, "Content-Type": "application/json"},
                    timeout=10,
                )
            except Exception:
                pass
        return media_id, source_url
    return None, ""


# SEO #55: IndexNow key file check cache (avoid re-checking every publish)
_indexnow_key_cache: dict[str, bool] = {}

# SEO #93: per-domain SEO plugin detection cache (anti-fingerprint)
_seo_plugin_cache: dict[str, str] = {}


async def _detect_seo_plugin(
    base_url: str, auth: str, http_user: str = "", http_pass: str = ""
) -> str:
    """Detect which SEO plugin is active on the target WP site.

    Checks REST API endpoints for RankMath, Yoast and AIOSEO.
    Returns one of: 'yoast', 'rankmath', 'aioseo', or 'yoast' as default.
    Results are cached per domain to avoid repeated requests.
    """
    if base_url in _seo_plugin_cache:
        return _seo_plugin_cache[base_url]
    site_auth = (http_user, http_pass) if http_user and http_pass else None
    plugin = "yoast"  # default fallback
    try:
        async with httpx.AsyncClient(timeout=5, verify=False, auth=site_auth) as client:
            headers = {"Authorization": auth}
            for name, path in [
                ("rankmath", "/wp-json/rankmath/v1/"),
                ("yoast", "/wp-json/yoast/v1/"),
                ("aioseo", "/wp-json/aioseo/v1/"),
            ]:
                try:
                    r = await client.get(f"{base_url}{path}", headers=headers)
                    if r.status_code in (200, 301, 401, 403):
                        plugin = name
                        break
                except Exception:
                    continue
    except Exception:
        pass
    _seo_plugin_cache[base_url] = plugin
    return plugin

async def _ping_sitemaps(base_url: str, site_auth, post_url: str = "") -> None:
    """Notify search engines about new/updated content via IndexNow + Bing sitemap ping."""
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as ping_client:
            # IndexNow — instant indexing for Bing, Yandex, Seznam, Naver
            if post_url:
                clean_domain = base_url.replace("https://", "").replace("http://", "").rstrip("/")
                indexnow_key = clean_domain.replace(".", "")[:32]
                # Validate key file exists on domain (required by IndexNow protocol)
                key_file_url = f"{base_url}/{indexnow_key}.txt"
                # SEO #55: use cache for key file check
                if key_file_url in _indexnow_key_cache:
                    key_file_ok = _indexnow_key_cache[key_file_url]
                else:
                    key_file_ok = False
                    try:
                        kf_resp = await ping_client.get(key_file_url)
                        key_file_ok = kf_resp.status_code == 200 and indexnow_key in (kf_resp.text or "")
                    except Exception:
                        pass
                    _indexnow_key_cache[key_file_url] = key_file_ok
                if not key_file_ok:
                    logger.warning(
                        f"[IndexNow] Key file missing or invalid at {key_file_url} — "
                        f"create a file '{indexnow_key}.txt' containing '{indexnow_key}' in WP root"
                    )
                indexnow_payload = {
                    "host": clean_domain,
                    "key": indexnow_key,
                    "urlList": [post_url],
                }
                for endpoint in [
                    "https://api.indexnow.org/indexnow",
                    "https://www.bing.com/indexnow",
                ]:
                    try:
                        resp = await ping_client.post(
                            endpoint,
                            json=indexnow_payload,
                            headers={"Content-Type": "application/json"},
                        )
                        if resp.status_code in (200, 202):
                            logger.info(f"[IndexNow] Submitted {post_url} to {endpoint}")
                    except Exception:
                        pass
            # Note: Google sitemap ping deprecated 2023 — only Bing remains
            # SEO #18: cache-bust sitemap URL with timestamp
            import time as _time
            sitemap_url = f"{base_url}/sitemap.xml?t={int(_time.time())}"
            try:
                await ping_client.get(f"https://www.bing.com/ping?sitemap={sitemap_url}")
            except Exception:
                pass
            # SEO #17: Pingomatic ping for broader reach (some PBN hosts disable IndexNow)
            try:
                _ping_body = (
                    '<?xml version="1.0"?>'
                    '<methodCall><methodName>weblogUpdates.ping</methodName>'
                    f'<params><param><value>{base_url}</value></param>'
                    f'<param><value>{base_url}/sitemap.xml</value></param>'
                    '</params></methodCall>'
                )
                await ping_client.post(
                    "https://rpc.pingomatic.com/",
                    content=_ping_body,
                    headers={"Content-Type": "text/xml"},
                )
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
    # SEO #20: vary alt text — not identical to title (Google image search)
    _alt_variations_pl = ["ilustracja", "grafika", "zdjęcie", "obraz"]
    _alt_variations_en = ["illustration", "image", "photo", "graphic"]
    import random as _rnd
    _alt_suffix = _rnd.choice(_alt_variations_pl) if (keyword and any(c in keyword for c in "ąćęłńóśźż")) else _rnd.choice(_alt_variations_en)
    # SEO #56: alt text includes year for freshness signal in image search
    _year = datetime.now(timezone.utc).year
    alt_text = f"{keyword or title} — {_alt_suffix} {_year}"
    # SEO #35: prefer WebP format for smaller files and better Core Web Vitals
    _use_webp = True
    try:
        from PIL import Image
        import io as _io
    except ImportError:
        _use_webp = False
    image_filename = f"{slug[:50]}.webp" if _use_webp else f"{slug[:50]}.jpg"

    # Excerpt fallback — strip HTML from content intro if excerpt is empty/bad
    # SEO #15: cut at sentence boundary, not mid-word
    if not excerpt or len(excerpt.strip()) < 20:
        _plain = re.sub(r'<[^>]+>', ' ', content or "")
        _plain = re.sub(r'\s+', ' ', _plain).strip()
        if _plain:
            # Try to cut at sentence boundary within 155 chars
            _sentences = re.split(r'(?<=[.!?])\s+', _plain[:200])
            _excerpt_build = ""
            for _s in _sentences:
                if len(_excerpt_build) + len(_s) + 1 <= 155:
                    _excerpt_build = (_excerpt_build + " " + _s).strip()
                else:
                    break
            if len(_excerpt_build) >= 50:
                excerpt = _excerpt_build
            else:
                parts = _plain[:155].rsplit(' ', 1)
                excerpt = (parts[0] if len(parts) > 1 else _plain[:155]) + "..."

    urls_to_try = _base_url(domain)

    last_error = None
    async with httpx.AsyncClient(verify=False, timeout=30, auth=site_auth) as client:
        for base_url in urls_to_try:
            try:
                media_id = None
                og_image_url = ""
                if image_b64:
                    try:
                        media_id, og_image_url = await _upload_image(
                            client, base_url, auth, image_b64,
                            filename=image_filename,
                            alt_text=alt_text,
                        )
                    except Exception as img_err:
                        logger.warning(f"Image upload failed for {base_url}: {img_err}")

                # SEO #6: explicit date for consistent timezone control
                _now_gmt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                post_data = {
                    "title": title,
                    "content": content,
                    "status": "publish",
                    "slug": slug,
                    "date_gmt": _now_gmt,
                    "comment_status": "closed",  # SEO #54: disable comments (spam magnet on PBN)
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
                # SEO #93: detect active SEO plugin to avoid fingerprint
                _seo_plugin = await _detect_seo_plugin(base_url, auth, http_user, http_pass)
                _is_yoast = _seo_plugin in ("yoast", "unknown")
                _is_rankmath = _seo_plugin in ("rankmath", "unknown")
                _is_aioseo = _seo_plugin == "aioseo"

                # SEO meta — only write keys for the detected plugin
                meta = {}
                if excerpt:
                    if _is_yoast:
                        meta["_yoast_wpseo_metadesc"] = excerpt[:160]
                        meta["_yoast_wpseo_title"] = f"{title} %%sep%% %%sitename%%"
                        meta["_yoast_wpseo_opengraph-title"] = title
                        meta["_yoast_wpseo_opengraph-description"] = excerpt[:200]
                        meta["_yoast_wpseo_twitter-title"] = title
                        meta["_yoast_wpseo_twitter-description"] = excerpt[:200]
                        meta["_yoast_wpseo_twitter-card-type"] = "summary_large_image"
                    if _is_rankmath:
                        meta["rank_math_description"] = excerpt[:160]
                        meta["rank_math_title"] = title
                        meta["rank_math_facebook_title"] = title
                        meta["rank_math_facebook_description"] = excerpt[:200]
                        meta["rank_math_twitter_title"] = title
                        meta["rank_math_twitter_description"] = excerpt[:200]
                        meta["rank_math_twitter_card_type"] = "summary_large_image"
                    if _is_aioseo:
                        meta["_aioseop_description"] = excerpt[:160]
                        meta["_aioseop_title"] = title
                    # SEO #8: OG article metadata for Facebook/Pinterest
                    meta["article:published_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
                    meta["article:section"] = keyword or title[:50]
                    if tags:
                        for _ti, _tag in enumerate(tags[:5]):
                            meta[f"article:tag_{_ti}"] = _tag
                if keyword:
                    if _is_yoast:
                        meta["_yoast_wpseo_focuskw"] = keyword
                    if _is_rankmath:
                        kw_with_lsi = keyword
                        if tags:
                            kw_with_lsi = ",".join([keyword] + list(tags[:4]))
                        meta["rank_math_focus_keyword"] = kw_with_lsi
                # OG image from uploaded featured image
                _og_img = og_image_url or f"{base_url}/wp-content/uploads/site-og-default.jpg"
                if _is_yoast:
                    meta["_yoast_wpseo_opengraph-image"] = _og_img
                    meta["_yoast_wpseo_twitter-image"] = _og_img
                if _is_rankmath:
                    meta["rank_math_facebook_image"] = _og_img
                    meta["rank_math_twitter_image"] = _og_img
                if meta:
                    post_data["meta"] = meta

                # Retry up to 2 times on timeout
                resp = None
                for _attempt in range(2):
                    try:
                        resp = await client.post(
                            f"{base_url}/wp-json/wp/v2/posts",
                            json=post_data,
                            headers=headers,
                            timeout=30,
                        )
                        break
                    except httpx.ReadTimeout:
                        if _attempt == 0:
                            # FIX #44: use module-level asyncio import (was importing inside function body)
                            await asyncio.sleep(3)
                            continue
                        raise
                if resp is None:
                    continue

                if resp.status_code in (200, 201):
                    data = resp.json()
                    post_url = data.get("link", "")
                    post_id = data.get("id")
                    # SEO #57: set canonical via meta update (uses post URL after publish)
                    if post_url and post_id:
                        canonical = post_url.rstrip("/") + "/"
                        _canon_meta: dict = {}
                        if _is_yoast:
                            _canon_meta["_yoast_wpseo_canonical"] = canonical
                        if _is_rankmath:
                            _canon_meta["rank_math_canonical_url"] = canonical
                            _canon_meta["rank_math_robots"] = ["index", "follow", "max-snippet:-1", "max-image-preview:large"]
                        if _canon_meta:
                            try:
                                await client.post(
                                    f"{base_url}/wp-json/wp/v2/posts/{post_id}",
                                    json={"meta": _canon_meta},
                                    headers=headers,
                                    timeout=10,
                                )
                            except Exception:
                                pass
                    # FIX #45: use module-level asyncio import (was importing inside function body)
                    asyncio.create_task(_ping_sitemaps(base_url, site_auth, post_url=post_url))
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
    # SEO #19: increased tag limit from 5 to 10 for richer taxonomy
    tags = tag_names[:10]
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
                # FIX #71: log specific auth error for debugging (403=forbidden, 401=bad creds)
                if resp.status_code in (401, 403):
                    logger.warning(f"[WP] Auth check failed for {base}: HTTP {resp.status_code}")
            except Exception:
                continue
    return False

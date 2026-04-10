import asyncio
import base64
import logging
import random
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Optional, Tuple
import httpx

try:
    from PIL import Image as PILImage
    HAS_PILLOW = True
except ImportError:
    PILImage = None  # type: ignore[assignment,misc]
    HAS_PILLOW = False

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

    async with httpx.AsyncClient(verify=False, timeout=20, auth=site_auth, follow_redirects=True) as client:
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
                # BUG-4 FIX: handle race condition — another request created the category first
                if resp.status_code == 400:
                    try:
                        body = resp.json()
                        if body.get("code") == "term_exists" and body.get("data", {}).get("term_id"):
                            return body["data"]["term_id"]
                    except Exception:
                        pass
            except Exception:
                continue
    return None


async def get_categories(domain: str, wp_login: str, wp_pass: str, http_user: str = "", http_pass: str = "") -> list[dict]:
    """Pobiera listę wszystkich kategorii z WP."""
    auth = _auth_header(wp_login, wp_pass)
    headers = {"Authorization": auth}
    site_auth = _http_auth(http_user, http_pass)
    result = []
    async with httpx.AsyncClient(verify=False, timeout=20, auth=site_auth, follow_redirects=True) as client:
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
        if not HAS_PILLOW:
            raise ImportError("Pillow required for WebP conversion")
        import io as _io
        _img = PILImage.open(_io.BytesIO(image_data))
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
                    if not HAS_PILLOW:
                        raise ImportError("Pillow required for image dimensions")
                    import io as _imgIO
                    _pil = PILImage.open(_imgIO.BytesIO(image_data))
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
        # SEO #109: set srcset sizes hint via WP image meta (WP generates srcset automatically from uploaded sizes)
        # No extra work needed — WP REST API auto-generates srcset from uploaded media.
        # The key is uploading high-res images (1792x1024 from DALL-E) so WP has material to create multiple sizes.
        return media_id, source_url
    return None, ""


# SEO #55: IndexNow key file check cache (avoid re-checking every publish)
# FIX: bounded caches — evict oldest when exceeding 500 entries (typical PBN has <200 domains)
_CACHE_MAX = 500
_indexnow_key_cache: dict[str, bool] = {}

# SEO #93: per-domain SEO plugin detection cache (anti-fingerprint)
_seo_plugin_cache: dict[str, str] = {}

# TOC plugin detection cache — True if domain has a WP TOC plugin (avoid double TOC)
_toc_plugin_cache: dict[str, bool] = {}


def _cache_put(cache: dict, key, value, max_size: int = _CACHE_MAX):
    """Add to cache with LRU-like eviction: drop first entries when full."""
    if len(cache) >= max_size:
        # Remove oldest ~10% entries
        to_remove = list(cache.keys())[:max(1, max_size // 10)]
        for k in to_remove:
            cache.pop(k, None)
    cache[key] = value


async def detect_toc_plugin(
    domain: str, wp_login: str, wp_pass: str,
    http_user: str = "", http_pass: str = ""
) -> bool:
    """Check if a WP domain has an active Table of Contents plugin.

    Checks for Easy TOC, LuckyWP TOC, Rank Math TOC, and TOC+ by querying
    the WP plugins list or checking for known REST endpoints / shortcodes.
    FIX: uses _base_url() for HTTP/HTTPS fallback (was hardcoded https://).
    """
    # FIX: use _base_url() for HTTP fallback (some PBN domains don't have SSL)
    cache_key = domain.replace("https://", "").replace("http://", "").rstrip("/")
    if cache_key in _toc_plugin_cache:
        return _toc_plugin_cache[cache_key]
    auth = _auth_header(wp_login, wp_pass)
    site_auth = (http_user, http_pass) if http_user and http_pass else None
    has_toc = False
    try:
        async with httpx.AsyncClient(timeout=6, verify=False, auth=site_auth) as client:
            headers = {"Authorization": auth}
            for base_url in _base_url(domain):
                # Method 1: check installed plugins list
                try:
                    r = await client.get(f"{base_url}/wp-json/wp/v2/plugins", headers=headers)
                    if r.status_code == 200:
                        plugins = r.json()
                        toc_slugs = ["ez-toc", "table-of-contents", "luckywp-table-of-contents",
                                     "easy-table-of-contents", "toc-plus", "joli-table-of-contents",
                                     "flavor-flavor-toc", "rich-table-of-contents"]
                        for p in plugins:
                            slug = p.get("textdomain", "") or p.get("plugin", "")
                            if any(ts in slug.lower() for ts in toc_slugs):
                                if p.get("status") == "active":
                                    has_toc = True
                                    break
                        if has_toc:
                            break
                except Exception:
                    pass
                # Method 2: check if Rank Math TOC module is active (common)
                if not has_toc:
                    try:
                        r = await client.get(f"{base_url}/wp-json/rankmath/v1/", headers=headers)
                        if r.status_code in (200, 301):
                            has_toc = True
                            break
                    except Exception:
                        pass
    except Exception:
        pass
    _cache_put(_toc_plugin_cache, cache_key, has_toc)
    return has_toc


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
    _cache_put(_seo_plugin_cache, base_url, plugin)
    return plugin

async def _ping_sitemaps(base_url: str, site_auth, post_url: str = "", extra_urls: list[str] = None) -> None:
    """Ping Bing sitemap and Pingomatic after publishing."""
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as ping_client:
            # Note: Google sitemap ping deprecated 2023 — only Bing remains
            # SEO #18: cache-bust sitemap URL with timestamp
            sitemap_url = f"{base_url}/sitemap.xml?t={int(time.time())}"
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
    _alt_suffix = random.choice(_alt_variations_pl) if (keyword and any(c in keyword for c in "ąćęłńóśźż")) else random.choice(_alt_variations_en)
    # SEO #56: alt text includes year for freshness signal in image search
    _year = datetime.now(timezone.utc).year
    alt_text = f"{keyword or title} — {_alt_suffix} {_year}"
    # SEO #35: prefer WebP format for smaller files and better Core Web Vitals
    _use_webp = HAS_PILLOW
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
    async with httpx.AsyncClient(verify=False, timeout=30, auth=site_auth, follow_redirects=True) as client:
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
                # FIX: when plugin is "yoast" (default/fallback), only write Yoast meta — not both
                _seo_plugin = await _detect_seo_plugin(base_url, auth, http_user, http_pass)
                _is_yoast = _seo_plugin == "yoast"
                _is_rankmath = _seo_plugin == "rankmath"
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
                        # SEO #106: og:type for AIOSEO (defaults to website, should be article)
                        meta["_aioseop_opengraph_settings"] = '{"object_type":"article"}'
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
                        # SEO #107: reading_time meta for Rank Math (improves snippet + schema)
                        _word_count_est = len(re.sub(r'<[^>]+>', ' ', content or '').split())
                        _reading_time = max(1, _word_count_est // 250)
                        meta["rank_math_readtime"] = str(_reading_time)
                    # SEO #122: robots meta on post level for max snippet/image preview
                    if _is_rankmath:
                        meta["rank_math_robots"] = ["index", "follow", "max-snippet:-1", "max-image-preview:large", "max-video-preview:-1"]
                    if _is_yoast:
                        meta["_yoast_wpseo_meta-robots-adv"] = "max-snippet:-1,max-image-preview:large,max-video-preview:-1"
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

                # Retry up to 3 times on transient failures (timeout, connect error, 502/503/504)
                resp = None
                _max_retries = 3
                for _attempt in range(_max_retries):
                    try:
                        resp = await client.post(
                            f"{base_url}/wp-json/wp/v2/posts",
                            json=post_data,
                            headers=headers,
                            timeout=30,
                        )
                        # BUG-2 FIX: retry on 502/503/504 gateway errors
                        if resp.status_code in (502, 503, 504) and _attempt < _max_retries - 1:
                            logger.warning(f"[WP] Transient HTTP {resp.status_code} on attempt {_attempt + 1}, retrying...")
                            await asyncio.sleep(3 * (_attempt + 1))
                            resp = None
                            continue
                        break
                    except (httpx.TimeoutException, httpx.ConnectError) as _te:
                        if _attempt < _max_retries - 1:
                            logger.warning(f"[WP] {type(_te).__name__} on attempt {_attempt + 1}, checking for duplicate before retry...")
                            # BUG-1 FIX: check if post was created despite timeout before retrying
                            try:
                                check_resp = await client.get(
                                    f"{base_url}/wp-json/wp/v2/posts",
                                    params={"slug": slug, "status": "publish,draft,pending", "per_page": 1},
                                    headers=headers,
                                    timeout=10,
                                )
                                if check_resp.status_code == 200 and check_resp.json():
                                    existing = check_resp.json()[0]
                                    logger.info(f"[WP] Post already exists (id={existing['id']}), skipping retry")
                                    return {
                                        "success": True,
                                        "url": existing.get("link", ""),
                                        "post_id": existing["id"],
                                    }
                            except Exception:
                                pass  # If duplicate check fails, proceed with retry
                            await asyncio.sleep(3 * (_attempt + 1))
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
                    # FIX: add done callback to log exceptions from fire-and-forget task
                    _ping_task = asyncio.create_task(_ping_sitemaps(base_url, site_auth, post_url=post_url))
                    _ping_task.add_done_callback(lambda t: logger.error(f"[WP] Ping failed: {t.exception()}") if not t.cancelled() and t.exception() else None)
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
            # BUG-3 FIX: handle race condition — another request created the tag first
            elif resp2.status_code == 400:
                try:
                    body = resp2.json()
                    if body.get("code") == "term_exists" and body.get("data", {}).get("term_id"):
                        ids.append(body["data"]["term_id"])
                except Exception:
                    pass
        except Exception:
            pass
    return [i for i in ids if i]


async def check_wp_credentials(domain: str, wp_login: str, wp_pass: str, http_user: str = "", http_pass: str = "") -> bool:
    """Quick WP REST API credentials check. Returns True if valid."""
    auth = _auth_header(wp_login, wp_pass)
    site_auth = _http_auth(http_user, http_pass)
    async with httpx.AsyncClient(verify=False, timeout=10, auth=site_auth, follow_redirects=True) as client:
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

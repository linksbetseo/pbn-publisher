"""
Multi-pass article generation following n8n workflow pattern:
1.  DataForSEO SERP top 10 → select 3 blog URLs
2.  DataForSEO content parsing per URL → avg word count, KW density, LSI terms
3.  GPT: keyword cluster + search intent
4.  GPT: outline (sections separated by <<<<)
5.  GPT: title
6.  GPT: intro (first paragraph = direct answer for AI Overview)
7.  GPT: each section separately (target word count + KW density + LSI)
8.  GPT: conclusion
9.  GPT: FAQ
10. GPT: excerpt (→ WP excerpt/meta description)
11. Assemble HTML + Schema FAQPage JSON-LD + internal links + anchor dedup
"""
import asyncio
import hashlib
import json as _json
import logging
import os
import random
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from openai import AsyncOpenAI

from config import OPENAI_API_KEY, DB_PATH
from services.content_enrichments import enrich_article
from services.article_helpers import (
    serp_cache_get as _serp_cache_get,
    serp_cache_set as _serp_cache_set,
    is_blog_url as _is_blog_url,
    count_words as _count_words,
    keyword_density as _keyword_density,
    extract_lsi as _extract_lsi,
    content_fingerprint as _content_fingerprint,
    markdown_to_html as _markdown_to_html,
    strip_markdown_remnants as _strip_markdown_remnants,
    slugify_heading as _slugify_heading,
)

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
logger = logging.getLogger(__name__)

# Default GPT model — can be overridden via GPT_MODEL env var or DB settings
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")

# In-memory cache for GPT model + custom LLM config (avoids DB query on every GPT call)
_gpt_model_cache: dict = {"model": None, "ts": 0}
_GPT_MODEL_CACHE_TTL = 120  # seconds

# Cache for custom LLM settings
_custom_llm_cache: dict = {"data": None, "ts": 0}


async def get_gpt_model() -> str:
    """Read GPT model from DB settings table, fall back to env var / default. Cached 2min."""
    now = time.time()
    if _gpt_model_cache["model"] and (now - _gpt_model_cache["ts"]) < _GPT_MODEL_CACHE_TTL:
        return _gpt_model_cache["model"]
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT value FROM settings WHERE key = 'gpt_model'"
            ) as cur:
                row = await cur.fetchone()
                if row and row[0]:
                    _gpt_model_cache["model"] = row[0]
                    _gpt_model_cache["ts"] = now
                    return row[0]
    except Exception as e:
        logger.debug(f"[GPT] Model cache read failed: {e}")
    return GPT_MODEL


async def get_custom_llm_config() -> dict:
    """Return custom LLM config from DB. Keys: enabled, base_url, model, api_key. Cached 2min."""
    now = time.time()
    if _custom_llm_cache["data"] is not None and (now - _custom_llm_cache["ts"]) < _GPT_MODEL_CACHE_TTL:
        return _custom_llm_cache["data"]
    default = {"enabled": False, "base_url": "", "model": "", "api_key": "", "max_tokens": 0, "serp_chars": 0}
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT key, value FROM settings WHERE key IN "
                "('custom_llm_enabled','custom_llm_base_url','custom_llm_model','custom_llm_api_key',"
                "'custom_llm_max_tokens','custom_llm_serp_chars')"
            ) as cur:
                rows = dict(await cur.fetchall())
        result = {
            "enabled": rows.get("custom_llm_enabled", "0") == "1",
            "base_url": rows.get("custom_llm_base_url", ""),
            "model": rows.get("custom_llm_model", ""),
            "api_key": rows.get("custom_llm_api_key", ""),
            "max_tokens": int(rows.get("custom_llm_max_tokens", "0") or "0"),
            "serp_chars": int(rows.get("custom_llm_serp_chars", "0") or "0"),
        }
        _custom_llm_cache["data"] = result
        _custom_llm_cache["ts"] = now
        return result
    except Exception as e:
        logger.debug(f"[GPT] Custom LLM cache read failed: {e}")
    return default


async def get_openai_client() -> tuple["AsyncOpenAI", str, bool]:
    """Return (client, model, is_custom).

    is_custom=True when a custom LLM endpoint is active.
    Callers should use is_custom to reduce max_tokens and prompt size
    because local/small models have limited context windows.
    """
    cfg = await get_custom_llm_config()
    if cfg["enabled"] and cfg["base_url"] and cfg["model"]:
        base = cfg["base_url"].rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        custom_client = AsyncOpenAI(
            api_key=cfg["api_key"] or "not-needed",
            base_url=base,
            timeout=1200.0,  # 20 min — local/tunneled LLMs can be slow
            max_retries=1,
        )
        return custom_client, cfg["model"], True
    # Standard OpenAI
    model = await get_gpt_model()
    return client, model, False


# Functions _is_blog_url, _count_words, _keyword_density, _extract_lsi,
# _content_fingerprint now imported from services.article_helpers


async def _fetch_serp_content(
    topic: str,
    dfs_login: str,
    dfs_password: str,
    location_code: int = 2616,
    language_code: str = "pl",
) -> dict:
    """
    Fetch top 3 blog URLs from SERP, parse content + PAA questions.
    Returns: text, avg_words, avg_density, lsi_terms, paa_questions
    Cached 24h per (topic, location, language).
    """
    empty = {"text": "", "avg_words": 0, "avg_density": 0.0, "lsi_terms": [], "paa_questions": [], "serp_urls": []}
    if not dfs_login or not dfs_password:
        return empty

    # SEO #125: include device in SERP cache key (mobile vs desktop SERP differs)
    cache_key = f"{topic.lower().strip()}:{location_code}:{language_code}:desktop"
    cached = await _serp_cache_get(cache_key)
    if cached:
        logger.info(f"[SERP] Cache hit for '{topic}'")
        return cached

    try:
        import httpx
        from services.dataforseo_service import DataForSEOClient
        dfs = DataForSEOClient(dfs_login, dfs_password)

        # Reuse single httpx connection for all DataForSEO requests (SERP + 3 page_content)
        async with httpx.AsyncClient(timeout=60) as _dfs_client:
            serp_raw = await dfs.serp_top10_full(topic, location_code, language_code, _client=_dfs_client)
            serp = serp_raw.get("organic", [])
            paa_questions = serp_raw.get("paa", [])

            blog_urls = [r["url"] for r in serp if r.get("url") and _is_blog_url(r["url"])][:3]

            if not blog_urls:
                logger.warning("[SERP] No blog URLs found")
                return empty

            logger.info(f"[SERP] Parsing {len(blog_urls)} URLs, PAA={len(paa_questions)}")

            parts = []
            word_counts = []
            densities = []
            text_parts = []
            contents = await asyncio.gather(*[dfs.page_content(url, _client=_dfs_client) for url in blog_urls], return_exceptions=True)

        for url, content in zip(blog_urls, contents):
            if isinstance(content, Exception) or not content:
                continue
            wc = _count_words(content)
            dens = _keyword_density(content, topic)
            word_counts.append(wc)
            densities.append(dens)
            text_parts.append(content)
            parts.append(f"--- {url} ({wc} słów, gęstość KW: {dens}%) ---\n{content[:3000]}")

        all_text = " ".join(text_parts)
        avg_words = int(sum(word_counts) / len(word_counts)) if word_counts else 0
        avg_density = round(sum(densities) / len(densities), 2) if densities else 0.0
        lsi_terms = _extract_lsi(all_text, topic, top_n=20)

        logger.info(f"[SERP] avg_words={avg_words}, avg_density={avg_density}%, LSI={lsi_terms[:8]}, PAA={paa_questions[:3]}")
        result = {
            "text": "\n\n".join(parts),
            "avg_words": avg_words,
            "avg_density": avg_density,
            "lsi_terms": lsi_terms,
            "paa_questions": paa_questions[:8],
            "serp_urls": blog_urls,  # real URLs for source_citations
        }
        await _serp_cache_set(cache_key, result)
        return result

    except Exception as e:
        logger.warning(f"[SERP] Failed: {e}")
        return empty



# Functions _markdown_to_html, _strip_markdown_remnants now imported from services.article_helpers


def _sanitize_for_json(s: str) -> str:
    """Remove null bytes and other control chars that break JSON serialization."""
    if not s:
        return s
    # Remove null bytes (\x00) and other ASCII control chars except \t \n \r
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', s)


async def _gpt(system: str, user: str, temperature: float = 0.7, max_tokens: int = 2000, model: str = None) -> str:
    _client, _model, _is_custom = await get_openai_client()
    if model is None:
        model = _model
    # Strip null bytes / control chars that cause OpenAI 400 "could not parse JSON body"
    system = _sanitize_for_json(system or "")
    user = _sanitize_for_json(user or "")
    for attempt in range(3):
        try:
            response = await _client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            # FIX #73: guard against empty choices (rare OpenAI edge case with content filters)
            if not response.choices:
                logger.warning("[GPT] Empty choices returned — possible content filter")
                return ""
            return (response.choices[0].message.content or "").strip()
        except Exception as e:
            if attempt == 2:
                raise
            # FIX #74: add jitter to GPT retry delay
            wait = 2 ** attempt + random.uniform(0, 1.5)
            logger.warning(f"[GPT] attempt {attempt+1} failed: {e} — retrying in {wait:.1f}s")
            await asyncio.sleep(wait)



def _fix_heading_hierarchy(html: str) -> str:
    """
    Post-process HTML to fix heading hierarchy issues from GPT output.
    Rules:
    - H2 can appear after H1 or another H2/H3
    - H3 must be preceded by H2 (no orphan H3 before first H2)
    - H4 must be preceded by H3
    - No skipped levels (H1 → H3 without H2)
    Fixes by promoting headings to the correct level.
    """
    # Find all heading tags with their positions
    heading_pattern = re.compile(r'<(h[1-6])([^>]*)>(.*?)</\1>', re.DOTALL | re.IGNORECASE)
    matches = list(heading_pattern.finditer(html))
    if not matches:
        return html

    replacements = []  # (start, end, old_tag, new_tag)
    last_level = 1  # assume H1 exists (title)

    for m in matches:
        tag = m.group(1).lower()
        level = int(tag[1])
        attrs = m.group(2)
        content = m.group(3)

        # FIX #13: promote H1 inside body to H2 (WP theme renders title as H1 — duplicate H1 is SEO error)
        if level == 1:
            new_tag = "h2"
            # SEO #105: add ID to heading for anchor linking / TOC jump links
            _id_slug = _slugify_heading(re.sub(r'<[^>]+>', '', content))
            _id_attr = f' id="{_id_slug}"' if _id_slug and 'id=' not in attrs else ''
            replacements.append((m.start(), m.end(), m.group(0),
                                 f"<{new_tag}{_id_attr}{attrs}>{content}</{new_tag}>"))
            last_level = 2
            continue
        # FIX #50: also demote H5/H6 to H4 max (H5/H6 are invisible in most WP themes)
        if level >= 5:
            new_tag = "h4"
            replacements.append((m.start(), m.end(), m.group(0),
                                 f"<{new_tag}{attrs}>{content}</{new_tag}>"))
            last_level = 4
            continue
        # Fix: heading level skips more than 1 level down from last
        if level > last_level + 1:
            new_level = last_level + 1
            new_tag = f"h{new_level}"
            # SEO #105: add ID to heading for anchor linking / TOC jump links
            _id_slug = _slugify_heading(re.sub(r'<[^>]+>', '', content))
            _id_attr = f' id="{_id_slug}"' if _id_slug and 'id=' not in attrs else ''
            replacements.append((m.start(), m.end(), m.group(0),
                                 f"<{new_tag}{_id_attr}{attrs}>{content}</{new_tag}>"))
            last_level = new_level
        else:
            # SEO #105: add ID to all H2/H3 headings for anchor linking / TOC jump links
            if level in (2, 3) and 'id=' not in attrs:
                _id_slug = _slugify_heading(re.sub(r'<[^>]+>', '', content))
                if _id_slug:
                    _id_attr = f' id="{_id_slug}"'
                    replacements.append((m.start(), m.end(), m.group(0),
                                         f"<{tag}{_id_attr}{attrs}>{content}</{tag}>"))
            last_level = level

    # Apply replacements in reverse order to preserve positions
    result = html
    for start, end, old, new in reversed(replacements):
        result = result[:start] + new + result[end:]

    return result




_LINK_CONTEXTS_PL = [
    lambda lnk: f" Więcej na ten temat znajdziesz na stronie {lnk}.",
    lambda lnk: f" Szczegółowe informacje dostępne są pod adresem {lnk}.",
    lambda lnk: f" Warto odwiedzić serwis {lnk}, gdzie znajdziesz więcej materiałów.",
    lambda lnk: f" Dodatkowe zasoby: {lnk}.",
    lambda lnk: f" Polecamy również stronę {lnk}.",
    lambda lnk: f" Temat szczegółowo omawia {lnk}.",
    lambda lnk: f" Jak podaje {lnk}, jest to kluczowy aspekt zagadnienia.",
    lambda lnk: f" Przeczytaj pełny przewodnik: {lnk}.",
    lambda lnk: f" Na stronie {lnk} można znaleźć uzupełniające informacje.",
    lambda lnk: f" Zgodnie z materiałami opublikowanymi na {lnk}, warto zwrócić uwagę na kilka kwestii.",
    lambda lnk: f" Więcej danych na ten temat publikuje {lnk}.",
    lambda lnk: f" Obszerną analizę tego zagadnienia przedstawia {lnk}.",
    lambda lnk: f" Praktyczne wskazówki znajdziesz również na {lnk}.",
    lambda lnk: f" Powiązane materiały zebrano na stronie {lnk}.",
    lambda lnk: f" {lnk} prezentuje dodatkowe dane, które mogą okazać się przydatne.",
]
_LINK_CONTEXTS_EN = [
    lambda lnk: f" Find more information at {lnk}.",
    lambda lnk: f" Detailed resources are available at {lnk}.",
    lambda lnk: f" We recommend visiting {lnk} for additional materials.",
    lambda lnk: f" Additional resources: {lnk}.",
    lambda lnk: f" Also check out {lnk}.",
    lambda lnk: f" This topic is covered in depth at {lnk}.",
    lambda lnk: f" As reported by {lnk}, this is a key consideration.",
    lambda lnk: f" Read the full guide: {lnk}.",
    lambda lnk: f" You can find additional insights at {lnk}.",
    lambda lnk: f" According to {lnk}, there are several factors worth noting.",
    lambda lnk: f" For a broader perspective, see {lnk}.",
    lambda lnk: f" A comprehensive breakdown is available at {lnk}.",
    lambda lnk: f" {lnk} offers a useful overview of related data.",
    lambda lnk: f" Practical tips on this subject can be found at {lnk}.",
    lambda lnk: f" Further reading on this topic: {lnk}.",
]


def _inject_anchors(html: str, anchors_info: str, language: str = "pl") -> str:
    """
    Inject client links into article paragraphs contextually.
    - First link: injected in paragraph 3-5 (after intro, inside content)
    - Additional links: spread across later paragraphs
    - Never injected in first 2 or last 2 paragraphs
    - Surrounding context varies to avoid footprint, language-aware
    """
    if not anchors_info:
        return html
    links = re.findall(r'<a\s[^>]*>.*?</a>', anchors_info, re.DOTALL | re.IGNORECASE)
    seen_hrefs: set = set()
    paragraphs = re.findall(r'<p>.*?</p>', html, re.DOTALL)
    para_count = len(paragraphs)

    _LINK_CONTEXTS = _LINK_CONTEXTS_PL if language == "pl" else _LINK_CONTEXTS_EN
    for i, link in enumerate(links):
        href_match = re.search(r'href=["\']([^"\']+)["\']', link)
        if not href_match:
            continue
        href = href_match.group(1).rstrip("/")
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)

        # Spread links across content, skip first 2 and last 2 paragraphs
        safe_start = 2
        safe_end = max(safe_start + 1, para_count - 2)
        if safe_end <= safe_start:
            continue

        # For first link: target 1/3 into article; for others: spread later
        if i == 0:
            target_idx = safe_start + (safe_end - safe_start) // 3
        else:
            target_idx = safe_start + ((safe_end - safe_start) * (i + 1)) // (len(links) + 1)
        target_idx = max(safe_start, min(safe_end, target_idx))

        para = paragraphs[target_idx] if target_idx < para_count else None
        # Skip paragraphs that already have links
        # SEO #64: use URL-based check instead of exact string match
        _link_href = re.search(r'href=["\']([^"\']+)["\']', link)
        _link_url = _link_href.group(1) if _link_href else ""
        if para and 'href=' not in para and (_link_url and _link_url not in html):
            ctx = random.choice(_LINK_CONTEXTS)
            new_para = para[:-4] + ctx(link) + "</p>"
            html = html.replace(para, new_para, 1)
    return html


def _inject_internal_links(html: str, published_posts: list[dict], topic: str, language: str = "pl") -> str:
    """
    Inject internal links to already-published posts on the same domain.
    - 1 link per ~400 words (min 2, max 5)
    - Best-matching paragraph per post (keyword overlap)
    - Natural inline context, not just appended at end
    """
    if not published_posts:
        return html

    paragraphs = re.findall(r'<p>.*?</p>', html, re.DOTALL)
    if len(paragraphs) < 3:
        return html

    # How many links to inject based on article length
    word_count = len(re.sub(r'<[^>]+>', '', html).split())
    max_links = max(2, min(5, word_count // 400))

    topic_words = set(re.findall(r'\w{3,}', topic.lower()))

    _INT_CONTEXTS_PL = [
        lambda lnk: f" Więcej na ten temat znajdziesz w artykule: {lnk}.",
        lambda lnk: f" Przeczytaj też: {lnk}.",
        lambda lnk: f" Powiązany artykuł: {lnk}.",
        lambda lnk: f" Szczegóły opisaliśmy w: {lnk}.",
        lambda lnk: f" Warto zapoznać się z: {lnk}.",
        lambda lnk: f" Dowiedz się więcej: {lnk}.",
    ]
    _INT_CONTEXTS_EN = [
        lambda lnk: f" Learn more in our article: {lnk}.",
        lambda lnk: f" Read also: {lnk}.",
        lambda lnk: f" Related article: {lnk}.",
        lambda lnk: f" We cover this in detail here: {lnk}.",
        lambda lnk: f" Worth reading: {lnk}.",
        lambda lnk: f" Find out more: {lnk}.",
    ]
    _int_contexts = _INT_CONTEXTS_PL if language == "pl" else _INT_CONTEXTS_EN

    # Pre-extract word sets for all inner paragraphs (avoids repeated regex in inner loop)
    para_word_sets = {}
    inner_indices = list(range(1, len(paragraphs) - 1))
    for idx in inner_indices:
        para_text = re.sub(r'<[^>]+>', '', paragraphs[idx]).lower()
        para_word_sets[idx] = set(re.findall(r'\w{3,}', para_text))

    injected = 0
    used_urls: set = set()
    used_paras: set = set()

    # Sort posts by relevance to current topic first
    def _relevance(post):
        t = (post.get("title", "") + " " + post.get("keyword", "")).lower()
        return len(topic_words & set(re.findall(r'\w{3,}', t)))

    candidates = sorted(published_posts[:20], key=_relevance, reverse=True)

    for post in candidates:
        if injected >= max_links:
            break
        url = post.get("url") or post.get("wp_post_url", "")
        title = post.get("title", "") or post.get("keyword", "")
        if not url or not title or url in used_urls:
            continue
        # FIX: skip self-link — don't link to current article's keyword
        _post_kw = (post.get("keyword", "") or post.get("title", "")).strip().lower()
        if _post_kw and _post_kw == topic.strip().lower():
            continue

        title_words = set(re.findall(r'\w{3,}', title.lower()))
        if not title_words:
            continue

        # Find best matching paragraph (skip first 1 and last 1)
        best_para = None
        best_score = 0
        for idx in inner_indices:
            para = paragraphs[idx]
            if para in used_paras:
                continue
            # Skip paragraphs that already have 2+ links
            if para.count('href=') >= 2:
                continue
            para_words = para_word_sets[idx]
            overlap = len(title_words & para_words)
            topic_bonus = 2 if len(topic_words & para_words) > 3 else 0
            score = overlap + topic_bonus
            if score > best_score:
                best_score = score
                best_para = para

        # Lower threshold — even score=0 is ok as fallback if we have few paragraphs
        min_score = 1 if len(paragraphs) > 6 else 0
        if best_para is None or best_score < min_score:
            # Fallback: pick any unused paragraph without links
            for para in paragraphs[1:-1]:
                if para not in used_paras and para.count('href=') == 0:
                    best_para = para
                    break

        if not best_para:
            continue

        kw = post.get("keyword", "")
        anchor_text = kw if (kw and len(kw.split()) <= 6) else " ".join(title.split()[:5])
        # SEO #9: escape HTML entities in title attribute to prevent broken markup
        _safe_title = title.replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;')
        link = f'<a href="{url}" title="{_safe_title}">{anchor_text}</a>'
        ctx = random.choice(_int_contexts)
        new_para = best_para[:-4] + ctx(link) + "</p>"
        html = html.replace(best_para, new_para, 1)
        used_urls.add(url)
        used_paras.add(best_para)
        injected += 1

    if injected:
        logger.info(f"[Article] Injected {injected}/{max_links} internal links (word_count={word_count})")
    return html


# ── Authority outbound links — natural editorial link profile ─────────────────
_AUTHORITY_DOMAINS_PL = [
    ("pl.wikipedia.org", "Wikipedia"),
    ("stat.gov.pl", "GUS"),
    ("biznes.gov.pl", "Biznes.gov.pl"),
    ("gov.pl", "Gov.pl"),
    ("europa.eu", "Europa.eu"),
]
_AUTHORITY_DOMAINS_EN = [
    ("en.wikipedia.org", "Wikipedia"),
    ("scholar.google.com", "Google Scholar"),
    ("data.gov", "Data.gov"),
    ("europa.eu", "Europa.eu"),
    ("who.int", "WHO"),
]
_AUTH_CONTEXTS_PL = [
    lambda lnk: f" Więcej informacji na ten temat można znaleźć na {lnk}.",
    lambda lnk: f" Zagadnienie to szerzej opisuje {lnk}.",
    lambda lnk: f" Źródło: {lnk}.",
    lambda lnk: f" Podstawowe informacje prezentuje {lnk}.",
]
_AUTH_CONTEXTS_EN = [
    lambda lnk: f" More background on this topic is available at {lnk}.",
    lambda lnk: f" For additional context, see {lnk}.",
    lambda lnk: f" Source: {lnk}.",
    lambda lnk: f" Basic information is presented at {lnk}.",
]


def _inject_authority_links(html: str, topic: str, language: str = "pl") -> str:
    """Inject 1-2 dofollow outbound links to authority domains for natural link profile."""
    paragraphs = re.findall(r'<p>.*?</p>', html, re.DOTALL)
    if len(paragraphs) < 6:
        return html

    pool = _AUTHORITY_DOMAINS_PL if language == "pl" else _AUTHORITY_DOMAINS_EN
    contexts = _AUTH_CONTEXTS_PL if language == "pl" else _AUTH_CONTEXTS_EN
    count = random.choice([1, 1, 2])  # ~67% one link, ~33% two links
    picked = random.sample(pool, min(count, len(pool)))

    topic_slug = urllib.parse.quote(topic.replace(" ", "_"), safe="/_-")
    # Place in last third of article (different zone from client links in first third)
    safe_start = len(paragraphs) * 2 // 3
    safe_end = len(paragraphs) - 2
    if safe_end <= safe_start:
        return html

    for i, (domain, label) in enumerate(picked):
        idx = safe_start + i
        if idx >= safe_end or idx >= len(paragraphs):
            break
        para = paragraphs[idx]
        if 'href=' in para:
            continue
        if domain.endswith("wikipedia.org"):
            href = f"https://{domain}/wiki/{topic_slug}"
        else:
            href = f"https://{domain}"
        link = f'<a href="{href}">{label}</a>'
        ctx = random.choice(contexts)
        new_para = para[:-4] + ctx(link) + "</p>"
        html = html.replace(para, new_para, 1)

    return html


# Anchor text rotation pools — reduces footprint, more natural link profile
_ANCHOR_GENERIC_PL = ["tutaj", "sprawdź", "dowiedz się więcej", "więcej informacji", "przeczytaj więcej", "na tej stronie"]
_ANCHOR_GENERIC_EN = ["here", "check it out", "learn more", "find out more", "read more", "on this page"]


def _rotate_anchor(anchor_text: str, client_domain: str, language: str = "pl") -> str:
    """
    Rotate anchor text to avoid footprint — RANDOM per article call.
    Distribution based on Google API leak analysis (context2 signal):
      10% exact match, 25% brand/naked URL, 25% partial/topic, 20% generic, 20% long-tail
    Google data shows >5% exact match triggers algorithmic flags.
    Uses os.urandom for true randomness (not deterministic hash).
    """
    # Use os.urandom for non-deterministic rotation per article
    bucket = int.from_bytes(os.urandom(1), "big") % 100
    if bucket < 10:
        # Exact match — keep as-is (max 10%, safe zone)
        return anchor_text
    elif bucket < 35:
        # Naked URL / brand name (strip www, use domain root)
        domain = client_domain.replace("https://", "").replace("http://", "").rstrip("/").split("/")[0]
        return domain.replace("www.", "")
    elif bucket < 60:
        # Partial match — first word(s) or last word(s) of anchor
        words = anchor_text.split()
        if len(words) > 2:
            # Randomly pick start or end fragment
            if random.random() < 0.5:
                return " ".join(words[:max(1, len(words) - 1)])
            else:
                return " ".join(words[1:])
        elif len(words) > 1:
            # FIX #14: for 2-word anchors, prefer the second word (more specific) or full phrase
            return words[-1] if random.random() < 0.5 else anchor_text
        return anchor_text
    elif bucket < 80:
        # Generic
        generics = _ANCHOR_GENERIC_PL if language == "pl" else _ANCHOR_GENERIC_EN
        return random.choice(generics)
    else:
        # Long-tail / contextual variation — add modifier
        _modifiers_pl = ["poradnik", "informacje", "oferta", "strona", "serwis"]
        _modifiers_en = ["guide", "info", "offer", "page", "service"]
        mods = _modifiers_pl if language == "pl" else _modifiers_en
        words = anchor_text.split()
        if len(words) <= 3:
            return f"{anchor_text} — {random.choice(mods)}"
        return " ".join(words[:2]) + f" {random.choice(mods)}"


async def generate_article(
    topic: str,
    client_domain: str,
    anchor_text: str,
    language: str = "pl",
    anchor_text2: str = "",
    anchor_url2: str = "",
    anchor_text3: str = "",
    anchor_url3: str = "",
    custom_prompt: str = "",
    variation_hint: str = "",
    dfs_login: str = "",
    dfs_password: str = "",
    location_code: int = 2616,
    published_posts: Optional[list] = None,  # for internal linking
    domain_fingerprints: Optional[set] = None,  # for dedup check
    layout_variant: Optional[str] = None,  # "faq_top" | "tldr" | "short_answer" | None (random)
    pillar_page_url: str = "",  # PBN inter-link: supporting → pillar
    pillar_page_anchor: str = "",  # anchor text for pillar link
    pbn_domain: str = "",  # FIX: PBN domain for Article JSON-LD publisher (avoids money site footprint)
    client_context: str = "",  # crawled summary of client's website — used to ground article facts
) -> dict:
    _t0 = time.time()

    # Resolve GPT model once for entire article generation (avoids 8+ cache lookups)
    _resolved_model = await get_gpt_model()

    def clean_url(url: str) -> str:
        url = url.strip()
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url
        # Block non-http(s) schemes (javascript:, data:, etc.)
        if not url.startswith(("http://", "https://")):
            return ""
        return url

    # Build anchor links — client links are always dofollow (PBN purpose is link equity transfer)
    anchors_info = ""
    if client_domain and client_domain.strip():
        rotated_anchor = _rotate_anchor(anchor_text or topic, client_domain, language)
        anchors_info = f'<a href="{clean_url(client_domain)}">{rotated_anchor}</a>'
    if anchor_text2 and anchor_url2:
        rotated2 = _rotate_anchor(anchor_text2, anchor_url2, language)
        anchors_info += f', <a href="{clean_url(anchor_url2)}">{rotated2}</a>'
    if anchor_text3 and anchor_url3:
        rotated3 = _rotate_anchor(anchor_text3, anchor_url3, language)
        anchors_info += f', <a href="{clean_url(anchor_url3)}">{rotated3}</a>'
    # PBN inter-link: supporting page → pillar page (internal silo, always dofollow, no rotation)
    if pillar_page_url and pillar_page_anchor:
        anchors_info += f', <a href="{clean_url(pillar_page_url)}">{pillar_page_anchor}</a>'

    _current_year = datetime.now(timezone.utc).year
    variation = f" Kąt tematyczny: {variation_hint}." if variation_hint else ""
    custom_block = f"\nDodatkowe wymagania: {custom_prompt}" if custom_prompt else ""
    # Client context block — inject scraped website knowledge into every section prompt
    _ctx_block = (
        f"\n\n[WIEDZA O STRONIE KLIENTA — używaj tych faktów, nie wymyślaj]:\n{_sanitize_for_json(client_context)}"
        if client_context else ""
    )
    custom_block = custom_block + _ctx_block
    lang_pl = language == "pl"

    # ── Layout variant (30% faq_top, 20% tldr, 25% short_answer, 25% standard) ──
    if layout_variant is None:
        _rv = random.random()
        layout_variant = "faq_top" if _rv < 0.30 else ("tldr" if _rv < 0.50 else ("short_answer" if _rv < 0.75 else "standard"))

    # ── STEP 1: SERP + competitor analysis ───────────────────────────────────
    language_code = "pl" if lang_pl else "en"
    serp_data = await _fetch_serp_content(topic, dfs_login, dfs_password, location_code, language_code)
    serp_text = serp_data["text"]
    avg_words = serp_data["avg_words"] or 1200
    avg_density = serp_data["avg_density"] or 1.5
    lsi_terms = serp_data["lsi_terms"]
    paa_questions = serp_data.get("paa_questions", [])
    serp_urls = serp_data.get("serp_urls", [])

    # FIX #3: quality over quantity — minimum 2500, target 15-20% above avg, cap 3500
    target_words = max(2500, min(3500, int(avg_words * 1.18)))
    # FIX #4: tighter density range — 0.8-1.8% sweet spot (enrichment blocks add ~0.2-0.5%)
    target_density = round(max(0.8, min(1.8, avg_density)), 1)
    lsi_block = f"\nSłowa semantyczne LSI do użycia: {', '.join(lsi_terms[:15])}" if lsi_terms else ""
    # Custom LLM: trim SERP to configured limit (0 = auto: 1200 for small models, full for OpenAI)
    _cfg = await get_custom_llm_config()
    _is_custom_llm = _cfg["enabled"] and bool(_cfg.get("base_url")) and bool(_cfg.get("model"))
    if _is_custom_llm:
        _serp_limit = _cfg["serp_chars"] if _cfg["serp_chars"] > 0 else 1200
        # Scale max_tokens for each call: small models need much less per call
        _mt_intent   = _cfg["max_tokens"] if _cfg["max_tokens"] > 0 else 200
        _mt_outline  = _cfg["max_tokens"] if _cfg["max_tokens"] > 0 else 250
        _mt_title    = 80
        _mt_intro    = _cfg["max_tokens"] if _cfg["max_tokens"] > 0 else 400
        _mt_section  = _cfg["max_tokens"] if _cfg["max_tokens"] > 0 else 600
        _mt_conc     = _cfg["max_tokens"] if _cfg["max_tokens"] > 0 else 300
        _mt_faq      = _cfg["max_tokens"] if _cfg["max_tokens"] > 0 else 300
        _mt_excerpt  = 100
    else:
        _serp_limit  = len(serp_text)
        _mt_intent   = 400
        _mt_outline  = 500
        _mt_title    = 100
        _mt_intro    = 700
        _mt_section  = 1100
        _mt_conc     = 600
        _mt_faq      = 800
        _mt_excerpt  = 150
    serp_block = f"\n\n[SEO Scraped Info — top 3 konkurentów]\n{serp_text[:_serp_limit]}" if serp_text else ""

    logger.info(f"[Article] Target: {target_words} słów, density: {target_density}%, LSI: {len(lsi_terms)}")

    # ── STEP 2: Intent + entities ─────────────────────────────────────────────
    if lang_pl:
        intent_user = (
            f"Dla frazy: '{topic}'{variation}\n"
            f"Podaj krótko (max 5 zdań):\n"
            f"1. Główna intencja wyszukiwania\n"
            f"2. Cluster tematyczny\n"
            f"3. Kluczowe encje nazwane (marki, firmy, produkty, osoby, miejsca, instytucje, normy/standardy)\n"
            f"4. Powiązane encje z Google Knowledge Graph (podaj 5-8 konkretnych nazw własnych){serp_block}"
        )
    else:
        intent_user = (
            f"For keyword: '{topic}'{variation}\n"
            f"Briefly (max 5 sentences):\n"
            f"1. Search intent\n"
            f"2. Topic cluster\n"
            f"3. Key named entities (brands, companies, products, people, places, institutions, standards)\n"
            f"4. Related Google Knowledge Graph entities (list 5-8 specific proper nouns){serp_block}"
        )
    intent_analysis = await _gpt(
        "Jesteś ekspertem SEO." if lang_pl else "You are an SEO expert.",
        intent_user, temperature=0.3, max_tokens=_mt_intent, model=_resolved_model
    )
    logger.info(f"[Article] Intent: {intent_analysis[:80]}")

    # SEO #10: Extract named entities from intent analysis for explicit injection into section prompts
    _extracted_entities = ""
    _entity_match = re.search(r'(?:encje|entities|nazw[ay]? własn[eay]|proper nouns)[:\s]*(.+?)(?:\n|$)', intent_analysis, re.IGNORECASE)
    if _entity_match:
        _extracted_entities = _entity_match.group(1).strip()

    # SEO #5: Detect search intent type for dynamic CTA in excerpt
    _intent_type = "informational"  # default
    for _itype, _ipatterns in [
        ("transactional", ["kup", "cena", "koszt", "sklep", "buy", "price", "cost", "shop", "order"]),
        ("commercial", ["najlep", "ranking", "porównanie", "opinie", "best", "top", "review", "compare"]),
        ("navigational", ["logowanie", "login", "strona", "kontakt", "contact", "website"]),
    ]:
        if any(p in topic.lower() or p in intent_analysis.lower() for p in _ipatterns):
            _intent_type = _itype
            break

    # ── STEP 3: Outline ───────────────────────────────────────────────────────
    # SEO #10: entity block for section prompts
    # SEO #115: entity gap analysis — GPT-extracted entities from SERP competitors
    # injected into each section prompt to ensure article covers ALL entities that competitors mention
    _entity_block = f"\nEncje do użycia (entity gap fill — użyj WSZYSTKICH): {_extracted_entities}" if _extracted_entities else ""

    # FIX #5: section count based on 250-300 words per section (was 200 → too many thin sections)
    n_sections = max(4, min(8, round(target_words / 280)))
    if lang_pl:
        outline_user = (
            f"Stwórz outline artykułu SEO dla frazy: '{topic}'\n"
            f"Intencja i encje: {intent_analysis}\n"
            f"{f'KĄT TEMATYCZNY: {variation}' if variation else ''}\n"
            f"DOKŁADNIE {n_sections} sekcji H2, każda oddzielona '<<<<', "
            f"bez wstępu i zakończenia.\nTylko nagłówki H2, bez tekstu.{serp_block}"
        )
    else:
        outline_user = (
            f"Create SEO article outline for: '{topic}'\n"
            f"Intent: {intent_analysis}\n"
            f"{f'THEMATIC ANGLE: {variation}' if variation else ''}\n"
            f"EXACTLY {n_sections} H2 sections separated by '<<<<', "
            f"no intro/conclusion.\nOnly H2 headings.{serp_block}"
        )
    outline_raw = await _gpt(
        "Jesteś ekspertem SEO tworzącym struktury artykułów. Nagłówki H2 oddzielone '<<<<'." if lang_pl
        else "You are an SEO expert. H2 headings separated by '<<<<'.",
        outline_user, temperature=0.5, max_tokens=_mt_outline, model=_resolved_model
    )
    sections = [s.strip() for s in outline_raw.split("<<<<") if s.strip()]
    # FIX: GPT often returns "H2: Title" or "## Title" — strip these prefixes
    sections = [re.sub(r'^(?:H[2-4]:\s*|#{1,4}\s*)', '', s).strip() for s in sections]
    sections = [s for s in sections if s]
    if not sections:
        sections = [topic]
    logger.info(f"[Article] Sections ({len(sections)}): {sections}")

    # ── STEP 4: Title ─────────────────────────────────────────────────────────
    if lang_pl:
        title_user = (
            f"Wymyśl unikalny tytuł SEO dla frazy: '{topic}'\n"
            f"Sekcje artykułu: {', '.join(sections[:3])}\n"
            # FIX #46: prompt says 50-65 to match the 65-char enforcement below
            f"ZASADY: 50-65 znaków, '{topic}' MUSI pojawić się w pierwszych 30 znakach, przyciąga uwagę.\n"
            f"Użyj jednego z formatów:\n"
            f"- '[Keyword] — kompletny przewodnik {_current_year}'\n"
            f"- 'Jak [działanie związane z keyword]? [X] kroków'\n"
            f"- 'Co to jest [keyword] i jak [korzyść]?'\n"
            f"- '[X] najważniejszych faktów o [keyword]'\n"
            f"- '[Keyword]: kompletny poradnik dla każdego'\n"
            f"- '[Keyword] od A do Z — praktyczny poradnik'\n"
            f"- 'Dlaczego [keyword] jest tak ważne? Wyjaśniamy'\n"
            f"- '[Keyword] vs [alternatywa] — co wybrać?'\n"
            f"- 'Najlepsze sposoby na [keyword] w {_current_year}'\n"
            f"- '[Keyword] krok po kroku dla początkujących'\n"
            f"- 'Prawda o [keyword] — obalamy mity'\n"
            f"- '[X] błędów przy [keyword] których musisz unikać'\n"
            f"Tylko tytuł, bez cudzysłowów, bez markdown.{custom_block}"
        )
    else:
        title_user = (
            f"Create unique SEO title for: '{topic}'\n"
            f"Article sections: {', '.join(sections[:3])}\n"
            # FIX #47: prompt says 50-65 to match the 65-char enforcement below
            f"RULES: 50-65 characters, '{topic}' MUST appear in the first 30 characters, attention-grabbing.\n"
            f"Use one of these formats:\n"
            f"- '[Keyword] — Complete Guide {_current_year}'\n"
            f"- 'How to [action related to keyword]? [X] Steps'\n"
            f"- 'What is [keyword] and how does it [benefit]?'\n"
            f"- '[X] Key Facts About [keyword]'\n"
            f"- '[Keyword]: Everything You Need to Know'\n"
            f"- '[Keyword] from A to Z — A Practical Guide'\n"
            f"- 'Why [keyword] Matters More Than You Think'\n"
            f"- '[Keyword] vs [alternative] — Which One Wins?'\n"
            f"- 'Best Ways to [keyword] in {_current_year}'\n"
            f"- '[Keyword] Step by Step for Beginners'\n"
            f"- 'The Truth About [keyword] — Myths Debunked'\n"
            f"- '[X] [Keyword] Mistakes You Must Avoid'\n"
            f"Only the title, no quotes, no markdown.{custom_block}"
        )
    title = await _gpt(
        "Jesteś copywriterem SEO." if lang_pl else "You are an SEO copywriter.",
        title_user, temperature=0.8, max_tokens=_mt_title, model=_resolved_model
    )
    title = title.strip('"\'').strip()
    # FIX #7: strip markdown artifacts from title (GPT sometimes adds # or *)
    title = re.sub(r'^[#*\s]+', '', title).strip()
    # strip any HTML tags GPT may have wrapped around the title
    title = re.sub(r"<[^>]+>", "", title).strip()
    # title = full H1/post title — no length limit here
    # seo_title = truncated version for meta title / Yoast / RankMath (max 65 chars)
    if len(title) > 65:
        _cut_match = re.search(r'[?!\u2014]', title[:65])
        if _cut_match:
            seo_title = title[:_cut_match.end()].strip()
        else:
            cut = title[:65].rsplit(' ', 1)[0]
            cut = re.sub(r'\s+(i|o|w|z|a|że|do|na|po|przez|dla|jak|co|się)$', '', cut, flags=re.IGNORECASE)
            seo_title = cut if len(cut) > 30 else title[:65]
    else:
        seo_title = title
    logger.info(f"[Article] Title: {title} | SEO title: {seo_title}")

    # ── STEP 5: Intro (direct answer first) ──────────────────────────────────
    intro_kw_count = max(1, round(target_words * target_density / 100 * 0.15))
    if lang_pl:
        intro_system = (
            "Jesteś ekspertem SEO z doświadczeniem E-E-A-T. Piszesz wstępy zoptymalizowane pod AI Overview i featured snippets.\n"
            "STRUKTURA OBOWIĄZKOWA (BLUF — Bottom Line Up Front):\n"
            "1) PIERWSZE ZDANIE = DEFINICJA jednozdaniowa: '<strong>[Keyword]</strong> to [co to jest — konkretna odpowiedź].' "
            "To jedno zdanie musi natychmiast odpowiedzieć na główne pytanie czytelnika — nie ogólnikowe, ale pełne i konkretne.\n"
            "2) Reszta pierwszego akapitu (2-3 zdania) = rozwinięcie definicji + najważniejsze fakty.\n"
            "3) Drugi akapit = dlaczego to ważne, kontekst praktyczny, korzyści.\n"
            "4) Trzeci akapit = co czytelnik znajdzie w artykule (zapowiedź 3-4 sekcji).\n"
            "WAŻNE: Definicja w pierwszym zdaniu musi być KONKRETNA i PEŁNA — nie ogólnikowa. "
            "Zły przykład: 'Zdrowie jest ważne dla każdego.' "
            "Dobry przykład: 'Zdrowy styl życia to zbiór nawyków — zbilansowana dieta, aktywność fizyczna i sen — który redukuje ryzyko chorób przewlekłych.'\n"
            "Używaj tagów <p> i <strong> dla kluczowych terminów. "
            f"<strong> na frazę główną użyj DOKŁADNIE RAZ — tylko w pierwszym zdaniu. NIE pogrubiaj tej frazy ponownie.\n"
            f"AKTUALNOŚĆ: Mamy rok {_current_year}. NIE pisz 'W 2023 roku' ani 'trendy 2024' jako aktualne. "
            f"Używaj '{_current_year}' lub 'obecnie/aktualnie'.\n"
            "STATYSTYKI: NIE podawaj konkretnych wartości procentowych ani liczbowych jeśli nie ma ich w dostarczonych danych SERP. "
            "Zamiast '30% mniejsze ryzyko' pisz 'znacząco mniejsze ryzyko'. Ogólne sformułowania są bezpieczniejsze niż zmyślone liczby.\n"
            "ENCJE OSOBOWE — ZAKAZ: NIE podawaj z pamięci nazwisk polityków, ministrów, prezydentów "
            "ani innych osób pełniących urzędy/funkcje. Używaj ogólnych ról ('aktualny minister X', 'prezes instytucji Y').\n"
            "BEZWZGLĘDNY ZAKAZ: NIE używaj markdown. NIE pisz ## ani # ani **tekst**. TYLKO tagi HTML <p> i <strong>."
        )
        intro_user = (
            f"Napisz wstęp do artykułu '{title}' (keyword: '{topic}').\n"
            f"Intencja wyszukiwania: {intent_analysis}\n"
            f"Sekcje artykułu: {', '.join(sections[:4])}\n"
            f"PIERWSZE ZDANIE musi być: '<strong>{topic}</strong> to [pełna, konkretna definicja].' — NIE zacznij od 'W dzisiejszych czasach' ani od pytania.\n"
            f"Użyj '{topic}' {intro_kw_count}x naturalnie.{lsi_block}\n"
            f"Tylko HTML <p> i <strong>, bez nagłówków. OK do użycia <ul>/<li> jeśli pasuje.\n"
            f"WAŻNE: Każdy akapit wstępu musi wnosić NOWĄ informację. NIE powtarzaj tych samych faktów ani korzyści w różnych akapitach.\n"
            f"HUMANIZACJA: Mieszaj krótkie i długie zdania. Użyj 1 pytania retorycznego.{custom_block}"
        )
    else:
        intro_system = (
            "You are an SEO expert with E-E-A-T signals. Write intros optimized for AI Overview and featured snippets.\n"
            "MANDATORY STRUCTURE:\n"
            "1) First paragraph = DEFINITION + DIRECT answer (2-3 sentences). "
            "Start with '<strong>[Keyword]</strong> is...' format. AI Overview style.\n"
            "2) Second = why it matters, practical context.\n"
            "3) Third = what reader will find (section preview).\n"
            "Use <p> and <strong> for key terms. "
            "Use <strong> on the main keyword EXACTLY ONCE — only in the first sentence. Do NOT bold it again.\n"
            "HUMANIZATION: Mix short and long sentences. Use 1 rhetorical question.\n"
            f"CURRENCY: Current year is {_current_year}. Do NOT write 'In 2023' or '2024 trends' as current. "
            f"Use '{_current_year}' or 'currently/nowadays'.\n"
            "STATISTICS: Do NOT state specific percentages or numbers unless they appear in the provided SERP data. "
            "Write 'significantly lower risk' instead of '30% lower risk'. Vague is safer than hallucinated figures.\n"
            "PERSONAL ENTITIES — NO HALLUCINATION: Do NOT name from memory any politicians, ministers, "
            "presidents or other officeholders. Use generic roles ('current minister of X', 'head of institution Y').\n"
            "STRICT: NO markdown. Never use ## or # or **text**. ONLY HTML tags <p> and <strong>."
        )
        intro_user = (
            f"Write intro for '{title}' (keyword: '{topic}').\n"
            f"Search intent: {intent_analysis}\n"
            f"Sections: {', '.join(sections[:4])}\n"
            f"FIRST PARAGRAPH must start with a definition of '{topic}'.\n"
            f"Use '{topic}' {intro_kw_count}x naturally.{lsi_block}\n"
            f"Only HTML <p> and <strong>, no headings. OK to use <ul>/<li> if appropriate.\n"
            f"IMPORTANT: Each intro paragraph must add NEW information. Do NOT repeat the same fact in different paragraphs.{custom_block}"
        )
    intro_html = await _gpt(intro_system, intro_user, temperature=0.7, max_tokens=_mt_intro, model=_resolved_model)
    if not intro_html.strip().startswith("<"):
        intro_html = _markdown_to_html(intro_html)
    intro_html = _strip_markdown_remnants(intro_html)
    # SEO #65: enforce <strong> on first keyword mention in intro (case-insensitive check)
    if re.search(r'\b' + re.escape(topic) + r'\b', intro_html, re.IGNORECASE) and not re.search(r'<strong>' + re.escape(topic) + r'</strong>', intro_html, re.IGNORECASE):
        _kw_pattern = re.compile(re.escape(topic), re.IGNORECASE)
        _replaced = False
        def _strong_first(m):
            nonlocal _replaced
            if not _replaced:
                _replaced = True
                return f'<strong>{m.group(0)}</strong>'
            return m.group(0)
        intro_html = _kw_pattern.sub(_strong_first, intro_html)
    logger.info("[Article] Intro done")

    # ── STEP 6: Sections (parallel) ───────────────────────────────────────────
    # FIX #6: removed 1.2x multiplier — target_words already 12% above avg; don't double-inflate
    words_per_section = max(280, target_words // max(1, len(sections)))
    # Cap keyword density per section at 1.5% max (audit: max 3-4x/1000 words)
    kw_per_section = max(1, min(round(words_per_section * target_density / 100), max(1, round(words_per_section * 0.015))))
    # Pick 3-4 LSI terms per section (rotate through the list)
    lsi_per_section = lsi_terms[:15] if lsi_terms else []

    if lang_pl:
        section_system = (
            "Jesteś ekspertem SEO i merytorycznym autorem. Piszesz sekcje artykułu w HTML.\n"
            "WYMAGANIA:\n"
            "- Zacznij od <h2>, dodaj 1-2 <h3> pod-sekcje\n"
            "- Używaj <p>, <ul>/<li> lub <ol>/<li> tam gdzie pasuje\n"
            "- <strong> używaj MAKSYMALNIE 1-2 razy per sekcja, tylko dla kluczowego terminu lub ważnej liczby. NIE pogrubiaj nazw marek, odmian frazy kluczowej ani każdego słowa — to wygląda jak spam.\n"
            "- Pisz szczegółowo — używaj konkretnych i praktycznych przykładów, badań w kontekście zdrowia, liczby i dane, porady praktyczne z głęboką analizą każdego aspektu.\n"
            "- E-E-A-T CYTOWANIA: W każdej sekcji wpleć MINIMUM 1 konkretne badanie lub źródło w formacie: "
            "'[Nazwa badania/programu/instytucji] ([rok]) wykazało/pokazuje/podaje, że...'. "
            "Przykłady akceptowane: 'Raport WHO (2024) wskazuje...', 'Program Moje Zdrowie NFZ (2023) obejmuje...', "
            "'Badanie opublikowane w European Heart Journal (2023) wykazało...'. "
            "ZAKAZ: nie pisz 'badania pokazują', 'eksperci twierdzą', 'według naukowców' bez konkretnej nazwy. "
            "Jeśli nie masz weryfikowalnego źródła z SERP — opisz zjawisko bez statystyki.\n"
            "- ENCJE — MINIMUM 3 NOWE per sekcja: Każda sekcja MUSI wprowadzać co najmniej 3 unikalne nazwy własne "
            "(marki, instytucje, produkty, programy, normy, aplikacje) których NIE było w poprzednich sekcjach. "
            "Google NLP ocenia topical authority przez liczbę unikalnych encji. "
            "NIE powtarzaj tych samych 3-4 encji (np. NFZ, WHO, MZ) przez cały artykuł — każda sekcja to nowe nazwy.\n"
            "- Każda sekcja musi pokrywać ODMIENNY aspekt tematu. Zakaz powtarzania faktów z poprzednich sekcji.\n"
            f"- ENCJE OSOBOWE — ZAKAZ HALUCYNACJI: NIE podawaj z pamięci nazwisk polityków, "
            "ministrów, prezydentów, premierów, dyrektorów ani innych osób pełniących urzędy/funkcje. "
            "Dane osobowe zmieniają się — GPT może mieć nieaktualne dane. "
            "Jeśli potrzebujesz konkretnej osoby, użyj sformułowania ogólnego "
            "('aktualny minister', 'prezes instytucji X') LUB podaj tylko jeśli masz to wprost z dostarczonych danych SERP.\n"
            "- INFORMATION GAIN: Dodaj 1-2 fakty/perspektywy których BRAK w typowych artykułach "
            "na ten temat — mało znane porady, kontrintuicyjne wnioski.\n"
            "- STATYSTYKI: NIE podawaj konkretnych wartości % ani liczb jeśli nie ma ich w dostarczonych danych SERP. "
            "Zamiast '30% mniejsze ryzyko' pisz 'znacząco mniejsze ryzyko'. Nigdy nie wymyślaj statystyk.\n"
            "- HUMANIZACJA: Mieszaj krótkie zdania (5-8 słów) z długimi (20-30 słów). "
            "Używaj pytań retorycznych, porównań, konkretnych przykładów. "
            "Nie pisz monotonnie — każdy akapit innym tonem.\n"
            f"AKTUALNOŚĆ: Mamy rok {_current_year}. NIE pisz o poprzednich latach jako aktualnych "
            f"(np. 'W 2023 roku...', 'trendy 2024'). Jeśli podajesz dane/statystyki — "
            f"pisz '{_current_year}' lub 'obecnie/aktualnie'. Stare daty (2023, 2024) tylko w kontekście historycznym "
            f"('od 2023 roku', 'w porównaniu z 2024').\n"
            "BEZWZGLĘDNY ZAKAZ: NIE używaj markdown. NIE pisz ## ani ### ani # na początku linii. "
            "NIE używaj **tekst** ani *tekst*. TYLKO czysty HTML — tagi <h2>, <h3>, <p>, <ul>, <li>, <strong>, <em>.\n"
            "BEZWZGLĘDNY ZAKAZ #2: NIE generuj instrukcji, zadań, kroków, planów ani list TODO. "
            "NIE pisz 'Krok 1:', 'Zadanie:', 'Zidentyfikuj', 'Zbierz dane'. "
            "Pisz GOTOWY artykuł dla czytelnika, NIE instrukcję jak go napisać.\n"
            "BEZWZGLĘDNY ZAKAZ #3: Tekst wewnątrz <h2> i <h3> to WYŁĄCZNIE krótki tytuł sekcji (3-8 słów). "
            "NIE wstawiaj akapitów, zdań ani długiej treści do wnętrza tagów <h2>/<h3>. "
            "Cała treść artykułu idzie WYŁĄCZNIE w <p>, <ul>, <ol> — nigdy w nagłówku."
        )
    else:
        section_system = (
            "You are an SEO expert and subject matter author. Write article sections in HTML.\n"
            "REQUIREMENTS:\n"
            "- Start with <h2>, add 1-2 <h3> subsections\n"
            "- Use <p>, <ul>/<li> or <ol>/<li> where appropriate\n"
            "- Use <strong> MAX 1-2 times per section, only for the single most important term or a key number. Do NOT bold brand names, keyword variants, or multiple words per paragraph — it looks like spam.\n"
            "- Be specific — data, numbers, examples, practical tips\n"
            "- E-E-A-T EXPERIENCE: Weave in 1-2 sentences from a practical perspective, "
            "writing as an expert who has seen hundreds of such cases. Do NOT use first person 'I'. "
            "Do NOT use template phrases — write originally.\n"
            "- Each section must cover a DIFFERENT aspect of the topic. Do NOT repeat facts from other sections.\n"
            "- ENTITIES: Use specific proper nouns (brands, companies, products, places, standards, "
            "institutions) instead of generic terms. Google NLP recognizes entities — more relevant "
            "proper nouns related to the topic means better topical authority.\n"
            f"- PERSONAL ENTITIES — NO HALLUCINATION: Do NOT name from memory any politicians, "
            "ministers, presidents, prime ministers, directors or other officeholders. "
            "Personnel changes frequently — GPT training data may be outdated. "
            "Use generic roles ('current minister', 'head of institution X') OR only name someone "
            "if they appear explicitly in the SERP data provided above.\n"
            "- INFORMATION GAIN: Add 1-2 facts/perspectives that are MISSING from typical articles "
            "on this topic — little-known tips, counterintuitive findings.\n"
            "- STATISTICS: Do NOT state specific percentages or numbers unless they appear in the provided SERP data. "
            "Write 'significantly lower risk' instead of '30% lower risk'. Never invent statistics.\n"
            "- HUMANIZATION: Mix short sentences (5-8 words) with long ones (20-30 words). "
            "Use rhetorical questions, comparisons, specific numeric examples. "
            "Don't write monotonously — vary tone across paragraphs.\n"
            f"CURRENCY: The current year is {_current_year}. Do NOT refer to previous years as current "
            f"(e.g., 'In 2023...', '2024 trends'). Use '{_current_year}' or 'currently/nowadays' for data/statistics. "
            f"Old dates (2023, 2024) only in historical context ('since 2023', 'compared to 2024').\n"
            "STRICT: NO markdown. Never use ## or ### or # at line start. "
            "Never use **text** or *text*. ONLY pure HTML tags: <h2>, <h3>, <p>, <ul>, <li>, <strong>, <em>.\n"
            "STRICT #2: Do NOT generate instructions, tasks, steps, plans or TODO lists. "
            "Do NOT write 'Step 1:', 'Task:', 'Identify', 'Collect data'. "
            "Write FINISHED article content for readers, NOT instructions on how to write it.\n"
            "STRICT #3: Text inside <h2> and <h3> tags must be ONLY a short section title (3-8 words). "
            "NEVER place paragraphs, sentences or body content inside <h2>/<h3> tags. "
            "All article content goes ONLY in <p>, <ul>, <ol> — never inside a heading tag."
        )

    # Semaphore: max 4 concurrent GPT calls for sections (avoid rate limit)
    _sem = asyncio.Semaphore(4)

    async def _generate_section(i: int, heading: str) -> str:
        async with _sem:
            # SEO #11: random sample LSI per section instead of rotating offset (avoids duplicates)
            section_lsi = random.sample(lsi_per_section, min(5, len(lsi_per_section))) if lsi_per_section else []
            lsi_section_block = f"\nSłowa LSI do wplecenia: {', '.join(section_lsi)}" if section_lsi else ""
            if lang_pl:
                section_user = (
                    f"Napisz sekcję dla artykułu '{title}' (keyword: '{topic}').\n"
                    f"Nagłówek sekcji: '{heading}'\n"
                    f"Intencja: {intent_analysis}\n"
                    f"Cel: ~{words_per_section} słów. Użyj frazy '{topic}' lub jej synonimów/odmian MAKSYMALNIE {kw_per_section}x — preferuj synonimy i odmiany naturalne, NIE powtarzaj dokładnej frazy więcej niż 1-2x per sekcja. Unikaj sztucznych powtórzeń.{lsi_section_block}{_entity_block}\n"
                    f"Struktura: <h2>{heading}</h2> → 1-2 <h3> podsekcje → <p> akapity + listy/tabele gdzie sens\n"
                    f"ENCJE: Wprowadź MINIMUM 3 NOWE nazwy własne (instytucje, marki, produkty, programy, aplikacje) nieużyte w poprzednich sekcjach. Sekcja bez 3 nowych encji = thin content.\n"
                    f"E-E-A-T: Podaj MINIMUM 1 konkretne źródło z nazwą i rokiem, np. 'Raport GUS (2024)' lub 'Wytyczne PTK (2023)'. NIE pisz 'badania pokazują' bez nazwy — to AI footprint.\n"
                    f"KEYWORD: użyj frazy '{topic}' MAX {kw_per_section}x w tej sekcji. Przekroczenie = keyword stuffing.\n"
                    f"Pisz ekspercko: konkretne fakty, przykłady praktyczne. Unikaj ogólników.{custom_block}"
                )
            else:
                section_user = (
                    f"Write section for '{title}' (keyword: '{topic}').\n"
                    f"Section heading: '{heading}'\n"
                    f"Intent: {intent_analysis}\n"
                    f"Target: ~{words_per_section} words. Use relevant and detailed information about the '{topic}', its semantic variations and related entities MAX {kw_per_section}x — prefer synonyms, do NOT repeat the exact phrase more than 1-2x per section.{lsi_section_block}{_entity_block}\n"
                    f"Structure: <h2>{heading}</h2> → 1-2 <h3> subsections → <p> + lists/tables where relevant\n"
                    f"ENTITIES: Each section must introduce NEW entities (brands, studies, institutions, people) not already used in previous sections. Do NOT repeat the same 3-4 entities throughout the entire article.\n"
                    f"Write expertly: specific facts, examples, practical tips. Avoid vague generalities and invented statistics.{custom_block}"
                )
            sec_html = await _gpt(section_system, section_user, temperature=0.7, max_tokens=_mt_section, model=_resolved_model)
            if not sec_html.strip().startswith("<"):
                sec_html = _markdown_to_html(sec_html)
            sec_html = _strip_markdown_remnants(sec_html)
            # FIX: detect GPT prompt leakage — task/instruction instead of article
            _leakage = re.search(
                r'(?:Zadanie\s+dotycz|Krok\s+\d+:\s*(Zidentyfikuj|Zbierz|Przeanalizuj|Porównaj|Sformułuj|Przygotuj)'
                r'|Step\s+\d+:\s*(Identify|Collect|Analyze|Compare|Prepare))',
                sec_html,
            )
            if _leakage:
                logger.warning(f"[Article] Prompt leakage detected in section '{heading[:40]}', retrying...")
                _retry_suffix = "\nWAŻNE: Pisz TREŚĆ artykułu, NIE instrukcje ani zadania. NIE pisz 'Krok 1', 'Zadanie'. Pisz gotowy tekst dla czytelnika." if lang_pl else "\nIMPORTANT: Write article CONTENT, NOT instructions or tasks. Do NOT write 'Step 1', 'Task'. Write ready text for the reader."
                sec_html = await _gpt(section_system, section_user + _retry_suffix, temperature=0.8, max_tokens=_mt_section, model=_resolved_model)
                if not sec_html.strip().startswith("<"):
                    sec_html = _markdown_to_html(sec_html)
                sec_html = _strip_markdown_remnants(sec_html)
            logger.info(f"[Article] Section {i+1}/{len(sections)}: {heading[:40]}")
            return sec_html

    # Generate all sections in parallel (max 4 concurrent)
    sections_html = list(await asyncio.gather(*[
        _generate_section(i, heading) for i, heading in enumerate(sections)
    ]))

    # ── STEPS 7-9: Conclusion + FAQ + Excerpt (PARALLEL — independent calls) ──
    # These 3 GPT calls don't depend on each other, running in parallel saves ~4-6s

    # Prepare prompts for all 3 before launching
    _concl_headings_pl = [
        "Podsumowanie", "Wnioski końcowe", "Co warto zapamiętać?",
        "Najważniejsze informacje", "Kluczowe wnioski", "Na zakończenie",
    ]
    _concl_headings_en = [
        "Summary", "Final Thoughts", "Key Takeaways",
        "What to Remember", "In Conclusion", "Wrapping Up",
    ]
    _concl_h2 = random.choice(_concl_headings_pl if lang_pl else _concl_headings_en)
    if lang_pl:
        conclusion_user = (
            f"Napisz zakończenie artykułu '{title}' (keyword: '{topic}').\n"
            f"Omówione tematy: {', '.join(sections[:5])}\n"
            f"STRUKTURA:\n"
            f"<h2>{_concl_h2}</h2>\n"
            f"- Akapit 1: główne wnioski (bullet points w <ul> lub tekst)\n"
            f"- Akapit 2: praktyczne zastosowanie / co teraz zrobić\n"
            f"- Akapit 3 (opcjonalny): CTA lub pytanie do czytelnika\n"
            f"Użyj '{topic}' 1-2x. Konkretne wnioski, nie ogólniki."
        )
    else:
        conclusion_user = (
            f"Write conclusion for '{title}' (keyword: '{topic}').\n"
            f"Topics covered: {', '.join(sections[:5])}\n"
            f"STRUCTURE:\n"
            f"<h2>{_concl_h2}</h2>\n"
            f"- Para 1: key takeaways (bullets in <ul> or prose)\n"
            f"- Para 2: practical next steps\n"
            f"- Para 3 (optional): CTA or question for readers\n"
            f"Use '{topic}' 1-2x. Specific conclusions, not generalities."
        )
    _concl_system = (
        "Jesteś ekspertem SEO. Piszesz zakończenie artykułu w HTML.\n"
        "BEZWZGLĘDNY ZAKAZ: NIE używaj markdown. NIE pisz ## ani ### ani # ani **tekst**. "
        "TYLKO tagi HTML: <h2>, <p>, <ul>, <li>, <strong>.\n"
        "STATYSTYKI: NIE podawaj konkretnych % ani liczb jeśli nie masz ich z danych SERP. Pisz ogólnie.\n"
        "ENCJE OSOBOWE: NIE podawaj z pamięci nazwisk polityków, ministrów, prezydentów ani innych urzędników.\n"
        "DUPLIKATY: NIE powtarzaj informacji ani zdań z wcześniejszych sekcji. Wnioski powinny dodawać nową perspektywę lub praktyczne wskazówki. Zakończenie wnosi nową perspektywę, wnioski lub praktyczne wskazówki których nie było wcześniej."
    ) if lang_pl else (
        "You are an SEO expert. Write article conclusion in HTML.\n"
        "STRICT: NO markdown. Never use ## or ### or # or **text**. "
        "ONLY HTML tags: <h2>, <p>, <ul>, <li>, <strong>.\n"
        "STATISTICS: Do NOT state specific percentages or numbers unless from provided SERP data. Write generally.\n"
        "PERSONAL ENTITIES: Do NOT name from memory any politicians, ministers, presidents or other officeholders.\n"
        "DUPLICATES: Do NOT repeat sentences or facts that appeared earlier in the article. The conclusion must introduce a new angle, highlight statistics or insights not previously emphasized, and offer practical applications."
    )

    paa_block = ""
    if paa_questions:
        paa_block = ("\nPytania z Google PAA (użyj tych jako baza):\n" if lang_pl
                     else "\nReal Google PAA questions (use as base):\n") + "\n".join(f"- {q}" for q in paa_questions[:6])
    _faq_headings_pl = [
        "Najczęściej zadawane pytania (FAQ)",
        "Pytania i odpowiedzi",
        "FAQ — co warto wiedzieć?",
        "Odpowiedzi na najczęstsze pytania",
        "To pytają użytkownicy",
    ]
    _faq_headings_en = [
        "Frequently Asked Questions (FAQ)",
        "Questions & Answers",
        "FAQ — What You Should Know",
        "Common Questions Answered",
        "People Also Ask",
    ]
    _faq_h2 = random.choice(_faq_headings_pl if lang_pl else _faq_headings_en)
    if lang_pl:
        faq_user = (
            f"Stwórz sekcję FAQ dla artykułu o '{topic}'.\n"
            f"WYMAGANIA:\n"
            f"- 8 pytań i odpowiedzi\n"
            f"- Pierwsze pytanie = definicja/wyjaśnienie '{topic}'\n"
            f"- Odpowiedzi: 2-4 zdania, konkretne i szczegółowe, zawierające praktyczne przykłady oraz poparte danymi i badaniami, unikając ogólności. Uwzględnij pytania dotyczące danych liczbowych, przypadków użycia oraz porównań.\n"
            f"- Mix: pytania informacyjne + praktyczne + porównawcze{paa_block}\n"
            f"HTML: <h2>{_faq_h2}</h2>\n"
            f"Format każdej pary: <h3>Pytanie?</h3><p>Odpowiedź.</p>\n"
            f"BEZWZGLĘDNY ZAKAZ: NIE używaj markdown. TYLKO HTML."
        )
    else:
        faq_user = (
            f"Create FAQ section for article about '{topic}'.\n"
            f"REQUIREMENTS:\n"
            f"- 8 questions and answers\n"
            f"- First question = definition/explanation of '{topic}'\n"
            f"- Answers: 2-4 sentences, specific, no filler\n"
            f"- Mix: informational + practical + comparative questions{paa_block}\n"
            f"HTML: <h2>{_faq_h2}</h2>\n"
            f"Each pair: <h3>Question?</h3><p>Answer.</p>\n"
            f"STRICT: NO markdown. ONLY HTML."
        )
    _faq_system = (
        "Jesteś ekspertem SEO. Tworzysz FAQ zoptymalizowane pod featured snippets, AI Overview i PAA (People Also Ask). "
        "NIE podawaj z pamięci nazwisk polityków, ministrów, prezydentów ani innych urzędników — użyj ogólnych ról. "
        "STATYSTYKI: NIE podawaj konkretnych % ani liczb jeśli nie ma ich w danych SERP. Nigdy nie wymyślaj statystyk."
        if lang_pl else
        "You are an SEO expert creating FAQ optimized for featured snippets, AI Overview, and PAA (People Also Ask). "
        "Do NOT name from memory any politicians, ministers, presidents or other officeholders — use generic roles. "
        "STATISTICS: Do NOT state specific percentages or numbers unless from the provided SERP data. Never invent statistics."
    )
    # SEO #5: intent-based CTA for meta description
    _cta_map_pl = {
        "informational": "Dowiedz się więcej", "transactional": "Sprawdź ofertę",
        "commercial": "Porównaj opcje", "navigational": "Przejdź na stronę",
    }
    _cta_map_en = {
        "informational": "Learn more", "transactional": "Shop now",
        "commercial": "Compare options", "navigational": "Visit the page",
    }
    _cta_hint = _cta_map_pl.get(_intent_type, "Sprawdź") if lang_pl else _cta_map_en.get(_intent_type, "Learn more")
    if lang_pl:
        excerpt_user = (
            f"Napisz meta description dla artykułu '{title}' o '{topic}'.\n"
            f"WYMAGANIA: max 155 znaków, bez HTML, zawiera '{topic}', kończy się CTA: '{_cta_hint}'.\n"
            f"Tylko tekst meta description, nic więcej."
        )
    else:
        excerpt_user = (
            f"Write meta description for '{title}' about '{topic}'.\n"
            f"REQUIREMENTS: max 155 chars, no HTML, includes '{topic}', ends with CTA: '{_cta_hint}'.\n"
            f"Only the meta description text, nothing else."
        )

    # Launch all 3 in parallel
    conclusion_raw, faq_raw, excerpt_raw = await asyncio.gather(
        _gpt(_concl_system, conclusion_user, temperature=0.7, max_tokens=_mt_conc, model=_resolved_model),
        _gpt(_faq_system, faq_user, temperature=0.6, max_tokens=_mt_faq, model=_resolved_model),
        _gpt(
            "Jesteś SEO copywriterem." if lang_pl else "You are an SEO copywriter.",
            excerpt_user, temperature=0.5, max_tokens=_mt_excerpt, model=_resolved_model
        ),
    )

    # Post-process conclusion
    conclusion_html = conclusion_raw
    if not conclusion_html.strip().startswith("<"):
        conclusion_html = _markdown_to_html(conclusion_html)
    conclusion_html = _strip_markdown_remnants(conclusion_html)

    # Post-process FAQ
    faq_html = faq_raw
    if not faq_html.strip().startswith("<"):
        faq_html = _markdown_to_html(faq_html)
    faq_html = _strip_markdown_remnants(faq_html)
    # SEO #13: add IDs to FAQ h3 headings for anchor linking
    _faq_idx = [0]
    def _add_faq_id(m):
        _faq_idx[0] += 1
        attrs = m.group(1) or ""
        content_h3 = m.group(2)
        _slug = _slugify_heading(re.sub(r'<[^>]+>', '', content_h3))
        return f'<h3 id="faq-{_slug}"{attrs}>{content_h3}</h3>'
    faq_html = re.sub(r'<h3([^>]*)>(.*?)</h3>', _add_faq_id, faq_html, flags=re.DOTALL | re.IGNORECASE)

    # Post-process excerpt
    excerpt = excerpt_raw.strip('"\'').strip()
    # FIX #9: enforce 150-155 char limit for meta description (Google truncates at ~155)
    if len(excerpt) > 155:
        excerpt = excerpt[:155].rsplit(' ', 1)[0].rstrip('.,;:') + '.'
    # FIX #10: ensure excerpt doesn't start with "Meta description:" or similar GPT artifacts
    excerpt = re.sub(r'^(?:Meta\s*(?:description|opis)\s*:?\s*)', '', excerpt, flags=re.IGNORECASE).strip()
    logger.info("[Article] Conclusion + FAQ + Excerpt done (parallel)")

    # ── STEP 10: Assemble — apply layout variant ──────────────────────────────
    # NOTE: No <h1> in content — WordPress theme renders post title as H1 already.
    # Adding <h1> here would create duplicate H1 on the page.

    if layout_variant == "faq_top":
        # FAQ at the very top (30% of articles)
        content_parts = [faq_html, intro_html] + sections_html + [conclusion_html]
    elif layout_variant == "tldr":
        # TL;DR box right at the start (20%)
        tldr_label = "W skrócie" if lang_pl else "TL;DR"
        tldr_sentence = excerpt[:200] if excerpt else ""
        tldr_box = (
            f'<div style="background:#e8f0fe;border-left:4px solid #1a73e8;padding:12px 18px;'
            f'margin:16px 0 24px;border-radius:0 8px 8px 0;">'
            f'<strong>{tldr_label}:</strong> {tldr_sentence}</div>'
        ) if tldr_sentence else ""
        content_parts = [tldr_box, intro_html] + sections_html + [conclusion_html, faq_html]
    elif layout_variant == "short_answer":
        # Featured snippet box — first sentence only (definition).
        # Then intro_html with that first sentence STRIPPED to avoid literal duplication.
        sa_intro_plain = re.sub(r'<[^>]+>', '', intro_html).strip()
        first_dot = sa_intro_plain.find('.')
        sa_text = sa_intro_plain[:first_dot + 1].strip() if first_dot > 0 and first_dot < 300 else sa_intro_plain[:200]
        sa_box = (
            f'<div style="background:#f0fdf4;border:1px solid #86efac;padding:16px 20px;'
            f'margin:16px 0 24px;border-radius:8px;">'
            f'{sa_text}</div>'
        ) if sa_text else ""
        # Remove the first <p> from intro_html if it starts with the same sentence as sa_box
        intro_html_trimmed = intro_html
        if sa_text:
            # Strip first <p>...</p> block if its text content starts with sa_text (or close match)
            first_p_match = re.match(r'\s*<p[^>]*>(.*?)</p>', intro_html, re.DOTALL | re.IGNORECASE)
            if first_p_match:
                first_p_text = re.sub(r'<[^>]+>', '', first_p_match.group(1)).strip()
                if first_p_text.startswith(sa_text[:60]):
                    intro_html_trimmed = intro_html[first_p_match.end():].lstrip()
        content_parts = [sa_box, intro_html_trimmed] + sections_html + [conclusion_html, faq_html]
    else:
        # Standard layout
        content_parts = [intro_html] + sections_html + [conclusion_html, faq_html]

    # SEO #34: content freshness signal — visible "last updated" date
    _freshness_date = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    _freshness_label = "Ostatnia aktualizacja" if lang_pl else "Last updated"
    _freshness_tag = f'<p style="font-size:0.85em;color:#666;margin-bottom:16px;"><time datetime="{datetime.now(timezone.utc).strftime("%Y-%m-%d")}">{_freshness_label}: {_freshness_date}</time></p>'
    content_parts.insert(0, _freshness_tag)

    content = "\n\n".join(p for p in content_parts if p)

    # SEO #14: wrap content in lang div for better NLP entity recognition
    content = f'<div lang="{language}">\n{content}\n</div>'

    # Inject external anchor links
    content = _inject_anchors(content, anchors_info, language=language)

    # Inject internal links to already-published posts on this domain
    if published_posts:
        content = _inject_internal_links(content, published_posts, topic, language=language)

    # Inject 1-2 authority outbound links (Wikipedia, .gov) for natural link profile
    content = _inject_authority_links(content, topic, language=language)

    # SEO #130: "Related articles" section at bottom — 5 published posts for interlinking
    if published_posts:
        # Pick up to 5 posts that are NOT the current topic
        _related = [p for p in published_posts
                    if (p.get("keyword", "") or p.get("title", "")).strip().lower() != topic.strip().lower()
                    and p.get("url")]
        random.shuffle(_related)
        _related = _related[:5]
        if _related:
            _rel_heading = "Podobne artykuły" if lang_pl else "Related Articles"
            _rel_items = []
            for _rp in _related:
                _rp_title = _rp.get("title") or _rp.get("keyword", "")
                _rp_url = _rp["url"]
                _rel_items.append(f'<li><a href="{_rp_url}">{_rp_title}</a></li>')
            _rel_html = (
                f'<div style="margin-top:2em;padding:1.2em 1.5em;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;">'
                f'<h2 style="font-size:1.15em;margin:0 0 0.7em 0;">{_rel_heading}</h2>'
                f'<ul style="margin:0;padding-left:1.2em;">{"".join(_rel_items)}</ul>'
                f'</div>'
            )
            content += "\n\n" + _rel_html

    # Final strip of any remaining ## markdown before enrichment
    content = _strip_markdown_remnants(content)

    # Enrich with random unique elements (3-4 per article)
    _enrich_client, _, _enrich_is_custom = await get_openai_client()
    content = await enrich_article(content, topic, sections, lang_pl=(language == "pl"), openai_client=_enrich_client, is_custom_llm=_enrich_is_custom, serp_urls=serp_urls)

    # Final strip again after enrichment (GPT content in enrichments may also have ##)
    content = _strip_markdown_remnants(content)

    # SEO #89: add loading="lazy" and decoding="async" to all inline images
    content = re.sub(r'<img(?!\s[^>]*loading=)\s', '<img loading="lazy" decoding="async" ', content)

    # Fix heading hierarchy (H3 before H2, skipped levels, etc.)
    content = _fix_heading_hierarchy(content)

    # Post-processing: remove AI-fingerprint template phrases
    _AI_PHRASES_PL = [
        r"W\s+praktyce\s+często\s+spotykamy",
        r"Z\s+doświadczenia\s+wynika",
        r"Typow[yi]\s+błąd\s+to",
        r"Warto\s+zauważyć,?\s+że",
        r"Warto\s+podkreślić,?\s+że",
        r"Należy\s+zaznaczyć,?\s+że",
        r"Nie\s+ulega\s+wątpliwości,?\s+że",
        r"Co\s+więcej,",
        r"Podsumowując,",
        r"W\s+kontekście\s+(?:tego|powyższego),",
        r"Warto\s+dodać,?\s+że",
        r"Należy\s+pamiętać,?\s+że",
        r"Jak\s+już\s+wspomniano,",
        r"W\s+tym\s+miejscu\s+warto",
        r"Jak\s+wynika\s+z\s+powyższego",
        r"Warto\s+również\b",
        r"Warto\s+zacząć\s+od",
        r"Warto\s+zwrócić\s+uwagę",
        r"W\s+dzisiejszym\s+(?:zabieganym|dynamicznym|nowoczesnym)\s+świecie",
        r"kluczow[ya]\s+(?:element|rola|aspekt|kwestia)",
        r"odgrywa\s+kluczow[aą]\s+rolę",
        r"ma\s+kluczowe\s+znaczenie",
        r"nie\s+można\s+(?:przecenić|zapominać\s+o)",
        r"jest\s+nieodłączn[ym]\s+elementem",
    ]
    _AI_PHRASES_EN = [
        r"In\s+practice,?\s+we\s+often\s+(?:see|encounter)",
        r"It\s+is\s+worth\s+(?:noting|mentioning)\s+that",
        r"It\s+should\s+be\s+noted\s+that",
        r"A\s+common\s+mistake\s+is",
        r"Furthermore,",
        r"In\s+summary,",
        r"In\s+the\s+context\s+of",
        r"It\s+goes\s+without\s+saying",
        r"Needless\s+to\s+say,",
        r"As\s+mentioned\s+above,",
        r"As\s+previously\s+mentioned,",
    ]
    _phrases = _AI_PHRASES_PL if lang_pl else _AI_PHRASES_EN
    for _phrase_pat in _phrases:
        content = re.sub(r'(?i)' + _phrase_pat + r'\s*', '', content)
    # Fix orphaned punctuation/conjunctions after phrase removal
    content = re.sub(r'(<p[^>]*>)\s*,\s*', r'\1', content)
    # Capitalize first word after <p> if it starts lowercase (result of phrase removal)
    content = re.sub(r'(<p[^>]*>)\s*([a-ząćęłńóśźż])', lambda m: m.group(1) + m.group(2).upper(), content)
    # Capitalize after ". " if next word is lowercase (phrase removed mid-sentence)
    content = re.sub(r'(\.\s+)([a-ząćęłńóśźża-z])', lambda m: m.group(1) + m.group(2).upper(), content)

    # Post-processing: limit <strong> usage — max 8 total in entire article
    # GPT abuses <strong> on every keyword variant and brand name — cap it hard
    _strong_all = re.findall(r'<strong>.*?</strong>', content, flags=re.DOTALL | re.IGNORECASE)
    if len(_strong_all) > 8:
        _strong_counter = [0]
        def _limit_strong(m: re.Match) -> str:
            _strong_counter[0] += 1
            if _strong_counter[0] <= 8:
                return m.group(0)
            return re.sub(r'</?strong>', '', m.group(0))
        content = re.sub(r'<strong>.*?</strong>', _limit_strong, content, flags=re.DOTALL | re.IGNORECASE)

    # FIX: strip body content accidentally placed inside h2/h3 tags
    def _clean_heading_openai(m: re.Match) -> str:
        tag = m.group(1)
        attrs = m.group(2)
        inner = m.group(3)
        plain = re.sub(r"<[^>]+>", " ", inner).strip()
        plain = re.sub(r"\s+", " ", plain).strip()
        if len(plain) > 100:
            dot = plain.find(". ")
            plain = plain[:dot].strip() if dot > 10 else plain[:80].rsplit(" ", 1)[0]
        return f"<{tag}{attrs}>{plain}</{tag}>"
    content = re.sub(r"<(h[23])([^>]*)>(.*?)</h[23]>", _clean_heading_openai, content, flags=re.DOTALL | re.IGNORECASE)

    # FIX #51: deduplicate anchor links and also remove empty/broken links
    seen_hrefs: set = set()

    def _dedup_link(m: re.Match) -> str:
        href = re.search(r'href=["\']([^"\']+)["\']', m.group(0))
        if not href:
            return m.group(0)
        url = href.group(1).rstrip("/")
        # FIX #52: remove links with empty/invalid href
        if not url or url in ("#", "javascript:void(0)", ""):
            return re.sub(r"<[^>]+>", "", m.group(0))
        if url in seen_hrefs:
            return re.sub(r"<[^>]+>", "", m.group(0))
        seen_hrefs.add(url)
        return m.group(0)

    content = re.sub(r'<a\s[^>]*?>.*?</a>', _dedup_link, content, flags=re.DOTALL | re.IGNORECASE)

    # ── Schema JSON-LD — FAQPage + Article + Person + Organization ──────────
    # FAQPage JSON-LD — Yoast/RankMath only auto-generate FAQ schema from their own
    # block types, not from raw <h3>/<p> HTML. Inject it explicitly for rich snippets.
    schema_blocks = []

    _include_faq_schema = random.random() < 0.15  # Only 15% of articles get FAQPage schema — avoids schema spam
    faq_pairs = re.findall(r'<h3[^>]*>(.*?)</h3>\s*<p>(.*?)</p>', content, re.DOTALL | re.IGNORECASE)
    if _include_faq_schema and faq_pairs and len(faq_pairs) >= 3:
        faq_ld = {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": re.sub(r'<[^>]+>', '', q).strip(),
                    "acceptedAnswer": {
                        "@type": "Answer",
                        "text": re.sub(r'<[^>]+>', '', a).strip()
                    }
                }
                for q, a in faq_pairs[:8]
            ]
        }
        schema_blocks.append(faq_ld)

    # Article JSON-LD — confirmed ranking signal (Google API leak: siteAuthority + entity signals)
    _now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    # SEO #62 + SEO #118: publisher should be PBN domain, not client domain (avoids footprint)
    # client_domain is the money site — publisher should be the PBN domain where article is hosted
    _publisher_name = "Publisher"
    _pub_src = pbn_domain or ""
    if _pub_src and _pub_src.strip():
        _publisher_name = _pub_src.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]

    # Use Organization (not Person) — PBN articles are published by the site, not fake individuals
    _author_entity: dict[str, str] = {
        "@type": "Organization",
        "name": _publisher_name,
    }
    if _pub_src and _pub_src.strip():
        _author_entity["url"] = clean_url(_pub_src)

    # SEO #101: extract first image from content for Article schema image field
    _first_img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content, re.IGNORECASE)
    _article_image = _first_img_match.group(1) if _first_img_match else None

    article_ld = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title[:110],
        "description": excerpt[:155] if excerpt else "",
        "datePublished": _now_iso,
        "dateModified": _now_iso,
        "author": _author_entity,
        "publisher": {
            "@type": "Organization",
            "name": _publisher_name,
        },
        "mainEntityOfPage": {
            "@type": "WebPage",
        },
        # SEO #4: speakable property — first paragraph as voice search target
        "speakable": {
            "@type": "SpeakableSpecification",
            "cssSelector": ["div[lang] > p:first-of-type", "div[lang] > p:nth-of-type(2)"],
        },
        "wordCount": _count_words(content),
        "articleSection": topic,
        "inLanguage": language,
    }
    # SEO #101: add image to Article schema (required for Google rich results)
    if _article_image:
        article_ld["image"] = _article_image
    # SEO #116: isPartOf for supporting pages linked to pillar
    if pillar_page_url:
        article_ld["isPartOf"] = {
            "@type": "WebPage",
            "url": pillar_page_url,
            "name": pillar_page_anchor or topic,
        }
    schema_blocks.append(article_ld)

    # SEO #104: VideoObject schema for YouTube embeds in content
    _yt_embeds = re.findall(r'(?:src=["\'])(?:https?://)?(?:www\.)?(?:youtube\.com/embed/|youtu\.be/)([a-zA-Z0-9_-]{11})', content)
    if _yt_embeds:
        for _yt_id in _yt_embeds[:2]:
            video_ld = {
                "@context": "https://schema.org",
                "@type": "VideoObject",
                "name": title[:110],
                "description": excerpt[:155] if excerpt else topic,
                "thumbnailUrl": f"https://img.youtube.com/vi/{_yt_id}/maxresdefault.jpg",
                "uploadDate": _now_iso,
                "embedUrl": f"https://www.youtube.com/embed/{_yt_id}",
            }
            schema_blocks.append(video_ld)

    # SEO #1: HowTo Schema for how-to intent articles
    _howto_patterns = ["jak ", "how to ", "krok po kroku", "step by step", "poradnik", "guide", "tutorial"]
    if any(p in topic.lower() for p in _howto_patterns):
        # SEO #63: only extract steps from <ol> lists (not <ul> bullet lists)
        _ol_blocks = re.findall(r'<ol>(.*?)</ol>', content, re.DOTALL | re.IGNORECASE)
        _steps_raw = []
        for _ol in _ol_blocks:
            _steps_raw.extend(re.findall(r'<li>(.*?)</li>', _ol, re.DOTALL))
        if len(_steps_raw) >= 3:
            howto_ld = {
                "@context": "https://schema.org",
                "@type": "HowTo",
                "name": title,
                "description": excerpt[:200] if excerpt else "",
                "step": [
                    {"@type": "HowToStep", "name": re.sub(r'<[^>]+>', '', step).strip()[:100], "text": re.sub(r'<[^>]+>', '', step).strip()}
                    for step in _steps_raw[:10]
                ],
            }
            schema_blocks.append(howto_ld)

    # SEO #2: BreadcrumbList Schema
    breadcrumb_ld = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": "/"},
            {"@type": "ListItem", "position": 2, "name": topic[:50]},
        ],
    }
    schema_blocks.append(breadcrumb_ld)

    # SEO #126: validate JSON-LD schema blocks before injection (catch malformed data)
    _valid_schema_blocks = []
    for _sb in schema_blocks:
        try:
            _json.dumps(_sb, ensure_ascii=False)  # test serialization
            if "@context" in _sb and "@type" in _sb:
                _valid_schema_blocks.append(_sb)
            else:
                logger.warning(f"[Article] Skipping schema block without @context/@type: {list(_sb.keys())}")
        except (TypeError, ValueError) as _json_err:
            logger.warning(f"[Article] Invalid JSON-LD schema skipped: {_json_err}")

    # Inject all validated schema blocks
    if _valid_schema_blocks:
        all_schema = "\n".join(
            f'<script type="application/ld+json">{_json.dumps(s, ensure_ascii=False)}</script>'
            for s in _valid_schema_blocks
        )
        content = all_schema + "\n" + content

    # Dedup fingerprint
    fingerprint = _content_fingerprint(content)
    if domain_fingerprints is not None and fingerprint in domain_fingerprints:
        logger.warning(f"[Article] Duplicate fingerprint detected for '{topic}' — variation hint applied")
    if domain_fingerprints is not None:
        domain_fingerprints.add(fingerprint)

    word_count = _count_words(content)
    _elapsed = round(time.time() - _t0, 1)
    logger.info(f"[Article] Done — '{title}' | {len(sections)} sekcji | {word_count} słów | fp={fingerprint[:8]} | {_elapsed}s")

    # FIX #53: minimum article length validation — warn if below 600 or significantly below target
    if word_count < 600:
        logger.warning(f"[Article] Short article ({word_count} words) for '{topic}' — may indicate GPT truncation")
    elif word_count < target_words * 0.6:
        logger.warning(f"[Article] Article under 60% target ({word_count}/{target_words} words) for '{topic}'")

    # FIX #11: compute keyword density of final article for quality monitoring
    final_density = _keyword_density(content, topic)
    if final_density < 0.5 or final_density > 2.0:
        logger.warning(f"[Article] KW density out of range: {final_density:.2f}% for '{topic}'")

    # SEO #19: expand tags — LSI + pillar label + intent type for richer WP taxonomy
    _all_tags = lsi_terms[:7]  # 7 LSI tags (was 5)
    if pillar_page_anchor:
        _all_tags.append(pillar_page_anchor)
    if _intent_type != "informational":
        _all_tags.append(_intent_type)
    # Dedupe and cap at 10
    _seen_tags = set()
    _unique_tags = []
    for _t in _all_tags:
        _tl = _t.lower().strip()
        if _tl and _tl not in _seen_tags:
            _seen_tags.add(_tl)
            _unique_tags.append(_t)
    _unique_tags = _unique_tags[:10]

    return {
        "title": title,
        "seo_title": seo_title,  # truncated for meta title / Yoast / RankMath (max 65 chars)
        "content": content,
        "excerpt": excerpt,
        "fingerprint": fingerprint,
        "lsi_tags": _unique_tags,  # SEO #19: 7-10 tags instead of 5
        "word_count": word_count,
        "keyword_density": final_density,
        "intent_type": _intent_type,  # SEO #5: expose intent for quality monitoring
    }


async def describe_image_and_generate(image_b64: str, topic: str) -> str:
    try:
        _model = await get_gpt_model()
        vision_response = await client.chat.completions.create(
            model=_model,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": f"Opisz krótko co widać na tym screenshocie (max 2 zdania). Kontekst: '{topic}'."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ]}],
            max_tokens=200,
        )
        description = (vision_response.choices[0].message.content or "").strip()
        return await generate_image(f"Professional illustration: {description}. Clean, modern design.")
    except Exception as e:
        logger.error(f"[Image] describe_image_and_generate failed: {e}")
        return ""


async def generate_image(prompt: str) -> Optional[str]:
    try:
        response = await client.images.generate(
            model="dall-e-3", prompt=prompt, n=1,
            size="1792x1024", response_format="b64_json",
        )
        return response.data[0].b64_json
    except Exception as e:
        logger.error(f"[Image] generate_image failed: {e}")
        return None

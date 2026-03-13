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
)

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
logger = logging.getLogger(__name__)

# Default GPT model — can be overridden via GPT_MODEL env var or DB settings
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")

# In-memory cache for GPT model (avoids DB query on every GPT call)
_gpt_model_cache: dict = {"model": None, "ts": 0}
_GPT_MODEL_CACHE_TTL = 120  # seconds


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
    except Exception:
        pass
    return GPT_MODEL


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

    cache_key = f"{topic.lower().strip()}:{location_code}:{language_code}"
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
            all_text = ""
            contents = await asyncio.gather(*[dfs.page_content(url, _client=_dfs_client) for url in blog_urls], return_exceptions=True)

        for url, content in zip(blog_urls, contents):
            if isinstance(content, Exception) or not content:
                continue
            wc = _count_words(content)
            dens = _keyword_density(content, topic)
            word_counts.append(wc)
            densities.append(dens)
            all_text += " " + content
            parts.append(f"--- {url} ({wc} słów, gęstość KW: {dens}%) ---\n{content[:3000]}")

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


async def _gpt(system: str, user: str, temperature: float = 0.7, max_tokens: int = 2000, model: str = None) -> str:
    if model is None:
        model = await get_gpt_model()
    for attempt in range(3):
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            if attempt == 2:
                raise
            wait = 2 ** attempt
            logger.warning(f"[GPT] attempt {attempt+1} failed: {e} — retrying in {wait}s")
            await asyncio.sleep(wait)
    return ""  # unreachable



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

        # Fix: heading level skips more than 1 level down from last
        if level > last_level + 1:
            new_level = last_level + 1
            new_tag = f"h{new_level}"
            replacements.append((m.start(), m.end(), m.group(0),
                                 f"<{new_tag}{attrs}>{content}</{new_tag}>"))
            last_level = new_level
        else:
            last_level = level

    # Apply replacements in reverse order to preserve positions
    result = html
    for start, end, old, new in reversed(replacements):
        result = result[:start] + new + result[end:]

    return result




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

    # Vary the surrounding context — anti-footprint, language-aware
    if language == "pl":
        _LINK_CONTEXTS = [
            lambda lnk: f" Więcej na ten temat znajdziesz na stronie {lnk}.",
            lambda lnk: f" Szczegółowe informacje dostępne są pod adresem {lnk}.",
            lambda lnk: f" Warto odwiedzić serwis {lnk}, gdzie znajdziesz więcej materiałów.",
            lambda lnk: f" Dodatkowe zasoby: {lnk}.",
            lambda lnk: f" Polecamy również stronę {lnk}.",
            lambda lnk: f" Temat szczegółowo omawia {lnk}.",
        ]
    else:
        _LINK_CONTEXTS = [
            lambda lnk: f" Find more information at {lnk}.",
            lambda lnk: f" Detailed resources are available at {lnk}.",
            lambda lnk: f" We recommend visiting {lnk} for additional materials.",
            lambda lnk: f" Additional resources: {lnk}.",
            lambda lnk: f" Also check out {lnk}.",
            lambda lnk: f" This topic is covered in depth at {lnk}.",
        ]
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
        if para and 'href=' not in para and link not in html:
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
        link = f'<a href="{url}" title="{title}">{anchor_text}</a>'
        ctx = random.choice(_int_contexts)
        new_para = best_para[:-4] + ctx(link) + "</p>"
        html = html.replace(best_para, new_para, 1)
        used_urls.add(url)
        used_paras.add(best_para)
        injected += 1

    if injected:
        logger.info(f"[Article] Injected {injected}/{max_links} internal links (word_count={word_count})")
    return html


# Anchor text rotation pools — reduces footprint, more natural link profile
_ANCHOR_GENERIC_PL = ["tutaj", "sprawdź", "dowiedz się więcej", "więcej informacji", "przeczytaj więcej", "na tej stronie"]
_ANCHOR_GENERIC_EN = ["here", "check it out", "learn more", "find out more", "read more", "on this page"]


def _rotate_anchor(anchor_text: str, client_domain: str, language: str = "pl") -> str:
    """
    Rotate anchor text to avoid footprint — RANDOM per article call.
    Distribution: 35% exact match, 20% brand/naked URL, 25% partial/topic, 20% generic
    Uses os.urandom for true randomness (not deterministic hash).
    """
    # Use os.urandom for non-deterministic rotation per article
    bucket = int.from_bytes(os.urandom(1), "big") % 100
    if bucket < 35:
        # Exact match — keep as-is
        return anchor_text
    elif bucket < 55:
        # Naked URL / brand name (strip www, use domain root)
        domain = client_domain.replace("https://", "").replace("http://", "").rstrip("/").split("/")[0]
        return domain.replace("www.", "")
    elif bucket < 80:
        # Partial match — first word(s) of anchor
        words = anchor_text.split()
        return " ".join(words[:max(1, len(words) - 1)]) if len(words) > 1 else anchor_text
    else:
        # Generic
        generics = _ANCHOR_GENERIC_PL if language == "pl" else _ANCHOR_GENERIC_EN
        return random.choice(generics)


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
) -> dict:
    _t0 = time.time()

    # Resolve GPT model once for entire article generation (avoids 8+ cache lookups)
    _resolved_model = await get_gpt_model()

    def clean_url(url: str) -> str:
        url = url.strip()
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url
        return url

    # Anchor text for link 1: use exact anchor_text if explicitly provided by user.
    # Rotation only happens when anchor equals the raw topic (autopilot default) — not when user typed it.
    anchors_info = ""
    if client_domain and client_domain.strip():
        # Only rotate if anchor_text looks like an auto-generated default (same as topic or empty)
        if not anchor_text or anchor_text.strip().lower() == topic.strip().lower():
            rotated_anchor = _rotate_anchor(anchor_text or topic, client_domain, language)
        else:
            rotated_anchor = anchor_text  # user provided explicit anchor — use as-is
        anchors_info = f'<a href="{clean_url(client_domain)}">{rotated_anchor}</a>'
    if anchor_text2 and anchor_url2:
        anchors_info += f', <a href="{clean_url(anchor_url2)}">{anchor_text2}</a>'
    if anchor_text3 and anchor_url3:
        anchors_info += f', <a href="{clean_url(anchor_url3)}">{anchor_text3}</a>'
    # PBN inter-link: supporting page → pillar page (internal, no rotation)
    if pillar_page_url and pillar_page_anchor:
        anchors_info += f', <a href="{clean_url(pillar_page_url)}">{pillar_page_anchor}</a>'

    _current_year = datetime.now(timezone.utc).year
    variation = f" Kąt tematyczny: {variation_hint}." if variation_hint else ""
    custom_block = f"\nDodatkowe wymagania: {custom_prompt}" if custom_prompt else ""
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

    target_words = max(800, avg_words)
    target_density = round(max(0.5, min(3.0, avg_density)), 1)
    lsi_block = f"\nSłowa semantyczne LSI do użycia: {', '.join(lsi_terms[:15])}" if lsi_terms else ""
    serp_block = f"\n\n[SEO Scraped Info — top 3 konkurentów]\n{serp_text}" if serp_text else ""

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
        intent_user, temperature=0.3, max_tokens=400, model=_resolved_model
    )
    logger.info(f"[Article] Intent: {intent_analysis[:80]}")

    # ── STEP 3: Outline ───────────────────────────────────────────────────────
    n_sections = max(4, min(8, round(target_words / 200)))
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
        outline_user, temperature=0.5, max_tokens=500, model=_resolved_model
    )
    sections = [s.strip() for s in outline_raw.split("<<<<") if s.strip()]
    if not sections:
        sections = [topic]
    logger.info(f"[Article] Sections ({len(sections)}): {sections}")

    # ── STEP 4: Title ─────────────────────────────────────────────────────────
    if lang_pl:
        title_user = (
            f"Wymyśl unikalny tytuł SEO dla frazy: '{topic}'\n"
            f"Sekcje artykułu: {', '.join(sections[:3])}\n"
            f"ZASADY: 50-60 znaków, zawiera '{topic}', przyciąga uwagę.\n"
            f"Użyj jednego z formatów:\n"
            f"- '[Keyword] — kompletny przewodnik {_current_year}'\n"
            f"- 'Jak [działanie związane z keyword]? [X] kroków'\n"
            f"- 'Co to jest [keyword] i jak [korzyść]?'\n"
            f"- '[X] najważniejszych faktów o [keyword]'\n"
            f"- '[Keyword]: wszystko co musisz wiedzieć'\n"
            f"Tylko tytuł, bez cudzysłowów, bez markdown.{custom_block}"
        )
    else:
        title_user = (
            f"Create unique SEO title for: '{topic}'\n"
            f"Article sections: {', '.join(sections[:3])}\n"
            f"RULES: 50-60 characters, contains '{topic}', attention-grabbing.\n"
            f"Use one of these formats:\n"
            f"- '[Keyword] — Complete Guide {_current_year}'\n"
            f"- 'How to [action related to keyword]? [X] Steps'\n"
            f"- 'What is [keyword] and how does it [benefit]?'\n"
            f"- '[X] Key Facts About [keyword]'\n"
            f"- '[Keyword]: Everything You Need to Know'\n"
            f"Only the title, no quotes, no markdown.{custom_block}"
        )
    title = await _gpt(
        "Jesteś copywriterem SEO." if lang_pl else "You are an SEO copywriter.",
        title_user, temperature=0.8, max_tokens=100, model=_resolved_model
    )
    title = title.strip('"\'').strip()
    logger.info(f"[Article] Title: {title}")

    # ── STEP 5: Intro (direct answer first) ──────────────────────────────────
    intro_kw_count = max(1, round(target_words * target_density / 100 * 0.15))
    if lang_pl:
        intro_system = (
            "Jesteś ekspertem SEO z doświadczeniem E-E-A-T. Piszesz wstępy zoptymalizowane pod AI Overview i featured snippets.\n"
            "STRUKTURA OBOWIĄZKOWA:\n"
            "1) Pierwszy akapit = DEFINICJA + BEZPOŚREDNIA odpowiedź (2-3 zdania). "
            "Zacznij od '<strong>[Keyword]</strong> to...' lub '[Keyword] polega na...'. Format AI Overview.\n"
            "2) Drugi akapit = dlaczego to ważne, kontekst praktyczny.\n"
            "3) Trzeci akapit = co czytelnik znajdzie w artykule (zapowiedź sekcji).\n"
            "Używaj tagów <p> i <strong> dla kluczowych terminów.\n"
            "BEZWZGLĘDNY ZAKAZ: NIE używaj markdown. NIE pisz ## ani # ani **tekst**. TYLKO tagi HTML <p> i <strong>."
        )
        intro_user = (
            f"Napisz wstęp do artykułu '{title}' (keyword: '{topic}').\n"
            f"Intencja wyszukiwania: {intent_analysis}\n"
            f"Sekcje artykułu: {', '.join(sections[:4])}\n"
            f"PIERWSZY AKAPIT musi zaczynać się od definicji '{topic}' — konkretna, prosta odpowiedź.\n"
            f"Użyj '{topic}' {intro_kw_count}x naturalnie.{lsi_block}\n"
            f"Tylko HTML <p> i <strong>, bez nagłówków. OK do użycia <ul>/<li> jeśli pasuje.{custom_block}"
        )
    else:
        intro_system = (
            "You are an SEO expert with E-E-A-T signals. Write intros optimized for AI Overview and featured snippets.\n"
            "MANDATORY STRUCTURE:\n"
            "1) First paragraph = DEFINITION + DIRECT answer (2-3 sentences). "
            "Start with '<strong>[Keyword]</strong> is...' format. AI Overview style.\n"
            "2) Second = why it matters, practical context.\n"
            "3) Third = what reader will find (section preview).\n"
            "Use <p> and <strong> for key terms.\n"
            "STRICT: NO markdown. Never use ## or # or **text**. ONLY HTML tags <p> and <strong>."
        )
        intro_user = (
            f"Write intro for '{title}' (keyword: '{topic}').\n"
            f"Search intent: {intent_analysis}\n"
            f"Sections: {', '.join(sections[:4])}\n"
            f"FIRST PARAGRAPH must start with a definition of '{topic}'.\n"
            f"Use '{topic}' {intro_kw_count}x naturally.{lsi_block}\n"
            f"Only HTML <p> and <strong>, no headings. OK to use <ul>/<li> if appropriate.{custom_block}"
        )
    intro_html = await _gpt(intro_system, intro_user, temperature=0.7, max_tokens=700, model=_resolved_model)
    if not intro_html.strip().startswith("<"):
        intro_html = _markdown_to_html(intro_html)
    intro_html = _strip_markdown_remnants(intro_html)
    logger.info("[Article] Intro done")

    # ── STEP 6: Sections (parallel) ───────────────────────────────────────────
    words_per_section = max(280, int(target_words * 1.2) // max(1, len(sections)))
    kw_per_section = max(1, round(words_per_section * target_density / 100))
    # Pick 3-4 LSI terms per section (rotate through the list)
    lsi_per_section = lsi_terms[:15] if lsi_terms else []

    if lang_pl:
        section_system = (
            "Jesteś ekspertem SEO i merytorycznym autorem. Piszesz sekcje artykułu w HTML.\n"
            "WYMAGANIA:\n"
            "- Zacznij od <h2>, dodaj 1-2 <h3> pod-sekcje\n"
            "- Używaj <p>, <ul>/<li> lub <ol>/<li> tam gdzie pasuje\n"
            "- Dodaj <strong> dla najważniejszych terminów i faktów\n"
            "- Pisz konkretnie — dane, liczby, przykłady, porady praktyczne\n"
            "- DOŚWIADCZENIE E-E-A-T: Wpleć 1-2 zdania z perspektywy praktycznej "
            "('W praktyce często spotykamy...', 'Typowy błąd to...', 'Z doświadczenia wynika...'). "
            "NIE pisz 'ja' — pisz w tonie eksperta który widział setki takich przypadków.\n"
            "- Bez powtarzania wstępu ani zakończenia artykułu\n"
            "- ENCJE: Używaj konkretnych nazw własnych (marki, firmy, produkty, osoby, miejsca, normy, "
            "instytucje) zamiast ogólników. Google NLP rozpoznaje encje — im więcej trafnych nazw "
            "własnych powiązanych z tematem, tym lepszy topical authority.\n"
            "BEZWZGLĘDNY ZAKAZ: NIE używaj markdown. NIE pisz ## ani ### ani # na początku linii. "
            "NIE używaj **tekst** ani *tekst*. TYLKO czysty HTML — tagi <h2>, <h3>, <p>, <ul>, <li>, <strong>, <em>."
        )
    else:
        section_system = (
            "You are an SEO expert and subject matter author. Write article sections in HTML.\n"
            "REQUIREMENTS:\n"
            "- Start with <h2>, add 1-2 <h3> subsections\n"
            "- Use <p>, <ul>/<li> or <ol>/<li> where appropriate\n"
            "- Add <strong> for key terms and important facts\n"
            "- Be specific — data, numbers, examples, practical tips\n"
            "- E-E-A-T EXPERIENCE: Weave in 1-2 sentences from a practical perspective "
            "('In practice, we often see...', 'A common mistake is...', 'Experience shows...'). "
            "DON'T use first person 'I' — write as an expert who has seen hundreds of such cases.\n"
            "- Don't repeat intro or conclusion\n"
            "- ENTITIES: Use specific proper nouns (brands, companies, products, people, places, standards, "
            "institutions) instead of generic terms. Google NLP recognizes entities — more relevant "
            "proper nouns related to the topic means better topical authority.\n"
            "STRICT: NO markdown. Never use ## or ### or # at line start. "
            "Never use **text** or *text*. ONLY pure HTML tags: <h2>, <h3>, <p>, <ul>, <li>, <strong>, <em>."
        )

    # Semaphore: max 4 concurrent GPT calls for sections (avoid rate limit)
    _sem = asyncio.Semaphore(4)

    async def _generate_section(i: int, heading: str) -> str:
        async with _sem:
            # Rotate LSI terms so each section gets different ones
            section_lsi = lsi_per_section[i * 3 % max(1, len(lsi_per_section)):(i * 3 % max(1, len(lsi_per_section))) + 5] if lsi_per_section else []
            lsi_section_block = f"\nSłowa LSI do wplecenia: {', '.join(section_lsi)}" if section_lsi else ""
            if lang_pl:
                section_user = (
                    f"Napisz sekcję dla artykułu '{title}' (keyword: '{topic}').\n"
                    f"H2: '{heading}'\n"
                    f"Intencja: {intent_analysis}\n"
                    f"Cel: ~{words_per_section} słów, użyj '{topic}' ~{kw_per_section}x{lsi_section_block}\n"
                    f"Struktura: <h2>{heading}</h2> → 1-2 <h3> podsekcje → <p> akapity + listy/tabele gdzie sens\n"
                    f"Pisz ekspercko: konkretne fakty, liczby, przykłady. Unikaj ogólników.{custom_block}"
                )
            else:
                section_user = (
                    f"Write section for '{title}' (keyword: '{topic}').\n"
                    f"H2: '{heading}'\n"
                    f"Intent: {intent_analysis}\n"
                    f"Target: ~{words_per_section} words, use '{topic}' ~{kw_per_section}x{lsi_section_block}\n"
                    f"Structure: <h2>{heading}</h2> → 1-2 <h3> subsections → <p> + lists/tables where relevant\n"
                    f"Write expertly: specific facts, numbers, examples. Avoid vague generalities.{custom_block}"
                )
            sec_html = await _gpt(section_system, section_user, temperature=0.7, max_tokens=1100, model=_resolved_model)
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
        "TYLKO tagi HTML: <h2>, <p>, <ul>, <li>, <strong>."
    ) if lang_pl else (
        "You are an SEO expert. Write article conclusion in HTML.\n"
        "STRICT: NO markdown. Never use ## or ### or # or **text**. "
        "ONLY HTML tags: <h2>, <p>, <ul>, <li>, <strong>."
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
            f"- Odpowiedzi: 2-4 zdania, konkretne, bez lania wody\n"
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
        "Jesteś ekspertem SEO. Tworzysz FAQ zoptymalizowane pod featured snippets, AI Overview i PAA (People Also Ask)." if lang_pl
        else "You are an SEO expert creating FAQ optimized for featured snippets, AI Overview, and PAA (People Also Ask)."
    )
    if lang_pl:
        excerpt_user = (
            f"Napisz meta description dla artykułu '{title}' o '{topic}'.\n"
            f"WYMAGANIA: max 155 znaków, bez HTML, zawiera '{topic}', kończy się CTA (np. 'Sprawdź', 'Dowiedz się', 'Przeczytaj').\n"
            f"Tylko tekst meta description, nic więcej."
        )
    else:
        excerpt_user = (
            f"Write meta description for '{title}' about '{topic}'.\n"
            f"REQUIREMENTS: max 155 chars, no HTML, includes '{topic}', ends with CTA (e.g. 'Learn more', 'Find out', 'Read now').\n"
            f"Only the meta description text, nothing else."
        )

    # Launch all 3 in parallel
    conclusion_raw, faq_raw, excerpt_raw = await asyncio.gather(
        _gpt(_concl_system, conclusion_user, temperature=0.7, max_tokens=600, model=_resolved_model),
        _gpt(_faq_system, faq_user, temperature=0.6, max_tokens=1400, model=_resolved_model),
        _gpt(
            "Jesteś SEO copywriterem." if lang_pl else "You are an SEO copywriter.",
            excerpt_user, temperature=0.5, max_tokens=80, model=_resolved_model
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

    # Post-process excerpt
    excerpt = excerpt_raw.strip('"\'').strip()
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
        # "Krótka odpowiedź" box — good for featured snippets (25%)
        sa_label = "Krótka odpowiedź" if lang_pl else "Quick Answer"
        sa_intro = intro_html[:400] if intro_html else ""
        sa_text = re.sub(r'<[^>]+>', '', sa_intro).strip()[:250]
        sa_box = (
            f'<div style="background:#f0fdf4;border:1px solid #86efac;padding:16px 20px;'
            f'margin:16px 0 24px;border-radius:8px;">'
            f'<strong>✅ {sa_label}:</strong> {sa_text}</div>'
        ) if sa_text else ""
        content_parts = [sa_box, intro_html] + sections_html + [conclusion_html, faq_html]
    else:
        # Standard layout
        content_parts = [intro_html] + sections_html + [conclusion_html, faq_html]

    content = "\n\n".join(p for p in content_parts if p)

    # Inject external anchor links
    content = _inject_anchors(content, anchors_info, language=language)

    # Inject internal links to already-published posts on this domain
    if published_posts:
        content = _inject_internal_links(content, published_posts, topic, language=language)

    # Final strip of any remaining ## markdown before enrichment
    content = _strip_markdown_remnants(content)

    # Enrich with random unique elements (3-4 per article)
    content = await enrich_article(content, topic, sections, lang_pl=(language == "pl"), openai_client=client, serp_urls=serp_urls)

    # Final strip again after enrichment (GPT content in enrichments may also have ##)
    content = _strip_markdown_remnants(content)

    # Fix heading hierarchy (H3 before H2, skipped levels, etc.)
    content = _fix_heading_hierarchy(content)

    # Deduplicate anchor links
    seen_hrefs: set = set()

    def _dedup_link(m: re.Match) -> str:
        href = re.search(r'href=["\']([^"\']+)["\']', m.group(0))
        if not href:
            return m.group(0)
        url = href.group(1).rstrip("/")
        if url in seen_hrefs:
            return re.sub(r"<[^>]+>", "", m.group(0))
        seen_hrefs.add(url)
        return m.group(0)

    content = re.sub(r'<a\s[^>]*?>.*?</a>', _dedup_link, content, flags=re.DOTALL | re.IGNORECASE)

    # FAQPage JSON-LD — Yoast/RankMath only auto-generate FAQ schema from their own
    # block types, not from raw <h3>/<p> HTML. Inject it explicitly for rich snippets.
    faq_pairs = re.findall(r'<h3[^>]*>(.*?)</h3>\s*<p>(.*?)</p>', content, re.DOTALL | re.IGNORECASE)
    if faq_pairs and len(faq_pairs) >= 3:
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
        faq_schema = f'<script type="application/ld+json">{_json.dumps(faq_ld, ensure_ascii=False)}</script>'
        content = faq_schema + "\n" + content

    # Dedup fingerprint
    fingerprint = _content_fingerprint(content)
    if domain_fingerprints is not None and fingerprint in domain_fingerprints:
        logger.warning(f"[Article] Duplicate fingerprint detected for '{topic}' — variation hint applied")
    if domain_fingerprints is not None:
        domain_fingerprints.add(fingerprint)

    word_count = _count_words(content)
    _elapsed = round(time.time() - _t0, 1)
    logger.info(f"[Article] Done — '{title}' | {len(sections)} sekcji | {word_count} słów | fp={fingerprint[:8]} | {_elapsed}s")

    # A8: Minimum article length validation
    if word_count < 600:
        logger.warning(f"[Article] Short article ({word_count} words) for '{topic}' — may indicate GPT truncation")

    return {
        "title": title,
        "content": content,
        "excerpt": excerpt,
        "fingerprint": fingerprint,
        "lsi_tags": lsi_terms[:5],  # top 5 LSI terms for WP tags
        "word_count": word_count,
    }


async def describe_image_and_generate(image_b64: str, topic: str) -> str:
    _model = await get_gpt_model()
    vision_response = await client.chat.completions.create(
        model=_model,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": f"Opisz krótko co widać na tym screenshocie (max 2 zdania). Kontekst: '{topic}'."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ]}],
        max_tokens=200,
    )
    description = vision_response.choices[0].message.content
    return await generate_image(f"Professional illustration: {description}. Clean, modern design.")


async def generate_image(prompt: str) -> str:
    response = await client.images.generate(
        model="dall-e-3", prompt=prompt, n=1,
        size="1792x1024", response_format="b64_json",
    )
    return response.data[0].b64_json

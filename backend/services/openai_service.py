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
import re
import time
from typing import Optional

import aiosqlite
from openai import AsyncOpenAI

from config import OPENAI_API_KEY, DB_PATH
from services.content_enrichments import enrich_article

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
logger = logging.getLogger(__name__)

_SERP_CACHE_TTL = 86400  # 24 hours — stored in SQLite serp_cache table


async def _serp_cache_get(key: str) -> Optional[dict]:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT data_json FROM serp_cache WHERE cache_key=? AND expires_at > ?",
                (key, time.time())
            ) as cur:
                row = await cur.fetchone()
        if row:
            return _json.loads(row[0])
    except Exception:
        pass
    return None


async def _serp_cache_set(key: str, data: dict) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS serp_cache (cache_key TEXT PRIMARY KEY, data_json TEXT, expires_at REAL)"
            )
            await db.execute(
                "INSERT OR REPLACE INTO serp_cache (cache_key, data_json, expires_at) VALUES (?,?,?)",
                (key, _json.dumps(data, ensure_ascii=False), time.time() + _SERP_CACHE_TTL)
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"[SERP] Cache write failed: {e}")

_BLOG_SKIP = re.compile(
    r"(youtube\.com|facebook\.com|twitter\.com|instagram\.com|tiktok\.com"
    r"|wikipedia\.org|reddit\.com|pinterest\.com|allegro\.pl|amazon\.|ebay\."
    r"|olx\.pl|ceneo\.pl|linkedin\.com|quora\.com)",
    re.IGNORECASE,
)


def _is_blog_url(url: str) -> bool:
    return not _BLOG_SKIP.search(url)


def _count_words(text: str) -> int:
    return len(re.findall(r'\w+', text))


def _keyword_density(text: str, keyword: str) -> float:
    words = _count_words(text)
    if not words:
        return 0.0
    count = len(re.findall(re.escape(keyword.lower()), text.lower()))
    return round((count * len(keyword.split())) / words * 100, 2)


def _extract_lsi(text: str, keyword: str, top_n: int = 20) -> list[str]:
    """Extract most frequent non-stopword terms from text excluding seed keyword words."""
    STOPWORDS = {
        "i", "w", "z", "na", "do", "po", "o", "a", "się", "nie", "jak", "co",
        "czy", "że", "to", "jest", "są", "dla", "przez", "przy", "za", "od",
        "ile", "ten", "ta", "te", "tego", "tej", "być", "mieć", "też", "już",
        "the", "is", "in", "of", "and", "to", "a", "for", "with", "that", "this",
        "it", "are", "was", "be", "as", "at", "by", "an", "or", "but", "not",
        "have", "from", "on", "your", "can", "we", "our", "you", "they", "their",
    }
    kw_words = set(keyword.lower().split())
    words = re.findall(r'\b[a-ząćęłńóśźżA-ZĄĆĘŁŃÓŚŹŻ]{4,}\b', text.lower())
    freq: dict[str, int] = {}
    for w in words:
        if w not in STOPWORDS and w not in kw_words:
            freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:top_n]]


def _content_fingerprint(content: str) -> str:
    """Simple fingerprint of first 200 words for dedup check."""
    words = re.findall(r'\w+', content.lower())[:200]
    return hashlib.md5(" ".join(words).encode()).hexdigest()


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
        from services.dataforseo_service import DataForSEOClient
        dfs = DataForSEOClient(dfs_login, dfs_password)

        serp_raw = await dfs.serp_top10_full(topic, location_code, language_code)
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
        for url in blog_urls:
            content = await dfs.page_content(url)
            if content:
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


def _markdown_to_html(text: str) -> str:
    """Convert markdown to HTML. Safe to call on mixed markdown+HTML content."""
    # Convert markdown tables to HTML
    def _convert_table(m: re.Match) -> str:
        rows = [r.strip() for r in m.group(0).strip().split("\n") if r.strip()]
        html = "<table>\n"
        for i, row in enumerate(rows):
            if re.match(r"^[\s|:-]+$", row):
                continue  # skip separator row
            cells = [c.strip() for c in row.strip("|").split("|")]
            tag = "th" if i == 0 else "td"
            html += "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>\n"
        html += "</table>"
        return html

    text = re.sub(r"(\|.+\|\n)+", _convert_table, text)
    text = re.sub(r"^#### (.+)$", r"<h4>\1</h4>", text, flags=re.MULTILINE)
    text = re.sub(r"^### (.+)$", r"<h3>\1</h3>", text, flags=re.MULTILINE)
    text = re.sub(r"^## (.+)$", r"<h2>\1</h2>", text, flags=re.MULTILINE)
    text = re.sub(r"^# (.+)$", r"<h1>\1</h1>", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"(?m)^[-*] (.+)$", r"<li>\1</li>", text)
    text = re.sub(r"(<li>.*?</li>)+", lambda m: f"<ul>{m.group(0)}</ul>", text, flags=re.DOTALL)
    text = re.sub(r"(?m)^\d+\. (.+)$", r"<li>\1</li>", text)
    lines = text.split("\n")
    result = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        result.append(line if line.startswith("<") else f"<p>{line}</p>")
    return "\n".join(result)


def _strip_markdown_remnants(html: str) -> str:
    """
    Remove leftover markdown syntax from HTML content that GPT mixed in.
    Called on every GPT output AFTER markdown_to_html to catch mixed content.
    e.g. GPT returns <h2>Title</h2> but inside <p> writes ## SubHeading or **bold**
    """
    # Convert any remaining ## / # headers that GPT slipped inside HTML blocks
    html = re.sub(r"(?m)^#### (.+)$", r"<h4>\1</h4>", html)
    html = re.sub(r"(?m)^### (.+)$", r"<h3>\1</h3>", html)
    html = re.sub(r"(?m)^## (.+)$", r"<h2>\1</h2>", html)
    html = re.sub(r"(?m)^# (.+)$", r"<h1>\1</h1>", html)
    # Bold/italic inside HTML tags
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"\*(.+?)\*", r"<em>\1</em>", html)
    # Backtick code
    html = re.sub(r"`(.+?)`", r"<code>\1</code>", html)
    return html


async def _gpt(system: str, user: str, temperature: float = 0.7, max_tokens: int = 2000, model: str = "gpt-4o-mini") -> str:
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


def _build_faq_schema(faq_html: str, topic: str) -> str:
    """Extract Q&A pairs from FAQ HTML and build FAQPage JSON-LD schema.
    Handles both <h3>Q</h3><p>A</p> and <dt>Q</dt><dd>A</dd> patterns.
    Falls back to extracting all h3+next-sibling content."""
    import json
    entities = []

    # Primary: h3 followed by p (most common GPT output)
    questions = re.findall(r'<h3[^>]*>(.*?)</h3>\s*<p>(.*?)</p>', faq_html, re.DOTALL | re.IGNORECASE)
    for q, a in questions[:8]:
        q_clean = re.sub(r'<[^>]+>', '', q).strip()
        a_clean = re.sub(r'<[^>]+>', '', a).strip()
        if q_clean and a_clean and len(a_clean) > 20:
            entities.append({"@type": "Question", "name": q_clean,
                             "acceptedAnswer": {"@type": "Answer", "text": a_clean}})

    # Fallback: h3 followed by any block element
    if not entities:
        qs = re.findall(r'<h3[^>]*>(.*?)</h3>(.*?)(?=<h3|</div>|$)', faq_html, re.DOTALL | re.IGNORECASE)
        for q, a_block in qs[:8]:
            q_clean = re.sub(r'<[^>]+>', '', q).strip()
            a_clean = re.sub(r'<[^>]+>', ' ', a_block).strip()
            a_clean = re.sub(r'\s+', ' ', a_clean).strip()
            if q_clean and a_clean and len(a_clean) > 20:
                entities.append({"@type": "Question", "name": q_clean,
                                 "acceptedAnswer": {"@type": "Answer", "text": a_clean[:500]}})

    if not entities:
        return ""
    schema = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": entities}
    return f'<script type="application/ld+json">\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n</script>'


def _build_article_schema(title: str, topic: str, excerpt: str, word_count: int = 0, domain: str = "") -> str:
    """Build Article JSON-LD schema with E-E-A-T signals."""
    import json
    from datetime import datetime
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    # Derive publisher name from domain
    publisher_name = "Redakcja"
    if domain:
        clean = domain.replace("https://", "").replace("http://", "").rstrip("/").split("/")[0]
        publisher_name = clean.replace("www.", "").split(".")[0].capitalize()
    schema = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title[:110],  # Google truncates at 110 chars
        "description": excerpt[:300] if excerpt else "",
        "keywords": topic,
        "datePublished": now,
        "dateModified": now,
        "inLanguage": "pl",
        "author": {
            "@type": "Organization",
            "name": publisher_name,
        },
        "publisher": {
            "@type": "Organization",
            "name": publisher_name,
        },
        "mainEntityOfPage": {
            "@type": "WebPage",
            "@id": f"https://{domain.replace('https://','').replace('http://','').rstrip('/')}/" if domain else "",
        },
    }
    if word_count:
        schema["wordCount"] = word_count
    return f'<script type="application/ld+json">\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n</script>'


def _inject_anchors(html: str, anchors_info: str) -> str:
    """
    Inject client links into article paragraphs contextually.
    - First link: injected in paragraph 3-5 (after intro, inside content)
    - Additional links: spread across later paragraphs
    - Never injected in first 2 or last 2 paragraphs
    - Surrounding context varies to avoid footprint
    """
    if not anchors_info:
        return html
    links = re.findall(r'<a\s[^>]*>.*?</a>', anchors_info, re.DOTALL | re.IGNORECASE)
    seen_hrefs: set = set()
    paragraphs = re.findall(r'<p>.*?</p>', html, re.DOTALL)
    para_count = len(paragraphs)

    # Vary the surrounding context — anti-footprint
    _LINK_CONTEXTS = [
        lambda lnk: f" Więcej na ten temat znajdziesz na stronie {lnk}.",
        lambda lnk: f" Szczegółowe informacje dostępne są pod adresem {lnk}.",
        lambda lnk: f" Warto odwiedzić serwis {lnk}, gdzie znajdziesz więcej materiałów.",
        lambda lnk: f" Dodatkowe zasoby: {lnk}.",
        lambda lnk: f" Polecamy również stronę {lnk}.",
    ]
    import random as _r

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
            ctx = _r.choice(_LINK_CONTEXTS)
            new_para = para[:-4] + ctx(link) + "</p>"
            html = html.replace(para, new_para, 1)
    return html


def _inject_internal_links(html: str, published_posts: list[dict], topic: str) -> str:
    """
    Inject 2-3 internal links to already-published posts on the same domain.
    Uses keyword/topic overlap (not just title words) for better matching.
    Adds title attribute for accessibility and SEO.
    """
    if not published_posts:
        return html

    paragraphs = re.findall(r'<p>.*?</p>', html, re.DOTALL)
    if len(paragraphs) < 4:
        return html

    # Current article topic words for avoiding self-similar links
    topic_words = set(re.findall(r'\w{4,}', topic.lower()))

    injected = 0
    used_urls: set = set()

    # Vary surrounding context for internal links
    _INT_CONTEXTS = [
        lambda lnk, title: f" Przeczytaj też: {lnk}.",
        lambda lnk, title: f" Polecamy powiązany artykuł: {lnk}.",
        lambda lnk, title: f" Więcej o tym w artykule: {lnk}.",
        lambda lnk, title: f" Powiązane informacje: {lnk}.",
    ]
    import random as _r

    for post in published_posts[:8]:
        if injected >= 3:
            break
        url = post.get("url") or post.get("wp_post_url", "")
        title = post.get("title", "") or post.get("keyword", "")
        if not url or not title or url in used_urls:
            continue

        # Match on keyword/title words
        title_words = set(re.findall(r'\w{4,}', title.lower()))
        if not title_words:
            continue

        best_para = None
        best_score = 0
        for para in paragraphs[2:-2]:
            para_text = re.sub(r'<[^>]+>', '', para).lower()
            if 'href=' in para:
                continue
            para_words = set(re.findall(r'\w{4,}', para_text))
            # Score = overlap with title words, bonus for topic words in paragraph
            overlap = len(title_words & para_words)
            topic_bonus = 1 if len(topic_words & para_words) > 2 else 0
            score = overlap + topic_bonus
            if score > best_score:
                best_score = score
                best_para = para

        if best_para and best_score >= 1:
            # Use title as anchor (4-6 words), with title attribute
            anchor_words = title.split()[:6]
            anchor_text = " ".join(anchor_words)
            link = f'<a href="{url}" title="{title}">{anchor_text}</a>'
            ctx = _r.choice(_INT_CONTEXTS)
            new_para = best_para[:-4] + ctx(link, title) + "</p>"
            html = html.replace(best_para, new_para, 1)
            used_urls.add(url)
            injected += 1

    if injected:
        logger.info(f"[Article] Injected {injected} internal links")
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
    import random as _rand
    import os
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
        return _rand.choice(generics)


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
) -> dict:
    def clean_url(url: str) -> str:
        url = url.strip()
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url
        return url

    # Rotate anchor text for natural link profile
    rotated_anchor = _rotate_anchor(anchor_text, client_domain, language)
    anchors_info = f'<a href="{clean_url(client_domain)}">{rotated_anchor}</a>'
    if anchor_text2 and anchor_url2:
        anchors_info += f', <a href="{clean_url(anchor_url2)}">{anchor_text2}</a>'
    if anchor_text3 and anchor_url3:
        anchors_info += f', <a href="{clean_url(anchor_url3)}">{anchor_text3}</a>'

    import random as _random
    variation = f" Kąt tematyczny: {variation_hint}." if variation_hint else ""
    lang_pl = language == "pl"

    # ── Layout variant (30% faq_top, 20% tldr, 25% short_answer, 25% standard) ──
    if layout_variant is None:
        _rv = _random.random()
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
            f"Podaj krótko (max 3 zdania):\n"
            f"1. Główna intencja wyszukiwania\n"
            f"2. Cluster tematyczny\n"
            f"3. Kluczowe encje i tematy pokrewne{serp_block}"
        )
    else:
        intent_user = (
            f"For keyword: '{topic}'{variation}\n"
            f"Briefly (max 3 sentences):\n"
            f"1. Search intent\n2. Topic cluster\n3. Key entities{serp_block}"
        )
    intent_analysis = await _gpt(
        "Jesteś ekspertem SEO." if lang_pl else "You are an SEO expert.",
        intent_user, temperature=0.3, max_tokens=400
    )
    logger.info(f"[Article] Intent: {intent_analysis[:80]}")

    # ── STEP 3: Outline ───────────────────────────────────────────────────────
    n_sections = max(4, min(8, round(target_words / 200)))
    if lang_pl:
        outline_user = (
            f"Stwórz outline artykułu SEO dla frazy: '{topic}'\n"
            f"Intencja i encje: {intent_analysis}\n"
            f"DOKŁADNIE {n_sections} sekcji H2, każda oddzielona '<<<<', "
            f"bez wstępu i zakończenia.\nTylko nagłówki H2, bez tekstu.{serp_block}"
        )
    else:
        outline_user = (
            f"Create SEO article outline for: '{topic}'\n"
            f"Intent: {intent_analysis}\n"
            f"EXACTLY {n_sections} H2 sections separated by '<<<<', "
            f"no intro/conclusion.\nOnly H2 headings.{serp_block}"
        )
    outline_raw = await _gpt(
        "Jesteś ekspertem SEO tworzącym struktury artykułów. Nagłówki H2 oddzielone '<<<<'." if lang_pl
        else "You are an SEO expert. H2 headings separated by '<<<<'.",
        outline_user, temperature=0.5, max_tokens=500
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
            f"- '[Keyword] — kompletny przewodnik [rok]'\n"
            f"- 'Jak [działanie związane z keyword]? [X] kroków'\n"
            f"- 'Co to jest [keyword] i jak [korzyść]?'\n"
            f"- '[X] najważniejszych faktów o [keyword]'\n"
            f"- '[Keyword]: wszystko co musisz wiedzieć'\n"
            f"Tylko tytuł, bez cudzysłowów, bez markdown."
        )
    else:
        title_user = (
            f"Create unique SEO title for: '{topic}'\n"
            f"Article sections: {', '.join(sections[:3])}\n"
            f"RULES: 50-60 characters, contains '{topic}', attention-grabbing.\n"
            f"Use one of these formats:\n"
            f"- '[Keyword] — Complete Guide [year]'\n"
            f"- 'How to [action related to keyword]? [X] Steps'\n"
            f"- 'What is [keyword] and how does it [benefit]?'\n"
            f"- '[X] Key Facts About [keyword]'\n"
            f"- '[Keyword]: Everything You Need to Know'\n"
            f"Only the title, no quotes, no markdown."
        )
    title = await _gpt(
        "Jesteś copywriterem SEO." if lang_pl else "You are an SEO copywriter.",
        title_user, temperature=0.8, max_tokens=100
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
            "Używaj tagów <p> i <strong> dla kluczowych terminów."
        )
        intro_user = (
            f"Napisz wstęp do artykułu '{title}' (keyword: '{topic}').\n"
            f"Intencja wyszukiwania: {intent_analysis}\n"
            f"Sekcje artykułu: {', '.join(sections[:4])}\n"
            f"PIERWSZY AKAPIT musi zaczynać się od definicji '{topic}' — konkretna, prosta odpowiedź.\n"
            f"Użyj '{topic}' {intro_kw_count}x naturalnie.{lsi_block}\n"
            f"Tylko HTML <p> i <strong>, bez nagłówków. OK do użycia <ul>/<li> jeśli pasuje."
        )
    else:
        intro_system = (
            "You are an SEO expert with E-E-A-T signals. Write intros optimized for AI Overview and featured snippets.\n"
            "MANDATORY STRUCTURE:\n"
            "1) First paragraph = DEFINITION + DIRECT answer (2-3 sentences). "
            "Start with '<strong>[Keyword]</strong> is...' format. AI Overview style.\n"
            "2) Second = why it matters, practical context.\n"
            "3) Third = what reader will find (section preview).\n"
            "Use <p> and <strong> for key terms."
        )
        intro_user = (
            f"Write intro for '{title}' (keyword: '{topic}').\n"
            f"Search intent: {intent_analysis}\n"
            f"Sections: {', '.join(sections[:4])}\n"
            f"FIRST PARAGRAPH must start with a definition of '{topic}'.\n"
            f"Use '{topic}' {intro_kw_count}x naturally.{lsi_block}\n"
            f"Only HTML <p> and <strong>, no headings. OK to use <ul>/<li> if appropriate."
        )
    intro_html = await _gpt(intro_system, intro_user, temperature=0.7, max_tokens=700)
    if not intro_html.strip().startswith("<"):
        intro_html = _markdown_to_html(intro_html)
    intro_html = _strip_markdown_remnants(intro_html)
    logger.info("[Article] Intro done")

    # ── STEP 6: Sections (parallel) ───────────────────────────────────────────
    words_per_section = max(180, target_words // max(1, len(sections)))
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
            "- Bez powtarzania wstępu ani zakończenia artykułu"
        )
    else:
        section_system = (
            "You are an SEO expert and subject matter author. Write article sections in HTML.\n"
            "REQUIREMENTS:\n"
            "- Start with <h2>, add 1-2 <h3> subsections\n"
            "- Use <p>, <ul>/<li> or <ol>/<li> where appropriate\n"
            "- Add <strong> for key terms and important facts\n"
            "- Be specific — data, numbers, examples, practical tips\n"
            "- Don't repeat intro or conclusion"
        )

    async def _generate_section(i: int, heading: str) -> str:
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
                f"Pisz ekspercko: konkretne fakty, liczby, przykłady. Unikaj ogólników."
            )
        else:
            section_user = (
                f"Write section for '{title}' (keyword: '{topic}').\n"
                f"H2: '{heading}'\n"
                f"Intent: {intent_analysis}\n"
                f"Target: ~{words_per_section} words, use '{topic}' ~{kw_per_section}x{lsi_section_block}\n"
                f"Structure: <h2>{heading}</h2> → 1-2 <h3> subsections → <p> + lists/tables where relevant\n"
                f"Write expertly: specific facts, numbers, examples. Avoid vague generalities."
            )
        sec_html = await _gpt(section_system, section_user, temperature=0.7, max_tokens=1100)
        if not sec_html.strip().startswith("<"):
            sec_html = _markdown_to_html(sec_html)
        sec_html = _strip_markdown_remnants(sec_html)
        logger.info(f"[Article] Section {i+1}/{len(sections)}: {heading[:40]}")
        return sec_html

    # Generate all sections in parallel
    sections_html = list(await asyncio.gather(*[
        _generate_section(i, heading) for i, heading in enumerate(sections)
    ]))

    # ── STEP 7: Conclusion ────────────────────────────────────────────────────
    if lang_pl:
        conclusion_user = (
            f"Napisz zakończenie artykułu '{title}' (keyword: '{topic}').\n"
            f"Omówione tematy: {', '.join(sections[:5])}\n"
            f"STRUKTURA:\n"
            f"<h2>Podsumowanie</h2>\n"
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
            f"<h2>Summary</h2>\n"
            f"- Para 1: key takeaways (bullets in <ul> or prose)\n"
            f"- Para 2: practical next steps\n"
            f"- Para 3 (optional): CTA or question for readers\n"
            f"Use '{topic}' 1-2x. Specific conclusions, not generalities."
        )
    conclusion_html = await _gpt(
        "Jesteś ekspertem SEO." if lang_pl else "You are an SEO expert.",
        conclusion_user, temperature=0.7, max_tokens=600
    )
    if not conclusion_html.strip().startswith("<"):
        conclusion_html = _markdown_to_html(conclusion_html)
    conclusion_html = _strip_markdown_remnants(conclusion_html)

    # ── STEP 8: FAQ ───────────────────────────────────────────────────────────
    paa_block = ""
    if paa_questions:
        paa_block = "\nPytania z Google PAA (użyj tych jako baza):\n" + "\n".join(f"- {q}" for q in paa_questions[:6])
    paa_block_en = ""
    if paa_questions:
        paa_block_en = "\nReal Google PAA questions (use as base):\n" + "\n".join(f"- {q}" for q in paa_questions[:6])

    if lang_pl:
        faq_user = (
            f"Stwórz sekcję FAQ dla artykułu o '{topic}'.\n"
            f"WYMAGANIA:\n"
            f"- 8 pytań i odpowiedzi\n"
            f"- Pierwsze pytanie = definicja/wyjaśnienie '{topic}'\n"
            f"- Odpowiedzi: 2-4 zdania, konkretne, bez lania wody\n"
            f"- Mix: pytania informacyjne + praktyczne + porównawcze{paa_block}\n"
            f"HTML: <h2>Najczęściej zadawane pytania (FAQ)</h2>\n"
            f"Format każdej pary: <h3>Pytanie?</h3><p>Odpowiedź.</p>"
        )
    else:
        faq_user = (
            f"Create FAQ section for article about '{topic}'.\n"
            f"REQUIREMENTS:\n"
            f"- 8 questions and answers\n"
            f"- First question = definition/explanation of '{topic}'\n"
            f"- Answers: 2-4 sentences, specific, no filler\n"
            f"- Mix: informational + practical + comparative questions{paa_block_en}\n"
            f"HTML: <h2>Frequently Asked Questions (FAQ)</h2>\n"
            f"Each pair: <h3>Question?</h3><p>Answer.</p>"
        )
    faq_html = await _gpt(
        "Jesteś ekspertem SEO. Tworzysz FAQ zoptymalizowane pod featured snippets, AI Overview i PAA (People Also Ask)." if lang_pl
        else "You are an SEO expert creating FAQ optimized for featured snippets, AI Overview, and PAA (People Also Ask).",
        faq_user, temperature=0.6, max_tokens=1400
    )
    if not faq_html.strip().startswith("<"):
        faq_html = _markdown_to_html(faq_html)
    faq_html = _strip_markdown_remnants(faq_html)

    # ── STEP 9: Excerpt ───────────────────────────────────────────────────────
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
    excerpt = await _gpt(
        "Jesteś SEO copywriterem." if lang_pl else "You are an SEO copywriter.",
        excerpt_user, temperature=0.5, max_tokens=80
    )
    excerpt = excerpt.strip('"\'').strip()
    logger.info("[Article] Excerpt done")

    # ── STEP 10: Assemble — apply layout variant ──────────────────────────────
    h1 = f"<h1>{title}</h1>"

    if layout_variant == "faq_top":
        # FAQ at the very top after H1 (30% of articles)
        content_parts = [h1, faq_html, intro_html] + sections_html + [conclusion_html]
    elif layout_variant == "tldr":
        # TL;DR box right after H1 (20%)
        tldr_label = "TL;DR" if not lang_pl else "W skrócie"
        tldr_sentence = excerpt[:200] if excerpt else ""
        tldr_box = (
            f'<div style="background:#e8f0fe;border-left:4px solid #1a73e8;padding:12px 18px;'
            f'margin:16px 0 24px;border-radius:0 8px 8px 0;">'
            f'<strong>{tldr_label}:</strong> {tldr_sentence}</div>'
        ) if tldr_sentence else ""
        content_parts = [h1, tldr_box, intro_html] + sections_html + [conclusion_html, faq_html]
    elif layout_variant == "short_answer":
        # "Krótka odpowiedź" box after H1 — good for featured snippets (25%)
        sa_label = "Krótka odpowiedź" if lang_pl else "Quick Answer"
        sa_intro = intro_html[:400] if intro_html else ""
        # Strip HTML tags for plain text in box
        import re as _re
        sa_text = _re.sub(r'<[^>]+>', '', sa_intro).strip()[:250]
        sa_box = (
            f'<div style="background:#f0fdf4;border:1px solid #86efac;padding:16px 20px;'
            f'margin:16px 0 24px;border-radius:8px;">'
            f'<strong>✅ {sa_label}:</strong> {sa_text}</div>'
        ) if sa_text else ""
        content_parts = [h1, sa_box, intro_html] + sections_html + [conclusion_html, faq_html]
    else:
        # Standard layout
        content_parts = [h1, intro_html] + sections_html + [conclusion_html, faq_html]

    content = "\n\n".join(p for p in content_parts if p)

    # Inject external anchor links
    content = _inject_anchors(content, anchors_info)

    # Inject internal links to already-published posts on this domain
    if published_posts:
        content = _inject_internal_links(content, published_posts, topic)

    # Enrich with random unique elements (2-3 per article)
    content = await enrich_article(content, topic, sections, lang_pl=(language == "pl"), openai_client=client, serp_urls=serp_urls)

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

    # Schema markup: FAQPage + Article
    faq_schema = _build_faq_schema(faq_html, topic)
    article_schema = _build_article_schema(title, topic, excerpt, word_count=_count_words(content), domain=client_domain)
    if faq_schema:
        content += f"\n\n{faq_schema}"
    if article_schema:
        content += f"\n\n{article_schema}"

    # Dedup fingerprint
    fingerprint = _content_fingerprint(content)
    if domain_fingerprints is not None and fingerprint in domain_fingerprints:
        logger.warning(f"[Article] Duplicate fingerprint detected for '{topic}' — variation hint applied")
    if domain_fingerprints is not None:
        domain_fingerprints.add(fingerprint)

    logger.info(f"[Article] Done — '{title}' | {len(sections)} sekcji | {_count_words(content)} słów | fp={fingerprint[:8]}")

    return {
        "title": title,
        "content": content,
        "excerpt": excerpt,
        "fingerprint": fingerprint,
        "lsi_tags": lsi_terms[:5],  # top 5 LSI terms for WP tags
    }


async def describe_image_and_generate(image_b64: str, topic: str) -> str:
    vision_response = await client.chat.completions.create(
        model="gpt-4o-mini",
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
        size="1024x1024", response_format="b64_json",
    )
    return response.data[0].b64_json

"""
SEO Content Writer Service.
Generates full articles based on SERP top10 analysis.
Optimized for AI Overview (answer in H1), internal linking, tone of voice.
"""
import asyncio
import json
import logging
import random
import re
from services.dataforseo_service import DataForSEOClient
from services.content_enrichments import enrich_article
# Reuse SERP cache from openai_service to avoid duplicate DataForSEO calls
from services.article_helpers import serp_cache_get as _serp_cache_get, serp_cache_set as _serp_cache_set
from services.openai_service import get_gpt_model, get_openai_client, _fix_heading_hierarchy

logger = logging.getLogger(__name__)


_SKIP_DOMAINS = re.compile(
    r"(youtube\.com|facebook\.com|twitter\.com|x\.com|instagram\.com|tiktok\.com"
    r"|wikipedia\.org|reddit\.com|pinterest\.com|allegro\.pl|amazon\.|ebay\."
    r"|olx\.pl|ceneo\.pl|linkedin\.com|quora\.com|wykop\.pl|money\.pl"
    r"|bankier\.pl|wp\.pl|onet\.pl|interia\.pl|gazeta\.pl"
    r"|gov\.pl|\.edu\.pl|medium\.com|substack\.com)",
    re.IGNORECASE,
)


async def _scrape_top10_content(keyword: str, dfs_login: str, dfs_password: str, language: str = "pl") -> dict:
    """
    Fetch SERP top10 + PAA and scrape content from top URLs.
    Returns dict with serp_items, paa_questions, serp_urls. Results cached 24h.
    """
    loc_code = 2616 if language == "pl" else 2840
    cache_key = f"cw:{keyword.lower().strip()}:{loc_code}:{language}"
    cached = await _serp_cache_get(cache_key)
    if cached and isinstance(cached, dict) and "serp_items" in cached:
        logger.info(f"[ContentWriter] SERP cache hit for '{keyword}'")
        return cached

    dfs = DataForSEOClient(dfs_login, dfs_password)

    try:
        serp_raw = await dfs.serp_top10_full(keyword, loc_code, language)
        serp = serp_raw.get("organic", [])
        paa_questions = serp_raw.get("paa", [])[:6]
    except Exception:
        # Fallback to basic serp_top10
        serp = await dfs.serp_top10(keyword)
        paa_questions = []

    # Filter out social/marketplace domains, take top 5
    blog_items = [s for s in serp if not _SKIP_DOMAINS.search(s.get("url", ""))][:5]

    async def _fetch(url: str) -> str:
        if not url:
            return ""
        try:
            return await dfs.page_content(url)
        except Exception:
            return ""

    urls = [item.get("url", "") for item in blog_items]
    contents = await asyncio.gather(*[_fetch(u) for u in urls], return_exceptions=True)

    enriched = []
    serp_urls = []
    for item, content in zip(blog_items, contents):
        url = item.get("url", "")
        if isinstance(content, Exception) or not content:
            content = ""
        serp_urls.append(url)
        enriched.append({
            "rank": item.get("rank", 0),
            "url": url,
            "title": item.get("title", ""),
            "description": item.get("description", ""),
            "content_snippet": content[:3000] if content else item.get("description", ""),
        })

    result = {
        "serp_items": enriched,
        "paa_questions": paa_questions,
        "serp_urls": serp_urls,
    }
    await _serp_cache_set(cache_key, result)
    return result


def _build_serp_context(serp_data: list[dict]) -> str:
    """Build context string from SERP data for prompt."""
    lines = []
    for item in serp_data:
        lines.append(f"[#{item['rank']}] {item['title']}")
        lines.append(f"URL: {item['url']}")
        if item.get("content_snippet"):
            lines.append(f"Treść: {item['content_snippet'][:1200]}")
        lines.append("")
    return "\n".join(lines)


async def generate_seo_article(
    keyword: str,
    client_domain: str,
    anchor_text: str,
    language: str = "pl",
    anchor_text2: str = "",
    anchor_url2: str = "",
    anchor_text3: str = "",
    anchor_url3: str = "",
    custom_prompt: str = "",
    variation_hint: str = "",
    pillar_page_url: str = "",
    pillar_page_anchor: str = "",
    supporting_page_urls: list[str] = None,
    tone_of_voice: str = "ekspert",
    dfs_login: str = "",
    dfs_password: str = "",
    use_serp_scrape: bool = True,
    on_step=None,
) -> dict:
    """
    Generate SEO article with:
    - H1 = direct answer (AI Overview optimization)
    - Content based on SERP top10 analysis
    - Internal linking to pillar + supporting pages
    - Tone of voice control
    """
    if supporting_page_urls is None:
        supporting_page_urls = []

    def clean_url(url):
        url = url.strip()
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url
        # Block non-http(s) schemes (javascript:, data:, etc.)
        if not url.startswith(("http://", "https://")):
            return ""
        return url

    _step = on_step or (lambda *a: None)

    # Fetch SERP data
    _step(1, "serp", "Analiza SERP top10...")
    serp_context = ""
    paa_questions = []
    serp_urls = []
    if use_serp_scrape and dfs_login and dfs_password:
        try:
            serp_result = await _scrape_top10_content(keyword, dfs_login, dfs_password, language=language)
            serp_items = serp_result.get("serp_items", [])
            paa_questions = serp_result.get("paa_questions", [])
            serp_urls = serp_result.get("serp_urls", [])
            serp_context = _build_serp_context(serp_items)
            logger.info(f"SERP scrape OK: {len(serp_items)} results, {len(paa_questions)} PAA for '{keyword}'")
        except Exception as e:
            logger.warning(f"SERP scrape failed: {e}")

    # Build anchors
    main_anchor = f'<a href="{clean_url(client_domain)}" rel="nofollow sponsored noopener noreferrer" target="_blank">{anchor_text}</a>'
    extra_anchors = []
    if anchor_text2 and anchor_url2:
        extra_anchors.append(f'<a href="{clean_url(anchor_url2)}" rel="nofollow sponsored noopener noreferrer" target="_blank">{anchor_text2}</a>')
    if anchor_text3 and anchor_url3:
        extra_anchors.append(f'<a href="{clean_url(anchor_url3)}" rel="nofollow sponsored noopener noreferrer" target="_blank">{anchor_text3}</a>')

    # Internal linking instructions
    internal_links_info = ""
    if pillar_page_url and pillar_page_anchor:
        internal_links_info += f'\n- Pilar page: <a href="{clean_url(pillar_page_url)}">{pillar_page_anchor}</a> (link naturalnie w treści, w pierwszej połowie artykułu)'
    for i, sup_url in enumerate(supporting_page_urls[:5]):
        internal_links_info += f'\n- Supporting page: <a href="{clean_url(sup_url)}">{sup_url}</a>'

    tone_map = {
        "ekspert": "Ton eksperta z 20+ letnim doświadczeniem. Autorytatywny, konkretny, bez lania wody. Fakty i przykłady.",
        "przyjazny": "Ton przyjaznego doradcy. Przystępny, ciepły, prosty język. Tłumacz jak przyjaciel.",
        "formalny": "Ton formalny, biznesowy. Precyzyjny, profesjonalny, bez kolokwializmów.",
        "poradnikowy": "Ton poradnika krok po kroku. Praktyczny, klarowny, numerowane listy i instrukcje.",
    }
    tone_instruction = tone_map.get(tone_of_voice, tone_map["ekspert"])

    variation = f" Kąt tematyczny: {variation_hint}." if variation_hint else ""

    paa_section = ""
    if paa_questions:
        paa_list = "\n".join(f"- {q}" for q in paa_questions[:6])
        paa_section = f"\nPYTANIA Z GOOGLE (People Also Ask) — użyj ich w FAQ:\n{paa_list}\n"

    serp_section = ""
    if serp_context:
        # Custom LLM (local/small model) — trim SERP context to avoid context window overflow
        _serp_content = serp_context
        serp_section = f"""
ANALIZA TOP10 SERP DLA FRAZY "{keyword}":
{_serp_content}

Na podstawie powyższej analizy:
- Zidentyfikuj wspólne tematy i sekcje, które pokrywają rywale
- Wypełnij luki informacyjne (napisz o tym, czego brakuje konkurencji)
- Użyj lepszej struktury i głębszych odpowiedzi
"""

    if language == "pl":
        system_prompt = (
            "Jesteś ekspertem SEO i copywriterem z doświadczeniem E-E-A-T. "
            "Tworzysz artykuły zoptymalizowane pod AI Overview Google, "
            "z H1 zawierającym bezpośrednią odpowiedź i <strong> dla kluczowych terminów. "
            "Zwracaj treść w formacie HTML (tylko body, bez <html>/<body> tagów)."
        )
        user_prompt = f"""Napisz kompletny artykuł SEO na frazę: '{keyword}'.{variation}

{serp_section}
{paa_section}
TON GŁOSU: {tone_instruction}

WYMAGANIA TECHNICZNE SEO:
1. H1 = BEZPOŚREDNIA ODPOWIEDŹ + definicja '{keyword}' (1-2 zdania, format AI Overview/Featured Snippet)
   Przykład: "<h1><strong>{keyword}</strong> — co to jest i jak działa?</h1>"
2. Wstęp: pierwszy akapit = definicja z <strong>{keyword}</strong> (AI Overview style)
3. 6-8 sekcji H2, każda z 1-2 podsekcjami H3
4. Używaj <strong> dla kluczowych terminów, liczb i ważnych faktów
5. Mix formatów: <p>, <ul>/<li>, <ol>/<li> — nie same akapity
6. FAQ na końcu: min 8 pytań (użyj pytań PAA powyżej jeśli dostępne + własne), format <h3>Pytanie?</h3><p>Odpowiedź.</p>
7. Podsumowanie z <ul> kluczowych wniosków
8. Łączna długość: 1800-2500 słów
9. '{keyword}' naturalnie 1-2% density
10. ENCJE NLP: Używaj konkretnych nazw własnych (marki, firmy, produkty, osoby, miejsca, normy, instytucje) powiązanych z tematem. Google NLP rozpoznaje encje — im więcej trafnych nazw własnych, tym lepszy topical authority. Unikaj ogólników typu "eksperci twierdzą" — podaj KTO konkretnie.

LINKOWANIE:
- Umieść DOKŁADNIE RAZ link do klienta: {main_anchor}
{('- Umieść też: ' + ', '.join(extra_anchors)) if extra_anchors else ''}
{('LINKOWANIE WEWNĘTRZNE:' + internal_links_info) if internal_links_info else ''}

{f'DODATKOWE INSTRUKCJE: {custom_prompt}' if custom_prompt else ''}

Zwróć JSON z polami:
- "title": tytuł SEO (50-60 znaków, zawiera '{keyword}')
- "meta_description": meta opis (150-160 znaków, CTA na końcu)
- "content": pełny HTML artykułu
- "category": 1 główna kategoria bloga (1-3 słowa, np. "Poradniki", "Finanse", "Zdrowie")
- "tags": lista 10 tagów WP (krótkie frazy LSI, tematycznie powiązane z '{keyword}')
Tylko JSON, bez markdown."""
    else:
        system_prompt = (
            "You are an SEO expert and copywriter with E-E-A-T expertise. "
            "Create articles optimized for Google AI Overview, "
            "with H1 containing a direct answer and <strong> for key terms. "
            "Return content in HTML format (body only, no <html>/<body> tags)."
        )
        user_prompt = f"""Write a complete SEO article for keyword: '{keyword}'.{variation}

{serp_section}
{paa_section}
TONE OF VOICE: {tone_instruction}

SEO REQUIREMENTS:
1. H1 = DIRECT ANSWER + definition of '{keyword}' (1-2 sentences, AI Overview/Featured Snippet format)
2. Intro: first paragraph = definition with <strong>{keyword}</strong> (AI Overview style)
3. 6-8 H2 sections, each with 1-2 H3 subsections
4. Use <strong> for key terms, numbers, and important facts
5. Mix formats: <p>, <ul>/<li>, <ol>/<li> — not just paragraphs
6. FAQ at the end: min 8 questions (use PAA questions above if available + your own), <h3>Question?</h3><p>Answer.</p>
7. Summary with <ul> of key takeaways
8. Total length: 1800-2500 words
9. '{keyword}' naturally at 1-2% density
10. NLP ENTITIES: Use specific proper nouns (brands, companies, products, people, places, standards, institutions) related to the topic. Google NLP recognizes entities — more relevant proper nouns means better topical authority. Avoid vague phrases like "experts say" — name WHO specifically.

LINKS:
- Place EXACTLY ONCE: {main_anchor}
{('- Also include: ' + ', '.join(extra_anchors)) if extra_anchors else ''}
{('INTERNAL LINKS:' + internal_links_info) if internal_links_info else ''}

{f'ADDITIONAL INSTRUCTIONS: {custom_prompt}' if custom_prompt else ''}

Return JSON with:
- "title": SEO title (50-60 chars, contains '{keyword}')
- "meta_description": meta description (150-160 chars, with CTA at the end)
- "content": full HTML article
- "category": 1 main blog category (1-3 words, e.g. "Guides", "Finance", "Health")
- "tags": list of 10 WP tags (short LSI phrases related to '{keyword}')
JSON only, no markdown."""

    _step(2, "gpt", "Generowanie artykulu (GPT)...")
    _cw_client, _active_model, _is_custom = await get_openai_client()

    # Custom/local LLMs (Llama, Mistral etc.) have smaller context windows —
    # trim SERP context and reduce max_tokens to avoid 400 context-exceeded errors
    if _is_custom and serp_section:
        # Keep only first 1500 chars of SERP section instead of full ~6000
        _serp_trimmed = serp_section[:1500]
        user_prompt = user_prompt.replace(serp_section, _serp_trimmed)
        serp_section = _serp_trimmed
    _max_tokens = 2500 if _is_custom else 6000

    for attempt in range(3):
        try:
            response = await _cw_client.chat.completions.create(
                model=_active_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.7,
                max_tokens=_max_tokens,
                response_format={"type": "json_object"},
            )
            break
        except Exception as e:
            if attempt == 2:
                raise
            await asyncio.sleep(2 ** attempt)
            logger.warning(f"[ContentWriter] GPT attempt {attempt+1} failed: {e}")

    raw = response.choices[0].message.content
    data = json.loads(raw)
    content = data.get("content", "")
    title_out = data.get("title", keyword)
    # Ensure H1 exists — many WP themes don't add it automatically
    if content and not re.search(r"<h1", content, re.IGNORECASE):
        content = f"<h1>{title_out}</h1>\n\n" + content

    # SEO #45: improved link dedup — URL-normalized, also remove empty/broken hrefs
    seen_hrefs = set()
    def dedup_link(m):
        href = re.search(r'href=["\']([^"\']+)["\']', m.group(0))
        if not href:
            return m.group(0)
        url = href.group(1).rstrip("/")
        if not url or url in ("#", "javascript:void(0)"):
            return re.sub(r"<[^>]+>", "", m.group(0))
        if url in seen_hrefs:
            return re.sub(r"<[^>]+>", "", m.group(0))
        seen_hrefs.add(url)
        return m.group(0)
    content = re.sub(r"<a\s[^>]+>.*?</a>", dedup_link, content, flags=re.DOTALL)

    # FAQPage JSON-LD — inject for rich snippets (Yoast only auto-generates from its own FAQ blocks)
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
        faq_schema = f'<script type="application/ld+json">{json.dumps(faq_ld, ensure_ascii=False)}</script>'
        content = content + "\n" + faq_schema
    # SEO #39: Article JSON-LD schema for rich snippets
    import random as _rnd_cw
    from datetime import datetime as _dt_cw, timezone as _tz_cw
    _now_iso = _dt_cw.now(_tz_cw.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    _author_pool = [
        {"name": "Redakcja", "desc": "Zespół ekspertów"},
        {"name": "Ekspert Tematyczny", "desc": "Specjalista z doświadczeniem"},
        {"name": "Zespół Redakcyjny", "desc": "Autorzy portalu"},
    ] if language == "pl" else [
        {"name": "Editorial Team", "desc": "Team of experts"},
        {"name": "Subject Expert", "desc": "Specialist with experience"},
        {"name": "Staff Writers", "desc": "Professional writers"},
    ]
    _auth = _rnd_cw.choice(_author_pool)
    # SEO #120: author URL for E-E-A-T
    _auth_slug_cw = re.sub(r'[^a-z0-9]+', '-', _auth["name"].lower()).strip('-')
    from services.article_helpers import count_words as _cw_count
    # SEO #101: extract first image from content for Article schema
    _first_img_cw = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content, re.IGNORECASE)
    article_ld = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title_out[:110],
        "description": data.get("meta_description", "")[:155],
        "datePublished": _now_iso,
        "dateModified": _now_iso,
        "author": {"@type": "Person", "name": _auth["name"], "description": _auth["desc"], "url": f"/author/{_auth_slug_cw}"},
        "publisher": {"@type": "Organization", "name": client_domain.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0] if client_domain else "Publisher"},
        "mainEntityOfPage": {"@type": "WebPage"},
        "wordCount": _cw_count(content),
        "inLanguage": language,
    }
    # SEO #101: add image field (required for Google rich results)
    if _first_img_cw:
        article_ld["image"] = _first_img_cw.group(1)
    article_schema = f'<script type="application/ld+json">{json.dumps(article_ld, ensure_ascii=False)}</script>'
    content = content + "\n" + article_schema
    # SEO #104: VideoObject schema for YouTube embeds
    _yt_cw = re.findall(r'(?:src=["\'])(?:https?://)?(?:www\.)?(?:youtube\.com/embed/|youtu\.be/)([a-zA-Z0-9_-]{11})', content)
    if _yt_cw:
        for _yt_id in _yt_cw[:2]:
            _vid_ld = {
                "@context": "https://schema.org",
                "@type": "VideoObject",
                "name": title_out[:110],
                "description": data.get("meta_description", "")[:155] or keyword,
                "thumbnailUrl": f"https://img.youtube.com/vi/{_yt_id}/maxresdefault.jpg",
                "uploadDate": _now_iso,
                "embedUrl": f"https://www.youtube.com/embed/{_yt_id}",
            }
            content = content + "\n" + f'<script type="application/ld+json">{json.dumps(_vid_ld, ensure_ascii=False)}</script>'

    # SEO #40: BreadcrumbList schema
    breadcrumb_ld = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": "/"},
            {"@type": "ListItem", "position": 2, "name": keyword[:50]},
        ],
    }
    content = content + "\n" + f'<script type="application/ld+json">{json.dumps(breadcrumb_ld, ensure_ascii=False)}</script>'

    excerpt = data.get("meta_description", "")
    # SEO #44: enforce 155 char limit for meta description
    if excerpt and len(excerpt) > 155:
        excerpt = excerpt[:155].rsplit(' ', 1)[0].rstrip('.,;:') + '.'
    excerpt = re.sub(r'^(?:Meta\s*(?:description|opis)\s*:?\s*)', '', excerpt, flags=re.IGNORECASE).strip()
    # SEO #103: dynamic CTA in meta description based on detected intent
    _cw_intent = "informational"
    for _it, _ip in [("transactional", ["kup", "cena", "sklep", "buy", "price", "shop"]),
                     ("commercial", ["porównanie", "ranking", "najlepsze", "compare", "best", "review"]),
                     ("navigational", ["login", "kontakt", "strona", "contact", "official"])]:
        if any(p in keyword.lower() for p in _ip):
            _cw_intent = _it
            break
    _cta_map_cw = {"informational": "Dowiedz się więcej", "transactional": "Sprawdź ofertę",
                   "commercial": "Porównaj opcje", "navigational": "Przejdź na stronę"} if language == "pl" else {
                   "informational": "Learn more", "transactional": "Shop now",
                   "commercial": "Compare options", "navigational": "Visit the page"}
    _cta_cw = _cta_map_cw.get(_cw_intent, _cta_map_cw["informational"])
    if excerpt and not any(cta in excerpt for cta in _cta_map_cw.values()):
        _space = len(excerpt) + len(_cta_cw) + 3
        if _space <= 155:
            excerpt = excerpt.rstrip('.!') + f". {_cta_cw}."
    sections = [re.sub(r"<[^>]+>", "", m).strip() for m in re.findall(r"<h2[^>]*>(.*?)</h2>", content, re.DOTALL)][:8]

    # SEO #41: content freshness signal
    from datetime import datetime as _dt41, timezone as _tz41
    _freshness_date = _dt41.now(_tz41.utc).strftime("%d.%m.%Y")
    _freshness_label = "Ostatnia aktualizacja" if language == "pl" else "Last updated"
    _freshness_tag = f'<p style="font-size:0.85em;color:#666;margin-bottom:16px;"><time datetime="{_dt41.now(_tz41.utc).strftime("%Y-%m-%d")}">{_freshness_label}: {_freshness_date}</time></p>'
    content = _freshness_tag + "\n" + content

    # SEO #119: layout_variant for content_writer (same as openai_service: faq_top/tldr/short_answer/standard)
    _rv_cw = random.random()
    _layout_cw = "faq_top" if _rv_cw < 0.30 else ("tldr" if _rv_cw < 0.50 else ("short_answer" if _rv_cw < 0.75 else "standard"))
    # Reorder FAQ block if faq_top layout selected
    _faq_match = re.search(r'(<h2[^>]*>(?:FAQ|Najczęściej|Frequently|Pytania).*)', content, re.DOTALL | re.IGNORECASE)
    if _layout_cw == "faq_top" and _faq_match:
        _faq_block = _faq_match.group(0)
        content = _faq_block + "\n" + content[:_faq_match.start()] + content[_faq_match.end():]

    # SEO #42: wrap content in lang div
    content = f'<div lang="{language}">\n{content}\n</div>'

    # SEO #122: robots meta hint in HTML comment (SEO plugins read from post meta, not HTML)
    content = f'<!-- robots: index, follow, max-snippet:-1, max-image-preview:large -->\n{content}'

    _step(3, "enrichment", "Wzbogacanie tresci (TOC, FAQ)...")
    # ── Enrichments (TOC + update_box + 2 random elements) ─────────────────
    lang_pl = language == "pl"
    try:
        _enrich_client, _, _ = await get_openai_client()
        content = await enrich_article(
            content=content,
            topic=keyword,
            sections=sections,
            lang_pl=lang_pl,
            openai_client=_enrich_client,
            serp_urls=serp_urls or None,
        )
    except Exception as e:
        logger.warning(f"[ContentWriter] Enrichment failed: {e}")

    # Fix heading hierarchy (H3 before H2, skipped levels, etc.)
    content = _fix_heading_hierarchy(content)

    _step(4, "done", "Gotowe!")
    raw_tags = data.get("tags", [])
    tags_out = [t for t in raw_tags if isinstance(t, str) and t.strip()][:10]

    return {
        "title": data.get("title", keyword),
        "meta_description": excerpt,
        "content": content,
        "category": data.get("category", ""),
        "tags": tags_out,
    }

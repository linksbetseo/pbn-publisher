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
from openai import AsyncOpenAI
from config import OPENAI_API_KEY
from services.dataforseo_service import DataForSEOClient
from services.content_enrichments import enrich_article
# Reuse SERP cache from openai_service to avoid duplicate DataForSEO calls
from services.article_helpers import serp_cache_get as _serp_cache_get, serp_cache_set as _serp_cache_set
from services.openai_service import get_gpt_model, _fix_heading_hierarchy

logger = logging.getLogger(__name__)

client_ai = AsyncOpenAI(api_key=OPENAI_API_KEY)


_SKIP_DOMAINS = re.compile(
    r"(youtube\.com|facebook\.com|twitter\.com|instagram\.com|wikipedia\.org"
    r"|reddit\.com|allegro\.pl|amazon\.|ebay\.|olx\.pl|ceneo\.pl|linkedin\.com)",
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
    main_anchor = f'<a href="{clean_url(client_domain)}">{anchor_text}</a>'
    extra_anchors = []
    if anchor_text2 and anchor_url2:
        extra_anchors.append(f'<a href="{clean_url(anchor_url2)}">{anchor_text2}</a>')
    if anchor_text3 and anchor_url3:
        extra_anchors.append(f'<a href="{clean_url(anchor_url3)}">{anchor_text3}</a>')

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
        serp_section = f"""
ANALIZA TOP10 SERP DLA FRAZY "{keyword}":
{serp_context}

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
- "tags": lista 5 tagów WP (krótkie frazy LSI, tematycznie powiązane z '{keyword}')
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
- "tags": list of 5 WP tags (short LSI phrases related to '{keyword}')
JSON only, no markdown."""

    _step(2, "gpt", "Generowanie artykulu (GPT)...")
    _active_model = await get_gpt_model()
    for attempt in range(3):
        try:
            response = await client_ai.chat.completions.create(
                model=_active_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.7,
                max_tokens=6000,
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

    # Deduplicate links
    seen_hrefs = set()
    def dedup_link(m):
        href = re.search(r'href=["\']([^"\']+)["\']', m.group(0))
        if not href:
            return m.group(0)
        url = href.group(1).rstrip("/")
        if url in seen_hrefs:
            return re.sub(r"<[^>]+>", "", m.group(0))
        seen_hrefs.add(url)
        return m.group(0)
    content = re.sub(r"<a\s[^>]+>.*?</a>", dedup_link, content, flags=re.DOTALL)

    # Schema.org JSON-LD removed — Yoast/RankMath generate schema automatically.
    # Injecting JSON-LD in post_content causes duplicate schema issues.
    excerpt = data.get("meta_description", "")
    sections = [re.sub(r"<[^>]+>", "", m).strip() for m in re.findall(r"<h2[^>]*>(.*?)</h2>", content, re.DOTALL)][:8]

    _step(3, "enrichment", "Wzbogacanie tresci (TOC, FAQ)...")
    # ── Enrichments (TOC + update_box + 2 random elements) ─────────────────
    lang_pl = language == "pl"
    try:
        content = await enrich_article(
            content=content,
            topic=keyword,
            sections=sections,
            lang_pl=lang_pl,
            openai_client=client_ai,
            serp_urls=serp_urls or None,
        )
    except Exception as e:
        logger.warning(f"[ContentWriter] Enrichment failed: {e}")

    # Fix heading hierarchy (H3 before H2, skipped levels, etc.)
    content = _fix_heading_hierarchy(content)

    _step(4, "done", "Gotowe!")
    raw_tags = data.get("tags", [])
    tags_out = [t for t in raw_tags if isinstance(t, str) and t.strip()][:5]

    return {
        "title": data.get("title", keyword),
        "meta_description": excerpt,
        "content": content,
        "category": data.get("category", ""),
        "tags": tags_out,
    }

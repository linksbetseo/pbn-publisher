"""
SEO Content Writer Service.
Generates full articles based on SERP top10 analysis.
Optimized for AI Overview (answer in H1), internal linking, tone of voice.
"""
import asyncio
import json
import logging
import re
from openai import AsyncOpenAI
from config import OPENAI_API_KEY
from services.dataforseo_service import DataForSEOClient

logger = logging.getLogger(__name__)

client_ai = AsyncOpenAI(api_key=OPENAI_API_KEY)


async def _scrape_top10_content(keyword: str, dfs_login: str, dfs_password: str) -> list[dict]:
    """Fetch SERP top10 and scrape content from each page."""
    dfs = DataForSEOClient(dfs_login, dfs_password)
    serp = await dfs.serp_top10(keyword)

    enriched = []
    for item in serp[:5]:  # Scrape top 5 to save API credits
        url = item.get("url", "")
        content = ""
        if url:
            try:
                content = await dfs.page_content(url)
            except Exception as e:
                logger.warning(f"Content fetch failed for {url}: {e}")
        enriched.append({
            "rank": item["rank"],
            "url": url,
            "title": item["title"],
            "description": item.get("description", ""),
            "content_snippet": content[:2000] if content else item.get("description", ""),
        })
    return enriched


def _build_serp_context(serp_data: list[dict]) -> str:
    """Build context string from SERP data for prompt."""
    lines = []
    for item in serp_data:
        lines.append(f"[#{item['rank']}] {item['title']}")
        lines.append(f"URL: {item['url']}")
        if item.get("content_snippet"):
            lines.append(f"Treść: {item['content_snippet'][:800]}")
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

    # Fetch SERP data
    serp_context = ""
    if use_serp_scrape and dfs_login and dfs_password:
        try:
            serp_data = await _scrape_top10_content(keyword, dfs_login, dfs_password)
            serp_context = _build_serp_context(serp_data)
            logger.info(f"SERP scrape OK: {len(serp_data)} results for '{keyword}'")
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
TON GŁOSU: {tone_instruction}

WYMAGANIA TECHNICZNE SEO:
1. H1 = BEZPOŚREDNIA ODPOWIEDŹ + definicja '{keyword}' (1-2 zdania, format AI Overview/Featured Snippet)
   Przykład: "<h1><strong>{keyword}</strong> — co to jest i jak działa?</h1>"
2. Wstęp: pierwszy akapit = definicja z <strong>{keyword}</strong> (AI Overview style)
3. 6-8 sekcji H2, każda z 1-2 podsekcjami H3
4. Używaj <strong> dla kluczowych terminów, liczb i ważnych faktów
5. Mix formatów: <p>, <ul>/<li>, <ol>/<li> — nie same akapity
6. FAQ na końcu: min 8 pytań (co to, jak, ile, czy, kiedy), format <h3>Pytanie?</h3><p>Odpowiedź.</p>
7. Podsumowanie z <ul> kluczowych wniosków
8. Łączna długość: 1800-2500 słów
9. '{keyword}' naturalnie 1-2% density

LINKOWANIE:
- Umieść DOKŁADNIE RAZ link do klienta: {main_anchor}
{('- Umieść też: ' + ', '.join(extra_anchors)) if extra_anchors else ''}
{('LINKOWANIE WEWNĘTRZNE:' + internal_links_info) if internal_links_info else ''}

{f'DODATKOWE INSTRUKCJE: {custom_prompt}' if custom_prompt else ''}

Zwróć JSON z polami:
- "title": tytuł SEO (50-60 znaków, zawiera '{keyword}')
- "meta_description": meta opis (150-160 znaków, CTA na końcu)
- "content": pełny HTML artykułu
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
TONE OF VOICE: {tone_instruction}

SEO REQUIREMENTS:
1. H1 = DIRECT ANSWER + definition of '{keyword}' (1-2 sentences, AI Overview/Featured Snippet format)
2. Intro: first paragraph = definition with <strong>{keyword}</strong> (AI Overview style)
3. 6-8 H2 sections, each with 1-2 H3 subsections
4. Use <strong> for key terms, numbers, and important facts
5. Mix formats: <p>, <ul>/<li>, <ol>/<li> — not just paragraphs
6. FAQ at the end: min 8 questions (what is, how, how much, does, when), <h3>Question?</h3><p>Answer.</p>
7. Summary with <ul> of key takeaways
8. Total length: 1800-2500 words
9. '{keyword}' naturally at 1-2% density

LINKS:
- Place EXACTLY ONCE: {main_anchor}
{('- Also include: ' + ', '.join(extra_anchors)) if extra_anchors else ''}
{('INTERNAL LINKS:' + internal_links_info) if internal_links_info else ''}

{f'ADDITIONAL INSTRUCTIONS: {custom_prompt}' if custom_prompt else ''}

Return JSON with:
- "title": SEO title (50-60 chars, contains '{keyword}')
- "meta_description": meta description (150-160 chars, with CTA at the end)
- "content": full HTML article
JSON only, no markdown."""

    for attempt in range(3):
        try:
            response = await client_ai.chat.completions.create(
                model="gpt-4o-mini",
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

    return {
        "title": data.get("title", keyword),
        "meta_description": data.get("meta_description", ""),
        "content": content,
    }

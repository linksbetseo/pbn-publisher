"""
Multi-pass article generation following n8n workflow pattern:
1. DataForSEO SERP top 10 → select 3 blog URLs
2. DataForSEO content parsing per URL → extract headings + text
3. GPT: keyword cluster + search intent
4. GPT: outline (sections separated by <<<<)
5. GPT: title
6. GPT: intro
7. GPT: each section separately
8. GPT: conclusion
9. GPT: FAQ
10. GPT: excerpt
11. Assemble HTML → publish
"""
import json
import logging
import re
from typing import Optional

import httpx
from openai import AsyncOpenAI

from config import OPENAI_API_KEY

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
logger = logging.getLogger(__name__)

_BLOG_SKIP = re.compile(
    r"(youtube\.com|facebook\.com|twitter\.com|instagram\.com|tiktok\.com"
    r"|wikipedia\.org|reddit\.com|pinterest\.com|allegro\.pl|amazon\.|ebay\."
    r"|olx\.pl|ceneo\.pl|sklepik|sklep|shop|store|kup|buy|cart|koszyk)",
    re.IGNORECASE,
)


def _is_blog_url(url: str) -> bool:
    return not _BLOG_SKIP.search(url)


def _count_words(text: str) -> int:
    return len(re.findall(r'\w+', text))


def _keyword_density(text: str, keyword: str) -> float:
    """Returns keyword density as percentage."""
    words = _count_words(text)
    if not words:
        return 0.0
    kw_words = keyword.lower().split()
    text_lower = text.lower()
    # Count non-overlapping occurrences of the full phrase
    count = len(re.findall(re.escape(keyword.lower()), text_lower))
    # Density = (keyword occurrences * keyword word count) / total words
    return round((count * len(kw_words)) / words * 100, 2)


async def _fetch_serp_content(
    topic: str,
    dfs_login: str,
    dfs_password: str,
    location_code: int = 2616,
    language_code: str = "pl",
) -> dict:
    """
    Fetch top 3 blog URLs from SERP, parse content.
    Returns dict with:
      - text: combined scraped text
      - avg_words: average word count of top3 articles
      - avg_density: average keyword density in top3
    """
    empty = {"text": "", "avg_words": 0, "avg_density": 0.0}
    if not dfs_login or not dfs_password:
        return empty

    try:
        from services.dataforseo_service import DataForSEOClient
        dfs = DataForSEOClient(dfs_login, dfs_password)

        serp = await dfs.serp_top10(topic, location_code, language_code)
        blog_urls = [r["url"] for r in serp if r.get("url") and _is_blog_url(r["url"])][:3]

        if not blog_urls:
            logger.warning("[SERP] No blog URLs found in SERP results")
            return empty

        logger.info(f"[SERP] Parsing {len(blog_urls)} URLs: {blog_urls}")

        parts = []
        word_counts = []
        densities = []
        for url in blog_urls:
            content = await dfs.page_content(url)
            if content:
                wc = _count_words(content)
                dens = _keyword_density(content, topic)
                word_counts.append(wc)
                densities.append(dens)
                parts.append(f"--- {url} ({wc} słów, gęstość KW: {dens}%) ---\n{content[:3000]}")

        avg_words = int(sum(word_counts) / len(word_counts)) if word_counts else 0
        avg_density = round(sum(densities) / len(densities), 2) if densities else 0.0

        logger.info(f"[SERP] avg_words={avg_words}, avg_density={avg_density}%")
        return {
            "text": "\n\n".join(parts),
            "avg_words": avg_words,
            "avg_density": avg_density,
        }

    except Exception as e:
        logger.warning(f"[SERP] Failed to fetch competitor content: {e}")
        return empty


def _markdown_to_html(text: str) -> str:
    """Convert basic markdown to HTML."""
    # Headers
    text = re.sub(r"^### (.+)$", r"<h3>\1</h3>", text, flags=re.MULTILINE)
    text = re.sub(r"^## (.+)$", r"<h2>\1</h2>", text, flags=re.MULTILINE)
    text = re.sub(r"^# (.+)$", r"<h1>\1</h1>", text, flags=re.MULTILINE)
    # Bold
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # Italic
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    # Unordered lists
    text = re.sub(r"(?m)^[-*] (.+)$", r"<li>\1</li>", text)
    text = re.sub(r"(<li>.*?</li>)+", lambda m: f"<ul>{m.group(0)}</ul>", text, flags=re.DOTALL)
    # Ordered lists
    text = re.sub(r"(?m)^\d+\. (.+)$", r"<li>\1</li>", text)
    # Paragraphs: wrap non-tagged lines
    lines = text.split("\n")
    result = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("<"):
            result.append(line)
        else:
            result.append(f"<p>{line}</p>")
    return "\n".join(result)


async def _gpt(system: str, user: str, temperature: float = 0.7, max_tokens: int = 2000) -> str:
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content.strip()


def _inject_anchors(html: str, anchors_info: str) -> str:
    """Inject anchor links naturally into the HTML content."""
    if not anchors_info:
        return html
    # Find anchor tags in anchors_info and inject the first one into a <p> tag
    links = re.findall(r'<a\s[^>]*>.*?</a>', anchors_info, re.DOTALL | re.IGNORECASE)
    seen_hrefs: set = set()
    paragraphs = re.findall(r'<p>.*?</p>', html, re.DOTALL)
    para_count = len(paragraphs)

    for i, link in enumerate(links):
        href_match = re.search(r'href=["\']([^"\']+)["\']', link)
        if not href_match:
            continue
        href = href_match.group(1).rstrip("/")
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)

        # Find a paragraph roughly 1/3 into the article per link, skip already-linked ones
        target_idx = max(0, min(para_count - 1, (para_count // (len(links) + 1)) * (i + 1)))
        para = paragraphs[target_idx] if target_idx < para_count else None
        if para and link not in html:
            # Append link inside the paragraph
            new_para = para[:-4] + f" {link}</p>"
            html = html.replace(para, new_para, 1)

    return html


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
) -> dict:
    def clean_url(url: str) -> str:
        url = url.strip()
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url
        return url

    # Build anchor HTML
    anchors_info = f'<a href="{clean_url(client_domain)}">{anchor_text}</a>'
    if anchor_text2 and anchor_url2:
        anchors_info += f', <a href="{clean_url(anchor_url2)}">{anchor_text2}</a>'
    if anchor_text3 and anchor_url3:
        anchors_info += f', <a href="{clean_url(anchor_url3)}">{anchor_text3}</a>'

    variation = f" Kąt tematyczny: {variation_hint}." if variation_hint else ""
    lang_pl = language == "pl"

    # ── STEP 1: Fetch competitor content from SERP ──────────────────────────
    language_code = "pl" if lang_pl else "en"
    serp_data = await _fetch_serp_content(topic, dfs_login, dfs_password, location_code, language_code)
    serp_text = serp_data["text"]
    avg_words = serp_data["avg_words"] or 1200   # fallback target
    avg_density = serp_data["avg_density"] or 1.5
    # Target word count: match top10 avg, minimum 800
    target_words = max(800, avg_words)
    # Target density: match top10, clamp to 0.5–3.0%
    target_density = round(max(0.5, min(3.0, avg_density)), 1)
    serp_block = f"\n\n[SEO Scraped Info — top 3 konkurentów]\n{serp_text}" if serp_text else ""

    logger.info(f"[Article] Target: {target_words} słów, gęstość KW: {target_density}%")

    # ── STEP 2: Keyword cluster + search intent ──────────────────────────────
    if lang_pl:
        intent_system = "Jesteś ekspertem SEO. Analizujesz słowa kluczowe i intencje wyszukiwania."
        intent_user = (
            f"Dla frazy: '{topic}'{variation}\n"
            f"Podaj krótko (max 3 zdania):\n"
            f"1. Główna intencja wyszukiwania (informacyjna/transakcyjna/nawigacyjna)\n"
            f"2. Cluster tematyczny (co chce wiedzieć użytkownik)\n"
            f"3. Kluczowe encje i tematy pokrewne do uwzględnienia w artykule"
            f"{serp_block}"
        )
    else:
        intent_system = "You are an SEO expert. You analyze keywords and search intent."
        intent_user = (
            f"For keyword: '{topic}'{variation}\n"
            f"Briefly describe (max 3 sentences):\n"
            f"1. Search intent (informational/transactional/navigational)\n"
            f"2. Topic cluster (what the user wants to know)\n"
            f"3. Key entities and related topics to include in the article"
            f"{serp_block}"
        )

    intent_analysis = await _gpt(intent_system, intent_user, temperature=0.3, max_tokens=400)
    logger.info(f"[Article] Intent: {intent_analysis[:100]}")

    # ── STEP 3: Outline ───────────────────────────────────────────────────────
    # Estimate number of sections to hit target word count (~200 words per section)
    n_sections = max(4, min(8, round(target_words / 200)))

    if lang_pl:
        outline_system = (
            "Jesteś ekspertem SEO tworzącym struktury artykułów. "
            "Generuj outline jako listę nagłówków H2, każdy na osobnej linii, oddzielony '<<<<'. "
            "Tylko nagłówki, bez tekstu, bez numeracji."
        )
        outline_user = (
            f"Stwórz outline artykułu SEO dla frazy: '{topic}'\n"
            f"Intencja i encje: {intent_analysis}\n"
            f"Wymagania: DOKŁADNIE {n_sections} sekcji H2 (docelowa długość artykułu: {target_words} słów), "
            f"każda oddzielona '<<<<', bez wstępu i zakończenia (te będą osobno).\n"
            f"Tylko nagłówki H2, bez tekstu.{serp_block}"
        )
    else:
        outline_system = (
            "You are an SEO expert creating article structures. "
            "Generate an outline as a list of H2 headings, each on a separate line, separated by '<<<<'. "
            "Only headings, no body text, no numbering."
        )
        outline_user = (
            f"Create an SEO article outline for keyword: '{topic}'\n"
            f"Intent and entities: {intent_analysis}\n"
            f"Requirements: EXACTLY {n_sections} H2 sections (target article length: {target_words} words), "
            f"each separated by '<<<<', without intro and conclusion (those will be separate).\n"
            f"Only H2 headings, no body text.{serp_block}"
        )

    outline_raw = await _gpt(outline_system, outline_user, temperature=0.5, max_tokens=500)
    sections = [s.strip() for s in outline_raw.split("<<<<") if s.strip()]
    logger.info(f"[Article] Outline sections: {sections}")

    if not sections:
        sections = [topic]

    # ── STEP 4: Title ─────────────────────────────────────────────────────────
    if lang_pl:
        title_system = "Jesteś copywriterem SEO. Generujesz chwytliwe tytuły artykułów."
        title_user = (
            f"Wymyśl jeden unikalny tytuł artykułu SEO dla frazy: '{topic}'\n"
            f"Intencja: {intent_analysis}\n"
            f"Sekcje artykułu: {', '.join(sections[:3])}\n"
            f"Tytuł ma być inny niż fraza, może być pytaniem lub poradnikiem. "
            f"Zwróć tylko tytuł, bez cudzysłowów."
        )
    else:
        title_system = "You are an SEO copywriter. You generate compelling article titles."
        title_user = (
            f"Create one unique SEO article title for keyword: '{topic}'\n"
            f"Intent: {intent_analysis}\n"
            f"Article sections: {', '.join(sections[:3])}\n"
            f"The title should be different from the keyword, can be a question or guide. "
            f"Return only the title, no quotes."
        )

    title = await _gpt(title_system, title_user, temperature=0.8, max_tokens=100)
    title = title.strip('"\'').strip()
    logger.info(f"[Article] Title: {title}")

    # ── STEP 5: Intro ─────────────────────────────────────────────────────────
    if lang_pl:
        intro_system = (
            "Jesteś ekspertem SEO. Piszesz wstępy zoptymalizowane pod AI Overview i featured snippets. "
            "Struktura wstępu: "
            "1) PIERWSZY akapit = bezpośrednia, konkretna odpowiedź na pytanie/temat (max 2-3 zdania, format odpowiedni dla AI Overview). "
            "2) Kolejne 2 akapity = rozwinięcie kontekstu i zapowiedź artykułu. "
            "Używaj tagów HTML <p>."
        )
        intro_user = (
            f"Napisz wstęp do artykułu '{title}' o temacie: '{topic}'.\n"
            f"Intencja czytelnika: {intent_analysis}\n"
            f"Główne sekcje: {', '.join(sections[:3])}\n"
            f"WAŻNE: Pierwszy akapit musi zawierać BEZPOŚREDNIĄ odpowiedź na '{topic}' "
            f"— krótko i konkretnie, jak w AI Overview Google.\n"
            f"Użyj frazy '{topic}' naturalnie {max(1, round(target_words * target_density / 100 * 0.15))} razy we wstępie.\n"
            f"Zwróć tylko HTML (<p> tagi), bez nagłówków."
        )
    else:
        intro_system = (
            "You are an SEO expert. You write introductions optimized for AI Overview and featured snippets. "
            "Structure: "
            "1) FIRST paragraph = direct, concise answer to the topic (max 2-3 sentences, AI Overview format). "
            "2) Next 2 paragraphs = context and article preview. "
            "Use HTML <p> tags."
        )
        intro_user = (
            f"Write an introduction for article '{title}' about: '{topic}'.\n"
            f"Reader intent: {intent_analysis}\n"
            f"Main sections: {', '.join(sections[:3])}\n"
            f"IMPORTANT: First paragraph must contain a DIRECT answer to '{topic}' "
            f"— brief and concrete, like Google AI Overview format.\n"
            f"Use '{topic}' naturally {max(1, round(target_words * target_density / 100 * 0.15))} times in the intro.\n"
            f"Return only HTML (<p> tags), no headings."
        )

    intro_html = await _gpt(intro_system, intro_user, temperature=0.7, max_tokens=600)
    if not intro_html.strip().startswith("<"):
        intro_html = _markdown_to_html(intro_html)
    logger.info("[Article] Intro generated")

    # ── STEP 6: Write each section ───────────────────────────────────────────
    words_per_section = max(150, target_words // max(1, len(sections)))
    kw_per_section = max(1, round(words_per_section * target_density / 100))

    if lang_pl:
        section_system = (
            "Jesteś ekspertem SEO. Piszesz sekcje artykułu w HTML. "
            "Każda sekcja: nagłówek H2 + akapity + opcjonalnie lista punktowana. "
            "Zwracaj tylko HTML (h2, p, ul/li). Bez wstępu i zakończenia."
        )
    else:
        section_system = (
            "You are an SEO expert. You write article sections in HTML. "
            "Each section: H2 heading + paragraphs + optionally a bullet list. "
            "Return only HTML (h2, p, ul/li). No intro or conclusion."
        )

    sections_html = []
    for i, section_heading in enumerate(sections):
        if lang_pl:
            section_user = (
                f"Napisz sekcję artykułu '{title}' (główny keyword: '{topic}').\n"
                f"Nagłówek H2: '{section_heading}'\n"
                f"Kontekst ({i+1}/{len(sections)}): {intent_analysis}\n"
                f"Wymagania:\n"
                f"- Około {words_per_section} słów\n"
                f"- Użyj frazy '{topic}' około {kw_per_section} raz naturalnie w tekście\n"
                f"- Zwróć HTML: <h2>{section_heading}</h2> + akapity <p> (+ ewentualnie <ul>/<li>)"
            )
        else:
            section_user = (
                f"Write a section for article '{title}' (main keyword: '{topic}').\n"
                f"H2 heading: '{section_heading}'\n"
                f"Context ({i+1}/{len(sections)}): {intent_analysis}\n"
                f"Requirements:\n"
                f"- About {words_per_section} words\n"
                f"- Use '{topic}' about {kw_per_section} times naturally\n"
                f"- Return HTML: <h2>{section_heading}</h2> + <p> paragraphs (+ optional <ul>/<li>)"
            )

        section_html = await _gpt(section_system, section_user, temperature=0.7, max_tokens=800)
        if not section_html.strip().startswith("<"):
            section_html = _markdown_to_html(section_html)
        sections_html.append(section_html)
        logger.info(f"[Article] Section {i+1}/{len(sections)} done: {section_heading[:40]}")

    # ── STEP 7: Conclusion ───────────────────────────────────────────────────
    if lang_pl:
        conclusion_system = "Jesteś ekspertem SEO. Piszesz zakończenia artykułów w HTML."
        conclusion_user = (
            f"Napisz zakończenie artykułu '{title}' (temat: '{topic}').\n"
            f"Podsumuj główne punkty: {', '.join(sections[:4])}\n"
            f"Zwróć HTML: <h2>Podsumowanie</h2> + 2-3 akapity <p>."
        )
    else:
        conclusion_system = "You are an SEO expert. You write article conclusions in HTML."
        conclusion_user = (
            f"Write a conclusion for article '{title}' (topic: '{topic}').\n"
            f"Summarize main points: {', '.join(sections[:4])}\n"
            f"Return HTML: <h2>Summary</h2> + 2-3 <p> paragraphs."
        )

    conclusion_html = await _gpt(conclusion_system, conclusion_user, temperature=0.7, max_tokens=500)
    if not conclusion_html.strip().startswith("<"):
        conclusion_html = _markdown_to_html(conclusion_html)
    logger.info("[Article] Conclusion generated")

    # ── STEP 8: FAQ ───────────────────────────────────────────────────────────
    if lang_pl:
        faq_system = "Jesteś ekspertem SEO. Tworzysz sekcje FAQ w HTML dla featured snippets."
        faq_user = (
            f"Stwórz sekcję FAQ dla artykułu o '{topic}'.\n"
            f"5 pytań i odpowiedzi związanych z tematem: {', '.join(sections[:3])}\n"
            f"Zwróć HTML: <h2>FAQ</h2> + pary <h3>Pytanie</h3><p>Odpowiedź</p>."
        )
    else:
        faq_system = "You are an SEO expert. You create FAQ sections in HTML for featured snippets."
        faq_user = (
            f"Create an FAQ section for an article about '{topic}'.\n"
            f"5 questions and answers related to: {', '.join(sections[:3])}\n"
            f"Return HTML: <h2>FAQ</h2> + pairs of <h3>Question</h3><p>Answer</p>."
        )

    faq_html = await _gpt(faq_system, faq_user, temperature=0.6, max_tokens=800)
    if not faq_html.strip().startswith("<"):
        faq_html = _markdown_to_html(faq_html)
    logger.info("[Article] FAQ generated")

    # ── STEP 9: Excerpt ───────────────────────────────────────────────────────
    if lang_pl:
        excerpt_user = (
            f"Napisz krótki opis (excerpt) artykułu '{title}' o '{topic}' "
            f"(max 2 zdania, bez HTML, do meta description)."
        )
    else:
        excerpt_user = (
            f"Write a short excerpt for article '{title}' about '{topic}' "
            f"(max 2 sentences, no HTML, for meta description)."
        )

    excerpt = await _gpt("You are an SEO copywriter.", excerpt_user, temperature=0.5, max_tokens=100)
    logger.info("[Article] Excerpt generated")

    # ── STEP 10: Assemble HTML ────────────────────────────────────────────────
    content_parts = [intro_html] + sections_html + [conclusion_html, faq_html]
    content = "\n\n".join(content_parts)

    # Inject anchor links
    content = _inject_anchors(content, anchors_info)

    # Deduplicate anchor links
    seen_hrefs: set = set()

    def remove_duplicate_link(m: re.Match) -> str:
        href = re.search(r'href=["\']([^"\']+)["\']', m.group(0))
        if not href:
            return m.group(0)
        url = href.group(1).rstrip("/")
        if url in seen_hrefs:
            text = re.sub(r"<[^>]+>", "", m.group(0))
            return text
        seen_hrefs.add(url)
        return m.group(0)

    content = re.sub(r'<a\s[^>]*?>.*?</a>', remove_duplicate_link, content, flags=re.DOTALL | re.IGNORECASE)

    logger.info(f"[Article] Done. Title: {title}, sections: {len(sections)}, chars: {len(content)}")

    return {
        "title": title,
        "content": content,
        "excerpt": excerpt,
    }


async def describe_image_and_generate(image_b64: str, topic: str) -> str:
    """Describe a static screenshot via vision, then generate a DALL-E image based on it."""
    vision_response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": f"Opisz krótko co widać na tym screenshocie strony (max 2 zdania). Kontekst: artykuł o '{topic}'."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ],
        }],
        max_tokens=200,
    )
    description = vision_response.choices[0].message.content
    prompt = f"Professional website screenshot illustration: {description}. Clean, modern design."
    return await generate_image(prompt)


async def generate_image(prompt: str) -> str:
    """Generate image with DALL-E 3 and return base64 string."""
    response = await client.images.generate(
        model="dall-e-3",
        prompt=prompt,
        n=1,
        size="1024x1024",
        response_format="b64_json",
    )
    b64 = response.data[0].b64_json
    return b64

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
import logging
import re
from typing import Optional

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
    Fetch top 3 blog URLs from SERP, parse content.
    Returns: text, avg_words, avg_density, lsi_terms
    """
    empty = {"text": "", "avg_words": 0, "avg_density": 0.0, "lsi_terms": []}
    if not dfs_login or not dfs_password:
        return empty

    try:
        from services.dataforseo_service import DataForSEOClient
        dfs = DataForSEOClient(dfs_login, dfs_password)

        serp = await dfs.serp_top10(topic, location_code, language_code)
        blog_urls = [r["url"] for r in serp if r.get("url") and _is_blog_url(r["url"])][:3]

        if not blog_urls:
            logger.warning("[SERP] No blog URLs found")
            return empty

        logger.info(f"[SERP] Parsing {len(blog_urls)} URLs: {blog_urls}")

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

        logger.info(f"[SERP] avg_words={avg_words}, avg_density={avg_density}%, LSI={lsi_terms[:8]}")
        return {
            "text": "\n\n".join(parts),
            "avg_words": avg_words,
            "avg_density": avg_density,
            "lsi_terms": lsi_terms,
        }

    except Exception as e:
        logger.warning(f"[SERP] Failed: {e}")
        return empty


def _markdown_to_html(text: str) -> str:
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


async def _gpt(system: str, user: str, temperature: float = 0.7, max_tokens: int = 2000) -> str:
    for attempt in range(3):
        try:
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
        except Exception as e:
            if attempt == 2:
                raise
            wait = 2 ** attempt
            logger.warning(f"[GPT] attempt {attempt+1} failed: {e} — retrying in {wait}s")
            await asyncio.sleep(wait)
    return ""  # unreachable


def _build_faq_schema(faq_html: str, topic: str) -> str:
    """Extract Q&A pairs from FAQ HTML and build FAQPage JSON-LD schema."""
    import json
    questions = re.findall(r'<h3[^>]*>(.*?)</h3>\s*<p>(.*?)</p>', faq_html, re.DOTALL | re.IGNORECASE)
    if not questions:
        return ""
    entities = []
    for q, a in questions[:8]:
        q_clean = re.sub(r'<[^>]+>', '', q).strip()
        a_clean = re.sub(r'<[^>]+>', '', a).strip()
        if q_clean and a_clean:
            entities.append({"@type": "Question", "name": q_clean,
                             "acceptedAnswer": {"@type": "Answer", "text": a_clean}})
    if not entities:
        return ""
    schema = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": entities}
    return f'<script type="application/ld+json">\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n</script>'


def _build_article_schema(title: str, topic: str, excerpt: str) -> str:
    """Build Article JSON-LD schema."""
    import json
    from datetime import datetime
    schema = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title,
        "description": excerpt,
        "keywords": topic,
        "datePublished": datetime.utcnow().strftime("%Y-%m-%d"),
        "inLanguage": "pl",
    }
    return f'<script type="application/ld+json">\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n</script>'


def _inject_anchors(html: str, anchors_info: str) -> str:
    if not anchors_info:
        return html
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
        target_idx = max(0, min(para_count - 1, (para_count // (len(links) + 1)) * (i + 1)))
        para = paragraphs[target_idx] if target_idx < para_count else None
        if para and link not in html:
            new_para = para[:-4] + f" {link}</p>"
            html = html.replace(para, new_para, 1)
    return html


def _inject_internal_links(html: str, published_posts: list[dict], topic: str) -> str:
    """
    Inject 2-3 internal links to already-published posts on the same domain.
    Matches anchor text from post title to existing paragraph text.
    """
    if not published_posts:
        return html

    paragraphs = re.findall(r'<p>.*?</p>', html, re.DOTALL)
    if len(paragraphs) < 4:
        return html

    injected = 0
    used_urls: set = set()

    for post in published_posts[:5]:
        if injected >= 3:
            break
        url = post.get("url") or post.get("wp_post_url", "")
        title = post.get("title", "")
        if not url or not title or url in used_urls:
            continue

        # Find a paragraph that has thematic overlap with the linked post title
        title_words = set(re.findall(r'\w{4,}', title.lower()))
        best_para = None
        best_score = 0
        for para in paragraphs[2:-2]:  # skip first 2 and last 2 paragraphs
            para_text = re.sub(r'<[^>]+>', '', para).lower()
            if 'href=' in para:  # skip paragraphs that already have links
                continue
            overlap = len(title_words & set(re.findall(r'\w{4,}', para_text)))
            if overlap > best_score:
                best_score = overlap
                best_para = para

        if best_para and best_score > 0:
            # Use first 4-5 words of title as anchor text
            anchor_words = title.split()[:5]
            anchor_text = " ".join(anchor_words)
            link = f'<a href="{url}">{anchor_text}</a>'
            new_para = best_para[:-4] + f" Więcej na ten temat: {link}.</p>"
            html = html.replace(best_para, new_para, 1)
            used_urls.add(url)
            injected += 1

    if injected:
        logger.info(f"[Article] Injected {injected} internal links")
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
    published_posts: Optional[list] = None,  # for internal linking
    domain_fingerprints: Optional[set] = None,  # for dedup check
) -> dict:
    def clean_url(url: str) -> str:
        url = url.strip()
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url
        return url

    anchors_info = f'<a href="{clean_url(client_domain)}">{anchor_text}</a>'
    if anchor_text2 and anchor_url2:
        anchors_info += f', <a href="{clean_url(anchor_url2)}">{anchor_text2}</a>'
    if anchor_text3 and anchor_url3:
        anchors_info += f', <a href="{clean_url(anchor_url3)}">{anchor_text3}</a>'

    variation = f" Kąt tematyczny: {variation_hint}." if variation_hint else ""
    lang_pl = language == "pl"

    # ── STEP 1: SERP + competitor analysis ───────────────────────────────────
    language_code = "pl" if lang_pl else "en"
    serp_data = await _fetch_serp_content(topic, dfs_login, dfs_password, location_code, language_code)
    serp_text = serp_data["text"]
    avg_words = serp_data["avg_words"] or 1200
    avg_density = serp_data["avg_density"] or 1.5
    lsi_terms = serp_data["lsi_terms"]

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
            f"Sekcje: {', '.join(sections[:3])}\n"
            f"Tytuł inny niż fraza, może być pytaniem lub poradnikiem. Tylko tytuł, bez cudzysłowów."
        )
    else:
        title_user = (
            f"Create unique SEO title for: '{topic}'\n"
            f"Sections: {', '.join(sections[:3])}\n"
            f"Different from keyword, can be a question or guide. Only title, no quotes."
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
            "Jesteś ekspertem SEO. Piszesz wstępy zoptymalizowane pod AI Overview i featured snippets.\n"
            "STRUKTURA: 1) Pierwszy akapit = BEZPOŚREDNIA odpowiedź na temat (2-3 zdania, format AI Overview). "
            "2) Drugi akapit = kontekst i tło. 3) Trzeci akapit = zapowiedź artykułu. Tagi <p>."
        )
        intro_user = (
            f"Napisz wstęp do artykułu '{title}' (keyword: '{topic}').\n"
            f"Intencja: {intent_analysis}\n"
            f"Sekcje: {', '.join(sections[:3])}\n"
            f"PIERWSZY AKAPIT = bezpośrednia odpowiedź na '{topic}' — konkretnie, jak AI Overview.\n"
            f"Użyj '{topic}' {intro_kw_count}x we wstępie.{lsi_block}\n"
            f"Tylko HTML <p>, bez nagłówków."
        )
    else:
        intro_system = (
            "You are an SEO expert writing intros optimized for AI Overview and featured snippets.\n"
            "STRUCTURE: 1) First paragraph = DIRECT answer (2-3 sentences, AI Overview format). "
            "2) Second = context. 3) Third = article preview. Use <p> tags."
        )
        intro_user = (
            f"Write intro for '{title}' (keyword: '{topic}').\n"
            f"Intent: {intent_analysis}\n"
            f"Sections: {', '.join(sections[:3])}\n"
            f"FIRST PARAGRAPH = direct answer to '{topic}' — brief, AI Overview style.\n"
            f"Use '{topic}' {intro_kw_count}x in intro.{lsi_block}\n"
            f"Only HTML <p>, no headings."
        )
    intro_html = await _gpt(intro_system, intro_user, temperature=0.7, max_tokens=600)
    if not intro_html.strip().startswith("<"):
        intro_html = _markdown_to_html(intro_html)
    logger.info("[Article] Intro done")

    # ── STEP 6: Sections ─────────────────────────────────────────────────────
    words_per_section = max(150, target_words // max(1, len(sections)))
    kw_per_section = max(1, round(words_per_section * target_density / 100))

    if lang_pl:
        section_system = (
            "Jesteś ekspertem SEO. Piszesz sekcje artykułu w HTML. "
            "Zwracaj tylko HTML (h2, p, ul/li). Bez wstępu i zakończenia."
        )
    else:
        section_system = (
            "You are an SEO expert writing article sections in HTML. "
            "Return only HTML (h2, p, ul/li). No intro or conclusion."
        )

    sections_html = []
    for i, heading in enumerate(sections):
        if lang_pl:
            section_user = (
                f"Napisz sekcję artykułu '{title}' (keyword: '{topic}').\n"
                f"H2: '{heading}'\n"
                f"Kontekst ({i+1}/{len(sections)}): {intent_analysis}\n"
                f"- ~{words_per_section} słów\n"
                f"- Użyj '{topic}' ~{kw_per_section}x naturalnie{lsi_block}\n"
                f"HTML: <h2>{heading}</h2> + <p> akapity (+ opcjonalnie <ul>/<li>)"
            )
        else:
            section_user = (
                f"Write section for '{title}' (keyword: '{topic}').\n"
                f"H2: '{heading}'\n"
                f"Context ({i+1}/{len(sections)}): {intent_analysis}\n"
                f"- ~{words_per_section} words\n"
                f"- Use '{topic}' ~{kw_per_section}x naturally{lsi_block}\n"
                f"HTML: <h2>{heading}</h2> + <p> paragraphs (+ optional <ul>/<li>)"
            )
        sec_html = await _gpt(section_system, section_user, temperature=0.7, max_tokens=900)
        if not sec_html.strip().startswith("<"):
            sec_html = _markdown_to_html(sec_html)
        sections_html.append(sec_html)
        logger.info(f"[Article] Section {i+1}/{len(sections)}: {heading[:40]}")

    # ── STEP 7: Conclusion ────────────────────────────────────────────────────
    if lang_pl:
        conclusion_user = (
            f"Napisz zakończenie artykułu '{title}' (temat: '{topic}').\n"
            f"Podsumuj: {', '.join(sections[:4])}\n"
            f"HTML: <h2>Podsumowanie</h2> + 2-3 <p>."
        )
    else:
        conclusion_user = (
            f"Write conclusion for '{title}' (topic: '{topic}').\n"
            f"Summarize: {', '.join(sections[:4])}\n"
            f"HTML: <h2>Summary</h2> + 2-3 <p>."
        )
    conclusion_html = await _gpt(
        "Jesteś ekspertem SEO." if lang_pl else "You are an SEO expert.",
        conclusion_user, temperature=0.7, max_tokens=500
    )
    if not conclusion_html.strip().startswith("<"):
        conclusion_html = _markdown_to_html(conclusion_html)

    # ── STEP 8: FAQ ───────────────────────────────────────────────────────────
    if lang_pl:
        faq_user = (
            f"Stwórz FAQ dla artykułu o '{topic}'.\n"
            f"7 pytań i odpowiedzi na temat: {', '.join(sections[:4])}\n"
            f"Pytania powinny być dokładnie tym co ludzie wpisują w Google.\n"
            f"HTML: <h2>FAQ — najczęstsze pytania</h2> + pary <h3>Pytanie?</h3><p>Odpowiedź.</p>"
        )
    else:
        faq_user = (
            f"Create FAQ for article about '{topic}'.\n"
            f"7 questions and answers about: {', '.join(sections[:4])}\n"
            f"Questions should match actual Google searches.\n"
            f"HTML: <h2>FAQ</h2> + pairs <h3>Question?</h3><p>Answer.</p>"
        )
    faq_html = await _gpt(
        "Jesteś ekspertem SEO. Tworzysz FAQ dla featured snippets i AI Overview." if lang_pl
        else "You are an SEO expert creating FAQ for featured snippets and AI Overview.",
        faq_user, temperature=0.6, max_tokens=1000
    )
    if not faq_html.strip().startswith("<"):
        faq_html = _markdown_to_html(faq_html)

    # ── STEP 9: Excerpt ───────────────────────────────────────────────────────
    if lang_pl:
        excerpt_user = f"Meta description dla artykułu '{title}' o '{topic}'. Max 155 znaków, bez HTML, zawiera keyword."
    else:
        excerpt_user = f"Meta description for article '{title}' about '{topic}'. Max 155 chars, no HTML, includes keyword."
    excerpt = await _gpt(
        "Jesteś SEO copywriterem." if lang_pl else "You are an SEO copywriter.",
        excerpt_user, temperature=0.5, max_tokens=80
    )
    logger.info("[Article] Excerpt done")

    # ── STEP 10: Assemble ─────────────────────────────────────────────────────
    # H1 at top (some WP themes don't add it automatically)
    h1 = f"<h1>{title}</h1>"

    content_parts = [h1, intro_html] + sections_html + [conclusion_html, faq_html]
    content = "\n\n".join(content_parts)

    # Inject external anchor links
    content = _inject_anchors(content, anchors_info)

    # Inject internal links to already-published posts on this domain
    if published_posts:
        content = _inject_internal_links(content, published_posts, topic)

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
    article_schema = _build_article_schema(title, topic, excerpt)
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

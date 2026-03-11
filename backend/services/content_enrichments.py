"""
Content Enrichments — losowe unikalne elementy wstrzykiwane do artykułów PBN.

Każdy artykuł losuje 2-3 elementy z puli 8 opcji:
1. expert_quote      — cytat fikcyjnego eksperta branżowego (E-E-A-T)
2. key_takeaways     — box "Kluczowe wnioski" na górze (AI Overview)
3. toc               — spis treści z linkami do H2 (sitelinks)
4. pro_tip           — 1-2 boxy Pro Tip wewnątrz sekcji
5. comparison_table  — tabela porównawcza opcji/metod
6. checklist         — interaktywna lista kontrolna
7. stats_block       — blok z danymi statystycznymi
8. update_box        — box "Artykuł zaktualizowany" (freshness signal)
"""
import random
import re
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ── Personas eksperckie — różne branże ────────────────────────────────────────
_EXPERT_PERSONAS_PL = [
    ("dr Marek Wiśniewski", "ekspert z 18-letnim doświadczeniem w branży"),
    ("mgr Agnieszka Kowalska", "specjalistka z wieloletnią praktyką"),
    ("Piotr Zawadzki", "konsultant i praktyk z 20-letnim stażem"),
    ("prof. Anna Nowak", "badaczka i ekspertka w tej dziedzinie"),
    ("Tomasz Jabłoński", "certyfikowany specjalista i doradca"),
    ("Katarzyna Wróbel", "ekspertka i autorka publikacji branżowych"),
    ("Michał Zieliński", "praktyk z doświadczeniem w ponad 200 projektach"),
    ("Barbara Kamińska", "konsultantka i trenerka branżowa"),
]

_EXPERT_PERSONAS_EN = [
    ("Dr. Michael Stevens", "expert with 18 years of industry experience"),
    ("Sarah Johnson", "specialist with extensive hands-on experience"),
    ("Robert Clarke", "certified consultant with 20+ years in the field"),
    ("Prof. Emily Watson", "researcher and subject matter expert"),
    ("David Miller", "certified specialist and industry advisor"),
    ("Jennifer Brown", "expert and author of professional publications"),
    ("James Wilson", "practitioner with experience in 200+ projects"),
    ("Laura Thompson", "industry consultant and trainer"),
]

# ── Stałe stylu CSS inline (nie wymaga zewnętrznego CSS) ──────────────────────
_STYLE_TAKEAWAYS = (
    'style="background:#f0f7ff;border-left:4px solid #1a73e8;padding:16px 20px;'
    'margin:24px 0;border-radius:0 8px 8px 0;"'
)
_STYLE_PRO_TIP = (
    'style="background:#fff8e1;border-left:4px solid #f9a825;padding:14px 18px;'
    'margin:20px 0;border-radius:0 8px 8px 0;"'
)
_STYLE_BLOCKQUOTE = (
    'style="background:#f8f9fa;border-left:4px solid #34a853;padding:16px 20px;'
    'margin:24px 0;font-style:italic;border-radius:0 8px 8px 0;"'
)
_STYLE_UPDATE = (
    'style="background:#e8f5e9;border:1px solid #a5d6a7;padding:10px 16px;'
    'margin:0 0 24px 0;border-radius:6px;font-size:0.9em;color:#2e7d32;"'
)
_STYLE_STATS = (
    'style="background:#fafafa;border:1px solid #e0e0e0;padding:20px;'
    'margin:24px 0;border-radius:8px;"'
)
_STYLE_TABLE = (
    'style="width:100%;border-collapse:collapse;margin:20px 0;"'
)
_STYLE_TH = 'style="background:#1a73e8;color:#fff;padding:10px 14px;text-align:left;"'
_STYLE_TD = 'style="padding:10px 14px;border-bottom:1px solid #e0e0e0;"'
_STYLE_TR_ALT = 'style="background:#f8f9fa;"'
_STYLE_CHECKLIST = (
    'style="background:#fff;border:1px solid #e0e0e0;padding:20px;'
    'margin:24px 0;border-radius:8px;"'
)


# ── Generatory elementów (pure HTML, bez zewnętrznych zależności) ──────────────

def _build_toc(sections: list[str], lang_pl: bool) -> str:
    """Spis treści z anchor linkami do H2."""
    heading = "Spis treści" if lang_pl else "Table of Contents"
    items = ""
    for i, section in enumerate(sections):
        anchor = re.sub(r"[^a-z0-9]+", "-", section.lower().strip())[:50]
        items += f'<li><a href="#{anchor}" style="color:#1a73e8;text-decoration:none;">{section}</a></li>\n'
    return (
        f'<nav {_STYLE_STATS}>\n'
        f'<strong style="display:block;margin-bottom:10px;">{heading}</strong>\n'
        f'<ol style="margin:0;padding-left:20px;">\n{items}</ol>\n'
        f'</nav>\n'
    )


def _add_toc_anchors(content: str, sections: list[str]) -> str:
    """Dodaje id="" do tagów H2 odpowiadających sekcjom."""
    for section in sections:
        anchor = re.sub(r"[^a-z0-9]+", "-", section.lower().strip())[:50]
        escaped = re.escape(section)
        content = re.sub(
            rf'<h2>({escaped})</h2>',
            rf'<h2 id="{anchor}">\1</h2>',
            content,
        )
    return content


def _build_key_takeaways(points: list[str], lang_pl: bool) -> str:
    """Box 'Kluczowe wnioski' — 3-4 punkty."""
    heading = "Kluczowe wnioski" if lang_pl else "Key Takeaways"
    sub = "Po przeczytaniu tego artykułu dowiesz się:" if lang_pl else "After reading this article you will know:"
    items = "".join(f"<li>{p}</li>\n" for p in points)
    return (
        f'<div {_STYLE_TAKEAWAYS}>\n'
        f'<strong style="font-size:1.05em;">✓ {heading}</strong>\n'
        f'<p style="margin:8px 0 4px;color:#555;">{sub}</p>\n'
        f'<ul style="margin:0;padding-left:20px;">\n{items}</ul>\n'
        f'</div>\n'
    )


def _build_expert_quote(quote: str, name: str, role: str) -> str:
    """Cytat eksperta w blockquote."""
    return (
        f'<blockquote {_STYLE_BLOCKQUOTE}>\n'
        f'<p style="margin:0 0 10px;">"{quote}"</p>\n'
        f'<footer style="font-size:0.9em;font-style:normal;color:#555;">'
        f'— <strong>{name}</strong>, {role}</footer>\n'
        f'</blockquote>\n'
    )


def _build_pro_tip(tip: str, lang_pl: bool) -> str:
    """Box Pro Tip."""
    label = "💡 Pro Tip" if lang_pl else "💡 Pro Tip"
    return (
        f'<div {_STYLE_PRO_TIP}>\n'
        f'<strong>{label}:</strong> {tip}\n'
        f'</div>\n'
    )


def _build_stats_block(stats: list[tuple[str, str]], lang_pl: bool) -> str:
    """Blok ze statystykami — lista (liczba, opis)."""
    heading = "📊 Kluczowe dane" if lang_pl else "📊 Key Statistics"
    rows = ""
    for stat, desc in stats:
        rows += (
            f'<div style="display:flex;align-items:baseline;gap:12px;margin:8px 0;">'
            f'<span style="font-size:1.6em;font-weight:bold;color:#1a73e8;">{stat}</span>'
            f'<span style="color:#555;">{desc}</span>'
            f'</div>\n'
        )
    return (
        f'<div {_STYLE_STATS}>\n'
        f'<strong style="display:block;margin-bottom:12px;">{heading}</strong>\n'
        f'{rows}'
        f'</div>\n'
    )


def _build_comparison_table(rows: list[dict], lang_pl: bool) -> str:
    """Tabela porównawcza — kolumny: Opcja, Zalety, Wady, Ocena."""
    h_option = "Opcja / Metoda" if lang_pl else "Option / Method"
    h_pros = "Zalety" if lang_pl else "Pros"
    h_cons = "Wady" if lang_pl else "Cons"
    h_rating = "Ocena" if lang_pl else "Rating"
    heading = "Porównanie opcji" if lang_pl else "Options Comparison"

    header = (
        f'<tr>'
        f'<th {_STYLE_TH}>{h_option}</th>'
        f'<th {_STYLE_TH}>{h_pros}</th>'
        f'<th {_STYLE_TH}>{h_cons}</th>'
        f'<th {_STYLE_TH}>{h_rating}</th>'
        f'</tr>\n'
    )
    body = ""
    for i, row in enumerate(rows):
        alt = f' {_STYLE_TR_ALT}' if i % 2 == 1 else ""
        stars = "★" * row.get("rating", 4) + "☆" * (5 - row.get("rating", 4))
        body += (
            f'<tr{alt}>'
            f'<td {_STYLE_TD}><strong>{row["name"]}</strong></td>'
            f'<td {_STYLE_TD}>{row["pros"]}</td>'
            f'<td {_STYLE_TD}>{row["cons"]}</td>'
            f'<td {_STYLE_TD}>{stars}</td>'
            f'</tr>\n'
        )
    return (
        f'<h3>{heading}</h3>\n'
        f'<div style="overflow-x:auto;">'
        f'<table {_STYLE_TABLE}><thead>{header}</thead><tbody>{body}</tbody></table>'
        f'</div>\n'
    )


def _build_checklist(items: list[str], title: str) -> str:
    """Checklist z CSS checkboxami."""
    checks = ""
    for item in items:
        uid = f"chk_{abs(hash(item)) % 99999}"
        checks += (
            f'<li style="list-style:none;margin:6px 0;">'
            f'<label style="display:flex;align-items:flex-start;gap:8px;cursor:pointer;">'
            f'<input type="checkbox" id="{uid}" style="margin-top:3px;width:16px;height:16px;flex-shrink:0;">'
            f'<span>{item}</span>'
            f'</label></li>\n'
        )
    return (
        f'<div {_STYLE_CHECKLIST}>\n'
        f'<strong style="display:block;margin-bottom:12px;">✅ {title}</strong>\n'
        f'<ul style="margin:0;padding:0;">\n{checks}</ul>\n'
        f'</div>\n'
    )


def _build_update_box(lang_pl: bool) -> str:
    """Box z datą aktualizacji artykułu."""
    # losowa data w przedziale ostatnich 30 dni
    days_ago = random.randint(0, 30)
    update_date = (datetime.now() - timedelta(days=days_ago)).strftime("%d.%m.%Y")
    if lang_pl:
        text = f"🔄 Artykuł zaktualizowany: <strong>{update_date}</strong> — treść zweryfikowana i uzupełniona o najnowsze informacje."
    else:
        text = f"🔄 Last updated: <strong>{update_date}</strong> — content verified and updated with the latest information."
    return f'<div {_STYLE_UPDATE}>{text}</div>\n'


# ── GPT-generowane dane dla elementów ──────────────────────────────────────────

async def _gpt_enrichment(client, topic: str, element: str, lang_pl: bool) -> dict:
    """Pobierz dane do elementu z GPT (jeden call na element)."""
    from openai import AsyncOpenAI
    try:
        if element == "expert_quote":
            persona = random.choice(_EXPERT_PERSONAS_PL if lang_pl else _EXPERT_PERSONAS_EN)
            if lang_pl:
                prompt = (
                    f"Napisz krótki cytat eksperta (2-3 zdania) o temacie: '{topic}'. "
                    f"Cytat powinien być konkretny, praktyczny, zawierać radę lub spostrzeżenie. "
                    f"Tylko treść cytatu, bez cudzysłowów, bez imienia."
                )
            else:
                prompt = (
                    f"Write a short expert quote (2-3 sentences) about: '{topic}'. "
                    f"Specific, practical, with advice or insight. "
                    f"Only the quote text, no quotation marks, no name."
                )
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.8, max_tokens=120,
            )
            quote = resp.choices[0].message.content.strip().strip('"\'')
            return {"quote": quote, "name": persona[0], "role": persona[1]}

        elif element == "key_takeaways":
            if lang_pl:
                prompt = (
                    f"Podaj 4 kluczowe wnioski / rzeczy które czytelnik dowie się z artykułu o '{topic}'. "
                    f"Każdy punkt: 1 krótkie zdanie (max 15 słów). Format: jedna linia = jeden punkt, bez numerów."
                )
            else:
                prompt = (
                    f"Give 4 key takeaways a reader will learn from an article about '{topic}'. "
                    f"Each point: 1 short sentence (max 15 words). Format: one line = one point, no numbers."
                )
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5, max_tokens=150,
            )
            points = [p.strip("- •*").strip() for p in resp.choices[0].message.content.strip().split("\n") if p.strip()][:4]
            return {"points": points}

        elif element == "pro_tip":
            if lang_pl:
                prompt = (
                    f"Podaj 2 praktyczne Pro Tipy związane z '{topic}'. "
                    f"Każdy tip: 1-2 zdania, konkretna rada, coś czego większość nie wie. "
                    f"Format: każdy tip w osobnej linii, bez numerów."
                )
            else:
                prompt = (
                    f"Give 2 practical Pro Tips related to '{topic}'. "
                    f"Each tip: 1-2 sentences, specific advice, something most people don't know. "
                    f"Format: each tip on a separate line, no numbers."
                )
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7, max_tokens=150,
            )
            tips = [t.strip("- •*").strip() for t in resp.choices[0].message.content.strip().split("\n") if t.strip()][:2]
            return {"tips": tips}

        elif element == "source_citations":
            # Real source citations are passed from SERP data, not GPT-generated
            # This element is handled specially in enrich_article() — skip GPT call
            return {}

        elif element == "comparison_table":
            if lang_pl:
                prompt = (
                    f"Stwórz tabelę porównawczą 3 opcji/metod związanych z '{topic}'. "
                    f"Format JSON: lista obiektów {{\"name\": str, \"pros\": str, \"cons\": str, \"rating\": int(1-5)}}. "
                    f"Tylko JSON, bez markdown."
                )
            else:
                prompt = (
                    f"Create a comparison table of 3 options/methods related to '{topic}'. "
                    f"JSON format: list of objects {{\"name\": str, \"pros\": str, \"cons\": str, \"rating\": int(1-5)}}. "
                    f"JSON only, no markdown."
                )
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5, max_tokens=300,
                response_format={"type": "json_object"},
            )
            import json
            raw = json.loads(resp.choices[0].message.content)
            rows = raw if isinstance(raw, list) else raw.get("rows", raw.get("options", raw.get("comparison", [])))
            return {"rows": rows[:3]}

        elif element == "checklist":
            if lang_pl:
                prompt = (
                    f"Stwórz praktyczną checklistę związaną z '{topic}' (6-8 punktów). "
                    f"Tytuł checklisty i punkty. "
                    f"Format JSON: {{\"title\": str, \"items\": [str, ...]}}. Tylko JSON."
                )
            else:
                prompt = (
                    f"Create a practical checklist related to '{topic}' (6-8 items). "
                    f"Include a checklist title and items. "
                    f"JSON format: {{\"title\": str, \"items\": [str, ...]}}. JSON only."
                )
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5, max_tokens=250,
                response_format={"type": "json_object"},
            )
            import json
            raw = json.loads(resp.choices[0].message.content)
            return {"title": raw.get("title", "Checklist"), "items": raw.get("items", [])[:8]}

    except Exception as e:
        logger.warning(f"[Enrichment] GPT call failed for {element}: {e}")
        return {}

    return {}


# ── Główna funkcja ─────────────────────────────────────────────────────────────

ALL_ELEMENTS = [
    "expert_quote",
    "key_takeaways",
    "toc",
    "pro_tip",
    "comparison_table",
    "checklist",
    "source_citations",
    "update_box",
]


def _build_source_citations(urls: list[str], lang_pl: bool) -> str:
    """Build a 'Sources' block from real SERP URLs."""
    if not urls:
        return ""
    heading = "Źródła i dodatkowe informacje" if lang_pl else "Sources & Further Reading"
    items = "".join(
        f'<li><a href="{url}" rel="nofollow noopener" target="_blank">{url}</a></li>\n'
        for url in urls[:3]
    )
    return (
        f'<div {_STYLE_STATS}>\n'
        f'<strong style="display:block;margin-bottom:10px;">📚 {heading}</strong>\n'
        f'<ul style="margin:0;padding-left:20px;">\n{items}</ul>\n'
        f'</div>\n'
    )


async def enrich_article(
    content: str,
    topic: str,
    sections: list[str],
    lang_pl: bool,
    openai_client,
    n_elements: int = None,
    serp_urls: list = None,
) -> str:
    """
    Losuje 2-3 elementy z puli i wstrzykuje je do artykułu.
    Zwraca wzbogacony HTML.
    """
    if n_elements is None:
        n_elements = random.randint(2, 3)

    chosen = random.sample(ALL_ELEMENTS, min(n_elements, len(ALL_ELEMENTS)))
    logger.info(f"[Enrichment] Chosen for '{topic}': {chosen}")

    # Fetch GPT data for elements that need it (in parallel)
    gpt_elements = [e for e in chosen if e not in ("toc", "update_box", "source_citations")]
    gpt_tasks = {e: _gpt_enrichment(openai_client, topic, e, lang_pl) for e in gpt_elements}
    gpt_results = {}
    if gpt_tasks:
        results = await asyncio.gather(*gpt_tasks.values(), return_exceptions=True)
        for key, result in zip(gpt_tasks.keys(), results):
            if isinstance(result, Exception):
                logger.warning(f"[Enrichment] {key} failed: {result}")
                gpt_results[key] = {}
            else:
                gpt_results[key] = result

    # ── TOC — wstaw po H1, dodaj anchory do H2 ────────────────────────────────
    if "toc" in chosen and sections:
        toc_html = _build_toc(sections, lang_pl)
        content = _add_toc_anchors(content, sections)
        # Wstaw po pierwszym </h1> lub po pierwszym </p>
        if "</h1>" in content:
            content = content.replace("</h1>", f"</h1>\n\n{toc_html}", 1)
        else:
            first_p = content.find("</p>")
            if first_p != -1:
                content = content[:first_p + 4] + f"\n\n{toc_html}" + content[first_p + 4:]

    # ── Key Takeaways — wstaw po TOC lub po H1 ────────────────────────────────
    if "key_takeaways" in chosen:
        data = gpt_results.get("key_takeaways", {})
        points = data.get("points", [])
        if points:
            box = _build_key_takeaways(points, lang_pl)
            # Wstaw po pierwszym </nav> (TOC) lub po pierwszym </h1>
            if "</nav>" in content:
                content = content.replace("</nav>", f"</nav>\n\n{box}", 1)
            elif "</h1>" in content:
                content = content.replace("</h1>", f"</h1>\n\n{box}", 1)

    # ── Update Box — wstaw przed pierwszą sekcją H2 ───────────────────────────
    if "update_box" in chosen:
        box = _build_update_box(lang_pl)
        h2_pos = content.find("<h2")
        if h2_pos != -1:
            content = content[:h2_pos] + box + content[h2_pos:]

    # ── Expert Quote — wstaw po 2. lub 3. sekcji ──────────────────────────────
    if "expert_quote" in chosen:
        data = gpt_results.get("expert_quote", {})
        if data.get("quote"):
            quote_html = _build_expert_quote(data["quote"], data["name"], data["role"])
            # Znajdź 2. lub 3. </h2> i wstaw po nim
            h2_matches = [m.start() for m in re.finditer(r"</h2>", content)]
            insert_after = h2_matches[min(2, len(h2_matches) - 1)] + 5 if h2_matches else -1
            if insert_after > 0:
                # Przesuń do końca paragrafu po tym H2
                para_end = content.find("</p>", insert_after)
                if para_end != -1:
                    content = content[:para_end + 4] + f"\n\n{quote_html}" + content[para_end + 4:]

    # ── Stats Block — wstaw po 1. sekcji ──────────────────────────────────────
    if "stats_block" in chosen:
        data = gpt_results.get("stats_block", {})
        stats = data.get("stats", [])
        if stats:
            stats_html = _build_stats_block(stats, lang_pl)
            h2_matches = [m.start() for m in re.finditer(r"</h2>", content)]
            insert_after_idx = min(1, len(h2_matches) - 1) if h2_matches else -1
            if insert_after_idx >= 0:
                para_end = content.find("</p>", h2_matches[insert_after_idx])
                if para_end != -1:
                    content = content[:para_end + 4] + f"\n\n{stats_html}" + content[para_end + 4:]

    # ── Pro Tips — wstaw po różnych sekcjach ──────────────────────────────────
    if "pro_tip" in chosen:
        data = gpt_results.get("pro_tip", {})
        tips = data.get("tips", [])
        h2_matches = [m.start() for m in re.finditer(r"</h2>", content)]
        for idx, tip in enumerate(tips[:2]):
            tip_html = _build_pro_tip(tip, lang_pl)
            section_idx = min(3 + idx * 2, len(h2_matches) - 1) if h2_matches else -1
            if section_idx >= 0:
                para_end = content.find("</p>", h2_matches[section_idx])
                if para_end != -1:
                    content = content[:para_end + 4] + f"\n\n{tip_html}" + content[para_end + 4:]

    # ── Comparison Table — wstaw przed zakończeniem ───────────────────────────
    if "comparison_table" in chosen:
        data = gpt_results.get("comparison_table", {})
        rows = data.get("rows", [])
        if rows:
            table_html = _build_comparison_table(rows, lang_pl)
            # Wstaw przed ostatnim H2 (zakończenie/podsumowanie)
            h2_positions = [(m.start(), m.end()) for m in re.finditer(r"<h2[^>]*>", content)]
            if len(h2_positions) >= 2:
                last_h2_start = h2_positions[-1][0]
                content = content[:last_h2_start] + table_html + "\n\n" + content[last_h2_start:]

    # ── Checklist — wstaw gdzieś w środku ─────────────────────────────────────
    if "checklist" in chosen:
        data = gpt_results.get("checklist", {})
        items = data.get("items", [])
        title = data.get("title", "Checklist")
        if items:
            check_html = _build_checklist(items, title)
            h2_matches = [m.start() for m in re.finditer(r"</h2>", content)]
            mid_idx = len(h2_matches) // 2 if h2_matches else -1
            if mid_idx >= 0:
                para_end = content.find("</p>", h2_matches[mid_idx])
                if para_end != -1:
                    content = content[:para_end + 4] + f"\n\n{check_html}" + content[para_end + 4:]

    # ── Source Citations — wstaw na końcu (real SERP URLs, nie fikcyjne dane) ─
    if "source_citations" in chosen and serp_urls:
        citations_html = _build_source_citations(serp_urls, lang_pl)
        if citations_html:
            content = content + f"\n\n{citations_html}"

    logger.info(f"[Enrichment] Done for '{topic}' — {len(chosen)} elements injected")
    return content

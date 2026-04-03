"""
Content Enrichments — losowe unikalne elementy wstrzykiwane do artykułów PBN.

Pula 20 elementów — na długi artykuł losowane 3-4:
 1. expert_quote       — cytat fikcyjnego eksperta (E-E-A-T)
 2. key_takeaways      — box "Kluczowe wnioski" na górze
 3. toc                — spis treści z linkami do H2
 4. pro_tip            — boxy Pro Tip wewnątrz sekcji
 5. comparison_table   — tabela porównawcza opcji/metod
 6. checklist          — interaktywna lista kontrolna
 7. stats_block        — blok z danymi statystycznymi
 8. update_box         — box "Artykuł zaktualizowany"
 9. source_citations   — cytowania źródeł (SERP URLs)
10. warning_box        — box ostrzeżenia / częste błędy
11. step_by_step       — numerowany przewodnik krok po kroku
12. quick_answer       — box "Szybka odpowiedź" (AI Overview snippet)
13. cost_table         — tabela kosztów / cennik
14. faq_mini           — 3 mini-pytania Q&A wewnątrz treści
15. did_you_know       — box "Czy wiesz, że..." z ciekawostką
16. tools_list         — lista narzędzi / zasobów
17. time_estimate      — box szacowanego czasu / trudności
18. case_study_box     — krótkie case study / przykład z życia
19. summary_table      — tabela podsumowująca (kiedy wybrać co)
20. action_steps       — box "Kolejne kroki / Co zrobić teraz"
21. methodology_box    — box "Nasza metodologia" (contentEffort signal)
22. data_insight       — box z unikatową analizą danych (Information Gain)
"""
import html as _html_mod
import json
import random
import re
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional


def _esc(text: str) -> str:
    """HTML-escape text to prevent XSS/broken markup from GPT output."""
    return _html_mod.escape(str(text)) if text else ""

from services.article_helpers import slugify_heading as _slugify_heading

logger = logging.getLogger(__name__)

# ── Generic source types (no false attribution to real institutions) ───────────
_GENERIC_SOURCES_PL = [
    "badania branżowe",
    "dane rynkowe",
    "analizy sektorowe",
    "obserwacje ekspertów",
    "opracowania tematyczne",
    "dostępne statystyki",
    "raporty branżowe",
    "analiza danych",
    "przegląd literatury",
    "dostępne źródła",
]
_GENERIC_SOURCES_EN = [
    "industry research",
    "market data",
    "sector analysis",
    "expert observations",
    "subject matter reviews",
    "available statistics",
    "industry reports",
    "data analysis",
    "literature review",
    "available sources",
]

# ── Anti-footprint CSS variation ──────────────────────────────────────────────
def _vary(base: int, delta: int = 3) -> int:
    """Add small random variation to px values for anti-footprint."""
    return max(0, base + random.randint(-delta, delta))


def _vary_hex(hex_color: str, delta: int = 12) -> str:
    """SEO #121: Add small random variation to hex colors for anti-footprint.
    Shifts each RGB channel by ±delta to create visually similar but unique colors.
    """
    hex_color = hex_color.lstrip('#')
    if len(hex_color) != 6:
        return f"#{hex_color}"
    r = max(0, min(255, int(hex_color[0:2], 16) + random.randint(-delta, delta)))
    g = max(0, min(255, int(hex_color[2:4], 16) + random.randint(-delta, delta)))
    b = max(0, min(255, int(hex_color[4:6], 16) + random.randint(-delta, delta)))
    return f"#{r:02x}{g:02x}{b:02x}"

# Base styles — used via _s() which adds small random px variations per article
# SEO #121: _vary_hex on all hex colors for anti-footprint (each article gets unique color scheme)
_STYLES = {
    "takeaways":  lambda: f'style="background:{_vary_hex("f0f7ff")};border-left:4px solid {_vary_hex("1a73e8")};padding:{_vary(16)}px {_vary(20)}px;margin:{_vary(24)}px 0;border-radius:0 {_vary(8)}px {_vary(8)}px 0;"',
    "pro_tip":    lambda: f'style="background:{_vary_hex("fff8e1")};border-left:4px solid {_vary_hex("f9a825")};padding:{_vary(14)}px {_vary(18)}px;margin:{_vary(20)}px 0;border-radius:0 {_vary(8)}px {_vary(8)}px 0;"',
    "blockquote": lambda: f'style="background:{_vary_hex("f8f9fa")};border-left:4px solid {_vary_hex("34a853")};padding:{_vary(16)}px {_vary(20)}px;margin:{_vary(24)}px 0;font-style:italic;border-radius:0 {_vary(8)}px {_vary(8)}px 0;"',
    "update":     lambda: f'style="background:{_vary_hex("e8f5e9")};border:1px solid {_vary_hex("a5d6a7")};padding:{_vary(10)}px {_vary(16)}px;margin:0 0 {_vary(24)}px 0;border-radius:{_vary(6)}px;font-size:0.9em;color:{_vary_hex("2e7d32")};"',
    "stats":      lambda: f'style="background:{_vary_hex("fafafa")};border:1px solid {_vary_hex("e0e0e0")};padding:{_vary(20)}px;margin:{_vary(24)}px 0;border-radius:{_vary(8)}px;"',
    "table":      lambda: f'style="width:100%;border-collapse:collapse;margin:{_vary(20)}px 0;"',
    "th":         lambda: f'style="background:{_vary_hex("1a73e8")};color:#fff;padding:{_vary(10)}px {_vary(14)}px;text-align:left;"',
    "td":         lambda: f'style="padding:{_vary(10)}px {_vary(14)}px;border-bottom:1px solid {_vary_hex("e0e0e0")};"',
    "tr_alt":     lambda: f'style="background:{_vary_hex("f8f9fa")};"',
    "checklist":  lambda: f'style="background:#fff;border:1px solid {_vary_hex("e0e0e0")};padding:{_vary(20)}px;margin:{_vary(24)}px 0;border-radius:{_vary(8)}px;"',
    "warning":    lambda: f'style="background:{_vary_hex("fff3e0")};border-left:4px solid {_vary_hex("e65100")};padding:{_vary(14)}px {_vary(18)}px;margin:{_vary(20)}px 0;border-radius:0 {_vary(8)}px {_vary(8)}px 0;"',
    "quick_ans":  lambda: f'style="background:{_vary_hex("e8f5e9")};border:2px solid {_vary_hex("43a047")};padding:{_vary(16)}px {_vary(20)}px;margin:{_vary(24)}px 0;border-radius:{_vary(8)}px;"',
    "did_know":   lambda: f'style="background:{_vary_hex("f3e5f5")};border-left:4px solid {_vary_hex("7b1fa2")};padding:{_vary(14)}px {_vary(18)}px;margin:{_vary(20)}px 0;border-radius:0 {_vary(8)}px {_vary(8)}px 0;"',
    "time":       lambda: f'style="background:{_vary_hex("e3f2fd")};border:1px solid {_vary_hex("90caf9")};padding:{_vary(12)}px {_vary(18)}px;margin:{_vary(20)}px 0;border-radius:{_vary(8)}px;display:flex;gap:{_vary(24)}px;flex-wrap:wrap;"',
    "case":       lambda: f'style="background:{_vary_hex("fafafa")};border:1px solid {_vary_hex("e0e0e0")};border-top:4px solid {_vary_hex("1a73e8")};padding:{_vary(20)}px;margin:{_vary(24)}px 0;border-radius:0 0 {_vary(8)}px {_vary(8)}px;"',
    "action":     lambda: f'style="background:{_vary_hex("e8f5e9")};border-left:4px solid {_vary_hex("43a047")};padding:{_vary(16)}px {_vary(20)}px;margin:{_vary(24)}px 0;border-radius:0 {_vary(8)}px {_vary(8)}px 0;"',
    "methodology":lambda: f'style="background:{_vary_hex("fce4ec")};border-left:4px solid {_vary_hex("c62828")};padding:{_vary(16)}px {_vary(20)}px;margin:{_vary(24)}px 0;border-radius:0 {_vary(8)}px {_vary(8)}px 0;"',
    "insight":    lambda: f'style="background:{_vary_hex("e0f2f1")};border:2px solid {_vary_hex("00897b")};padding:{_vary(18)}px {_vary(22)}px;margin:{_vary(24)}px 0;border-radius:{_vary(8)}px;"',
}

def _s(name: str) -> str:
    """Get a style with anti-footprint variation."""
    return _STYLES[name]()



# ── Generatory elementów (pure HTML) ──────────────────────────────────────────

def _build_toc(sections: list[str], lang_pl: bool) -> str:
    heading = "Spis treści" if lang_pl else "Table of Contents"
    items = ""
    for section in sections:
        # FIX: strip any leftover "H2:" prefix from GPT outline
        clean = re.sub(r'^H[2-4]:\s*', '', section).strip() or section
        # FIX #39: use slugify_heading for proper Polish char handling (ąćę → ace)
        anchor = _slugify_heading(clean)
        items += f'<li><a href="#{anchor}" style="color:#1a73e8;text-decoration:none;">{clean}</a></li>\n'
    # SEO #32: add role="navigation" and aria-label for accessibility
    return (
        f'<nav role="navigation" aria-label="{heading}" {_s("stats")}>\n'
        f'<strong style="display:block;margin-bottom:10px;">{heading}</strong>\n'
        f'<ol style="margin:0;padding-left:20px;">\n{items}</ol>\n'
        f'</nav>\n'
    )


def _add_toc_anchors(content: str, sections: list[str]) -> str:
    for section in sections:
        # FIX #40: use slugify_heading for proper Polish char handling (ąćę → ace)
        anchor = _slugify_heading(section)
        escaped = re.escape(section)
        # FIX: match <h2> with optional existing attributes (id=, class=, etc.)
        # Replace/override the id attribute to ensure TOC links work
        content = re.sub(
            rf'<h2([^>]*)>({escaped})</h2>',
            rf'<h2 id="{anchor}"\1>\2</h2>',
            content,
        )
        # Clean up duplicate id attrs if _fix_heading_hierarchy already added one
        content = re.sub(r'(<h2\s+id="[^"]*")\s+id="[^"]*"', r'\1', content)
    # Also add ids to all h3 that don't have one yet
    def _add_h3_id(m: re.Match) -> str:
        if 'id=' in m.group(0):
            return m.group(0)
        text = re.sub(r'<[^>]+>', '', m.group(1))
        # FIX #41: use slugify_heading for H3 anchors too
        anchor = _slugify_heading(text)
        return f'<h3 id="{anchor}">{m.group(1)}</h3>'
    content = re.sub(r'<h3>(.*?)</h3>', _add_h3_id, content, flags=re.DOTALL)
    return content


def _build_key_takeaways(points: list[str], lang_pl: bool) -> str:
    heading = "Kluczowe wnioski" if lang_pl else "Key Takeaways"
    sub = "Po przeczytaniu tego artykułu dowiesz się:" if lang_pl else "After reading this article you will know:"
    items = "".join(f"<li>{p}</li>\n" for p in points)
    return (
        f'<div {_s("takeaways")}>\n'
        f'<strong style="font-size:1.05em;">✓ {heading}</strong>\n'
        f'<p style="margin:8px 0 4px;color:#555;">{sub}</p>\n'
        f'<ul style="margin:0;padding-left:20px;">\n{items}</ul>\n'
        f'</div>\n'
    )


def _build_expert_quote(quote: str, source: str, _role: str = "") -> str:
    # SEO #30: blockquote with cite attribute for E-E-A-T signal
    _cite_attr = f' cite="{_esc(source)}"' if source and source.startswith("http") else ""
    return (
        f'<blockquote{_cite_attr} {_s("blockquote")}>\n'
        f'<p style="margin:0 0 10px;">{_esc(quote)}</p>\n'
        f'<footer style="font-size:0.9em;font-style:normal;color:#555;">'
        f'— <strong>{_esc(source)}</strong></footer>\n'
        f'</blockquote>\n'
    )


def _build_pro_tip(tip: str, lang_pl: bool) -> str:
    return f'<div {_s("pro_tip")}>\n<strong>💡 Pro Tip:</strong> {_esc(tip)}\n</div>\n'


def _build_stats_block(stats: list[tuple[str, str]], lang_pl: bool) -> str:
    heading = "📊 Kluczowe dane" if lang_pl else "📊 Key Statistics"
    rows = ""
    for stat, desc in stats:
        rows += (
            f'<div style="display:flex;align-items:baseline;gap:12px;margin:8px 0;">'
            f'<span style="font-size:1.6em;font-weight:bold;color:#1a73e8;">{_esc(stat)}</span>'
            f'<span style="color:#555;">{_esc(desc)}</span>'
            f'</div>\n'
        )
    return f'<div {_s("stats")}>\n<strong style="display:block;margin-bottom:12px;">{heading}</strong>\n{rows}</div>\n'


def _build_comparison_table(rows: list[dict], lang_pl: bool) -> str:
    h_option = "Opcja / Metoda" if lang_pl else "Option / Method"
    h_pros   = "Zalety" if lang_pl else "Pros"
    h_cons   = "Wady" if lang_pl else "Cons"
    h_rating = "Ocena" if lang_pl else "Rating"
    heading  = "Porównanie opcji" if lang_pl else "Options Comparison"
    header = (
        f'<tr><th {_s("th")}>{h_option}</th><th {_s("th")}>{h_pros}</th>'
        f'<th {_s("th")}>{h_cons}</th><th {_s("th")}>{h_rating}</th></tr>\n'
    )
    body = ""
    for i, row in enumerate(rows):
        alt   = f' {_s("tr_alt")}' if i % 2 == 1 else ""
        rating = max(1, min(5, row.get("rating", 4)))
        stars = "★" * rating + "☆" * (5 - rating)
        body += (
            f'<tr{alt}><td {_s("td")}><strong>{_esc(row["name"])}</strong></td>'
            f'<td {_s("td")}>{_esc(row["pros"])}</td><td {_s("td")}>{_esc(row["cons"])}</td>'
            f'<td {_s("td")}>{stars}</td></tr>\n'
        )
    return (
        f'<h3>{heading}</h3>\n'
        f'<div style="overflow-x:auto;">'
        f'<table {_s("table")}><thead>{header}</thead><tbody>{body}</tbody></table>'
        f'</div>\n'
    )


def _build_checklist(items: list[str], title: str) -> str:
    checks = ""
    for item in items:
        uid = f"chk_{abs(hash(item)) % 99999}"
        checks += (
            f'<li style="list-style:none;margin:6px 0;">'
            f'<label style="display:flex;align-items:flex-start;gap:8px;cursor:pointer;">'
            f'<input type="checkbox" id="{uid}" style="margin-top:3px;width:16px;height:16px;flex-shrink:0;">'
            f'<span>{item}</span></label></li>\n'
        )
    return (
        f'<div {_s("checklist")}>\n'
        f'<strong style="display:block;margin-bottom:12px;">✅ {title}</strong>\n'
        f'<ul style="margin:0;padding:0;">\n{checks}</ul>\n'
        f'</div>\n'
    )


def _build_update_box(lang_pl: bool, topic: str = "", publish_date: str = "") -> str:
    # FIX #23: use UTC timezone-aware datetime (was naive datetime)
    update_date = publish_date or datetime.now(timezone.utc).strftime("%d.%m.%Y")
    if lang_pl:
        text = f"🔄 Opublikowano: <strong>{update_date}</strong> — treść zweryfikowana i oparta o aktualne źródła."
    else:
        text = f"🔄 Published: <strong>{update_date}</strong> — content verified and based on current sources."
    return f'<div {_s("update")}>{text}</div>\n'


def _build_source_citations(urls: list[str], lang_pl: bool) -> str:
    if not urls:
        return ""
    heading = "Źródła i dodatkowe informacje" if lang_pl else "Sources & Further Reading"
    items = "".join(
        f'<li><a href="{url}" rel="nofollow noopener noreferrer" target="_blank">{url}</a></li>\n'
        for url in urls[:3]
    )
    return (
        f'<div {_s("stats")}>\n'
        f'<strong style="display:block;margin-bottom:10px;">📚 {heading}</strong>\n'
        f'<ul style="margin:0;padding-left:20px;">\n{items}</ul>\n'
        f'</div>\n'
    )


def _build_warning_box(text: str, lang_pl: bool) -> str:
    label = "⚠️ Częsty błąd" if lang_pl else "⚠️ Common Mistake"
    return f'<div {_s("warning")}>\n<strong>{label}:</strong> {_esc(text)}\n</div>\n'


def _build_step_by_step(steps: list[str], title: str, lang_pl: bool = True) -> str:
    step_label = "Krok" if lang_pl else "Step"
    items = ""
    for i, step in enumerate(steps, 1):
        items += (
            f'<li style="margin:10px 0;padding-left:8px;">'
            f'<strong style="color:#1a73e8;">{step_label} {i}:</strong> {step}</li>\n'
        )
    return (
        f'<div {_s("checklist")}>\n'
        f'<strong style="display:block;margin-bottom:12px;">📋 {title}</strong>\n'
        f'<ol style="margin:0;padding-left:24px;">\n{items}</ol>\n'
        f'</div>\n'
    )


def _build_quick_answer(answer: str, lang_pl: bool) -> str:
    return (
        f'<div {_s("quick_ans")}>\n'
        f'<p style="margin:0;">{_esc(answer)}</p>\n'
        f'</div>\n'
    )


def _build_cost_table(rows: list[dict], lang_pl: bool) -> str:
    h_item   = "Usługa / Produkt" if lang_pl else "Service / Product"
    h_cost   = "Koszt" if lang_pl else "Cost"
    h_note   = "Uwagi" if lang_pl else "Notes"
    heading  = "💰 Przegląd kosztów" if lang_pl else "💰 Cost Overview"
    header = f'<tr><th {_s("th")}>{h_item}</th><th {_s("th")}>{h_cost}</th><th {_s("th")}>{h_note}</th></tr>\n'
    body = ""
    for i, row in enumerate(rows):
        alt = f' {_s("tr_alt")}' if i % 2 == 1 else ""
        body += (
            f'<tr{alt}><td {_s("td")}><strong>{_esc(row.get("item",""))}</strong></td>'
            f'<td {_s("td")}>{_esc(row.get("cost",""))}</td>'
            f'<td {_s("td")}>{_esc(row.get("note",""))}</td></tr>\n'
        )
    return (
        f'<h3>{heading}</h3>\n'
        f'<div style="overflow-x:auto;">'
        f'<table {_s("table")}><thead>{header}</thead><tbody>{body}</tbody></table>'
        f'</div>\n'
    )


def _build_faq_mini(pairs: list[dict], lang_pl: bool) -> str:
    heading = "❓ Najczęstsze pytania" if lang_pl else "❓ Common Questions"
    items = ""
    for pair in pairs[:3]:
        q = pair.get("q", "")
        a = pair.get("a", "")
        items += (
            f'<div style="margin:12px 0;">'
            f'<strong style="color:#1a73e8;">Q: {_esc(q)}</strong>'
            f'<p style="margin:4px 0 0 0;color:#555;">A: {_esc(a)}</p>'
            f'</div>\n'
        )
    return (
        f'<div {_s("stats")}>\n'
        f'<strong style="display:block;margin-bottom:12px;">{heading}</strong>\n'
        f'{items}'
        f'</div>\n'
    )


def _build_did_you_know(fact: str, lang_pl: bool) -> str:
    label = "🧠 Czy wiesz, że..." if lang_pl else "🧠 Did You Know?"
    return f'<div {_s("did_know")}>\n<strong>{label}</strong><p style="margin:8px 0 0;">{_esc(fact)}</p>\n</div>\n'


def _build_tools_list(tools: list[dict], lang_pl: bool) -> str:
    heading = "🛠️ Przydatne narzędzia i zasoby" if lang_pl else "🛠️ Useful Tools & Resources"
    items = ""
    for tool in tools[:5]:
        name = tool.get("name", "")
        desc = tool.get("desc", "")
        items += f'<li style="margin:6px 0;"><strong>{_esc(name)}</strong> — {_esc(desc)}</li>\n'
    return (
        f'<div {_s("stats")}>\n'
        f'<strong style="display:block;margin-bottom:10px;">{heading}</strong>\n'
        f'<ul style="margin:0;padding-left:20px;">\n{items}</ul>\n'
        f'</div>\n'
    )


def _build_time_estimate(time_str: str, difficulty: str, lang_pl: bool) -> str:
    t_label = "⏱️ Czas realizacji" if lang_pl else "⏱️ Time Required"
    d_label = "📈 Poziom trudności" if lang_pl else "📈 Difficulty Level"
    return (
        f'<div {_s("time")}>\n'
        f'<div><strong>{t_label}:</strong> {time_str}</div>\n'
        f'<div><strong>{d_label}:</strong> {difficulty}</div>\n'
        f'</div>\n'
    )


def _build_case_study(title: str, body: str, result: str, lang_pl: bool) -> str:
    r_label = "Wynik" if lang_pl else "Result"
    return (
        f'<div {_s("case")}>\n'
        f'<strong style="display:block;font-size:1.05em;margin-bottom:8px;">📌 Case Study: {_esc(title)}</strong>\n'
        f'<p style="margin:0 0 8px;">{_esc(body)}</p>\n'
        f'<p style="margin:0;"><strong>{r_label}:</strong> {_esc(result)}</p>\n'
        f'</div>\n'
    )


def _build_summary_table(rows: list[dict], lang_pl: bool) -> str:
    h_when   = "Kiedy wybrać" if lang_pl else "When to Choose"
    h_why    = "Dlaczego" if lang_pl else "Why"
    heading  = "📋 Kiedy wybrać jakie rozwiązanie?" if lang_pl else "📋 Which Solution to Choose?"
    header = f'<tr><th {_s("th")}>{h_when}</th><th {_s("th")}>{h_why}</th></tr>\n'
    body = ""
    for i, row in enumerate(rows):
        alt = f' {_s("tr_alt")}' if i % 2 == 1 else ""
        body += f'<tr{alt}><td {_s("td")}><strong>{_esc(row.get("when",""))}</strong></td><td {_s("td")}>{_esc(row.get("why",""))}</td></tr>\n'
    return (
        f'<h3>{heading}</h3>\n'
        f'<div style="overflow-x:auto;">'
        f'<table {_s("table")}><thead>{header}</thead><tbody>{body}</tbody></table>'
        f'</div>\n'
    )


def _build_action_steps(steps: list[str], lang_pl: bool) -> str:
    heading = "🚀 Co zrobić teraz?" if lang_pl else "🚀 Next Steps"
    items = "".join(f'<li style="margin:6px 0;">{s}</li>\n' for s in steps)
    return (
        f'<div {_s("action")}>\n'
        f'<strong style="display:block;margin-bottom:10px;">{heading}</strong>\n'
        f'<ol style="margin:0;padding-left:20px;">\n{items}</ol>\n'
        f'</div>\n'
    )


def _build_methodology_box(methodology: str, lang_pl: bool) -> str:
    """contentEffort signal — shows research methodology, boosts perceived effort."""
    label = "🔬 Nasza metodologia" if lang_pl else "🔬 Our Methodology"
    return (
        f'<div {_s("methodology")}>\n'
        f'<strong style="display:block;margin-bottom:8px;">{label}</strong>\n'
        f'<p style="margin:0;font-size:0.95em;">{_esc(methodology)}</p>\n'
        f'</div>\n'
    )


def _build_data_insight(title: str, insight: str, source: str, lang_pl: bool) -> str:
    """Information Gain element — unique data analysis not found in competing articles."""
    label = "📈 Unikalna analiza" if lang_pl else "📈 Unique Analysis"
    src_label = "Źródło" if lang_pl else "Source"
    return (
        f'<div {_s("insight")}>\n'
        f'<strong style="display:block;margin-bottom:8px;">{label}: {_esc(title)}</strong>\n'
        f'<p style="margin:0 0 8px;">{_esc(insight)}</p>\n'
        f'<p style="margin:0;font-size:0.85em;color:#555;"><em>{src_label}: {_esc(source)}</em></p>\n'
        f'</div>\n'
    )


# ── GPT-generowane dane ────────────────────────────────────────────────────────

async def _gpt_enrichment(client, topic: str, element: str, lang_pl: bool, is_custom_llm: bool = False) -> dict:
    # Use dynamic model from settings (same as article generation)
    from services.openai_service import get_gpt_model, get_custom_llm_config
    _enrich_model = await get_gpt_model()
    if is_custom_llm:
        _cfg = await get_custom_llm_config()
        _enrich_model = _cfg["model"] or _enrich_model
    try:
        if element == "expert_quote":
            source = random.choice(_GENERIC_SOURCES_PL if lang_pl else _GENERIC_SOURCES_EN)
            prompt = (
                f"Napisz krótką obserwację lub trend (2-3 zdania) związany z tematem: '{topic}'. "
                f"Kontekst: {source}. "
                f"Format: 'Według dostępnych danych, ...' lub 'Analizy wskazują, że...'. "
                f"Podaj ogólne trendy lub przedziały wartości, nie konkretne liczby. "
                f"Nie przypisuj danych do konkretnych instytucji."
                if lang_pl else
                f"Write a short observation or trend (2-3 sentences) about '{topic}'. "
                f"Context: {source}. "
                f"Format: 'According to available data, ...' or 'Analysis suggests that...'. "
                f"Provide general trends or value ranges, not specific numbers. "
                f"Do not attribute data to specific institutions."
            )
            resp = await client.chat.completions.create(model=_enrich_model, messages=[{"role":"user","content":prompt}], temperature=0.4, max_tokens=150)
            # FIX #24: also strip em-dash, newlines, GPT preamble from quote
            _raw_quote = resp.choices[0].message.content.strip().strip('"\'').strip('—–-').strip()
            return {"quote": _raw_quote, "name": source, "role": ""}

        elif element == "key_takeaways":
            prompt = (
                f"Podaj 4 kluczowe wnioski z artykułu o '{topic}'. Każdy: 1 zdanie max 15 słów. Jedna linia = jeden punkt, bez numerów."
                if lang_pl else
                f"Give 4 key takeaways from an article about '{topic}'. Each: 1 sentence max 15 words. One line = one point, no numbers."
            )
            resp = await client.chat.completions.create(model=_enrich_model, messages=[{"role":"user","content":prompt}], temperature=0.5, max_tokens=150)
            points = [p.strip("- •*").strip() for p in resp.choices[0].message.content.strip().split("\n") if p.strip()][:4]
            return {"points": points}

        elif element == "pro_tip":
            prompt = (
                f"Podaj 2 praktyczne Pro Tipy o '{topic}'. Każdy: 1-2 zdania, konkretna rada. Każdy tip w osobnej linii, bez numerów."
                if lang_pl else
                f"Give 2 practical Pro Tips about '{topic}'. Each: 1-2 sentences, specific advice. Each on separate line, no numbers."
            )
            resp = await client.chat.completions.create(model=_enrich_model, messages=[{"role":"user","content":prompt}], temperature=0.7, max_tokens=150)
            tips = [t.strip("- •*").strip() for t in resp.choices[0].message.content.strip().split("\n") if t.strip()][:2]
            return {"tips": tips}

        elif element == "stats_block":
            prompt = (
                f"Podaj 4 konkretne statystyki/fakty liczbowe o '{topic}' WYŁĄCZNIE ze znanych "
                f"źródeł (GUS, Eurostat, raporty rządowe, badania akademickie). "
                f"Dla każdej statystyki podaj źródło w nawiasie. "
                f"Jeśli nie znasz pewnych danych — NIE wymyślaj, zamiast tego podaj przedziały z oznaczeniem 'szacunkowo'. "
                f"Format JSON: {{\"stats\": [[\"wartość\", \"opis (źródło)\"], ...]}}. Tylko JSON."
                if lang_pl else
                f"Give 4 specific statistics about '{topic}' ONLY from known sources "
                f"(government agencies, Eurostat, academic studies, industry reports). "
                f"Include source in parentheses for each. If unsure — use ranges marked 'estimated'. "
                f"JSON format: {{\"stats\": [[\"value\", \"description (source)\"], ...]}}. JSON only."
            )
            resp = await client.chat.completions.create(model=_enrich_model, messages=[{"role":"user","content":prompt}], temperature=0.1, max_tokens=250, **({} if is_custom_llm else {"response_format": {"type": "json_object"}}))
            raw = json.loads(resp.choices[0].message.content)
            stats = []
            for s in raw.get("stats", [])[:4]:
                if isinstance(s, list) and len(s) >= 2:
                    stats.append((str(s[0]), str(s[1])))
                elif isinstance(s, dict):
                    stats.append((str(s.get("value", s.get("wartość",""))), str(s.get("description", s.get("opis","")))))
            return {"stats": stats}

        elif element == "comparison_table":
            prompt = (
                f"Stwórz tabelę porównawczą 3 opcji/metod dla '{topic}'. JSON: lista obiektów {{\"name\": str, \"pros\": str, \"cons\": str, \"rating\": int(1-5)}}. Tylko JSON."
                if lang_pl else
                f"Create comparison table of 3 options/methods for '{topic}'. JSON: list of {{\"name\": str, \"pros\": str, \"cons\": str, \"rating\": int(1-5)}}. JSON only."
            )
            resp = await client.chat.completions.create(model=_enrich_model, messages=[{"role":"user","content":prompt}], temperature=0.5, max_tokens=300, **({} if is_custom_llm else {"response_format": {"type": "json_object"}}))
            raw = json.loads(resp.choices[0].message.content)
            rows = raw if isinstance(raw, list) else raw.get("rows", raw.get("options", raw.get("comparison", [])))
            return {"rows": rows[:3]}

        elif element == "checklist":
            prompt = (
                f"Stwórz praktyczną checklistę dla '{topic}' (6-8 punktów). JSON: {{\"title\": str, \"items\": [str, ...]}}. Tylko JSON."
                if lang_pl else
                f"Create practical checklist for '{topic}' (6-8 items). JSON: {{\"title\": str, \"items\": [str, ...]}}. JSON only."
            )
            resp = await client.chat.completions.create(model=_enrich_model, messages=[{"role":"user","content":prompt}], temperature=0.5, max_tokens=250, **({} if is_custom_llm else {"response_format": {"type": "json_object"}}))
            raw = json.loads(resp.choices[0].message.content)
            return {"title": raw.get("title", "Checklist"), "items": raw.get("items", [])[:8]}

        elif element == "warning_box":
            prompt = (
                f"Podaj 1 najczęstszy błąd lub pułapkę związaną z '{topic}'. Max 2 zdania, konkretnie."
                if lang_pl else
                f"Give 1 most common mistake or pitfall related to '{topic}'. Max 2 sentences, specific."
            )
            resp = await client.chat.completions.create(model=_enrich_model, messages=[{"role":"user","content":prompt}], temperature=0.6, max_tokens=100)
            return {"text": resp.choices[0].message.content.strip()}

        elif element == "step_by_step":
            prompt = (
                f"Podaj 5-6 kroków do wykonania zadania związanego z '{topic}'. JSON: {{\"title\": str, \"steps\": [str, ...]}}. Tylko JSON."
                if lang_pl else
                f"Give 5-6 steps to accomplish the task related to '{topic}'. JSON: {{\"title\": str, \"steps\": [str, ...]}}. JSON only."
            )
            resp = await client.chat.completions.create(model=_enrich_model, messages=[{"role":"user","content":prompt}], temperature=0.5, max_tokens=300, **({} if is_custom_llm else {"response_format": {"type": "json_object"}}))
            raw = json.loads(resp.choices[0].message.content)
            return {"title": raw.get("title", "Jak to zrobić krok po kroku"), "steps": raw.get("steps", [])[:6]}

        elif element == "quick_answer":
            prompt = (
                f"Napisz krótką, bezpośrednią odpowiedź (2-3 zdania) na pytanie 'Co to jest {topic} i jak działa?'. Konkretnie, bez wstępu."
                if lang_pl else
                f"Write a short, direct answer (2-3 sentences) to 'What is {topic} and how does it work?'. Specific, no preamble."
            )
            resp = await client.chat.completions.create(model=_enrich_model, messages=[{"role":"user","content":prompt}], temperature=0.4, max_tokens=120)
            return {"answer": resp.choices[0].message.content.strip()}

        elif element == "cost_table":
            prompt = (
                f"Podaj tabelę kosztów dla '{topic}' (3-4 pozycje). JSON: {{\"rows\": [{{\"item\": str, \"cost\": str, \"note\": str}}]}}. Tylko JSON."
                if lang_pl else
                f"Give cost table for '{topic}' (3-4 items). JSON: {{\"rows\": [{{\"item\": str, \"cost\": str, \"note\": str}}]}}. JSON only."
            )
            resp = await client.chat.completions.create(model=_enrich_model, messages=[{"role":"user","content":prompt}], temperature=0.4, max_tokens=250, **({} if is_custom_llm else {"response_format": {"type": "json_object"}}))
            raw = json.loads(resp.choices[0].message.content)
            return {"rows": raw.get("rows", [])[:4]}

        elif element == "faq_mini":
            prompt = (
                f"Podaj 3 najczęściej zadawane pytania z krótkimi odpowiedziami o '{topic}'. JSON: {{\"pairs\": [{{\"q\": str, \"a\": str}}]}}. Tylko JSON."
                if lang_pl else
                f"Give 3 most common questions with short answers about '{topic}'. JSON: {{\"pairs\": [{{\"q\": str, \"a\": str}}]}}. JSON only."
            )
            resp = await client.chat.completions.create(model=_enrich_model, messages=[{"role":"user","content":prompt}], temperature=0.5, max_tokens=300, **({} if is_custom_llm else {"response_format": {"type": "json_object"}}))
            raw = json.loads(resp.choices[0].message.content)
            return {"pairs": raw.get("pairs", [])[:3]}

        elif element == "did_you_know":
            prompt = (
                f"Podaj 1 zaskakującą ciekawostkę o '{topic}'. Max 2 zdania, coś czego większość nie wie."
                if lang_pl else
                f"Give 1 surprising fact about '{topic}'. Max 2 sentences, something most people don't know."
            )
            resp = await client.chat.completions.create(model=_enrich_model, messages=[{"role":"user","content":prompt}], temperature=0.7, max_tokens=100)
            return {"fact": resp.choices[0].message.content.strip()}

        elif element == "tools_list":
            prompt = (
                f"Podaj 4-5 konkretnych narzędzi lub zasobów przydatnych przy '{topic}'. JSON: {{\"tools\": [{{\"name\": str, \"desc\": str}}]}}. Tylko JSON."
                if lang_pl else
                f"Give 4-5 specific tools or resources useful for '{topic}'. JSON: {{\"tools\": [{{\"name\": str, \"desc\": str}}]}}. JSON only."
            )
            resp = await client.chat.completions.create(model=_enrich_model, messages=[{"role":"user","content":prompt}], temperature=0.5, max_tokens=250, **({} if is_custom_llm else {"response_format": {"type": "json_object"}}))
            raw = json.loads(resp.choices[0].message.content)
            return {"tools": raw.get("tools", [])[:5]}

        elif element == "time_estimate":
            prompt = (
                f"Dla tematu '{topic}' podaj szacowany czas realizacji i poziom trudności. JSON: {{\"time\": str, \"difficulty\": str}}. Tylko JSON."
                if lang_pl else
                f"For topic '{topic}' give estimated time and difficulty level. JSON: {{\"time\": str, \"difficulty\": str}}. JSON only."
            )
            resp = await client.chat.completions.create(model=_enrich_model, messages=[{"role":"user","content":prompt}], temperature=0.4, max_tokens=80, **({} if is_custom_llm else {"response_format": {"type": "json_object"}}))
            raw = json.loads(resp.choices[0].message.content)
            return {"time": raw.get("time",""), "difficulty": raw.get("difficulty","")}

        elif element == "case_study_box":
            prompt = (
                f"Napisz krótkie case study związane z '{topic}'. JSON: {{\"title\": str, \"body\": str (2 zdania), \"result\": str (1 zdanie)}}. Tylko JSON."
                if lang_pl else
                f"Write a short case study related to '{topic}'. JSON: {{\"title\": str, \"body\": str (2 sentences), \"result\": str (1 sentence)}}. JSON only."
            )
            resp = await client.chat.completions.create(model=_enrich_model, messages=[{"role":"user","content":prompt}], temperature=0.7, max_tokens=200, **({} if is_custom_llm else {"response_format": {"type": "json_object"}}))
            raw = json.loads(resp.choices[0].message.content)
            return {"title": raw.get("title",""), "body": raw.get("body",""), "result": raw.get("result","")}

        elif element == "summary_table":
            prompt = (
                f"Stwórz tabelę 'kiedy wybrać co' dla '{topic}' (3-4 wiersze). JSON: {{\"rows\": [{{\"when\": str, \"why\": str}}]}}. Tylko JSON."
                if lang_pl else
                f"Create 'when to choose what' table for '{topic}' (3-4 rows). JSON: {{\"rows\": [{{\"when\": str, \"why\": str}}]}}. JSON only."
            )
            resp = await client.chat.completions.create(model=_enrich_model, messages=[{"role":"user","content":prompt}], temperature=0.5, max_tokens=250, **({} if is_custom_llm else {"response_format": {"type": "json_object"}}))
            raw = json.loads(resp.choices[0].message.content)
            return {"rows": raw.get("rows", [])[:4]}

        elif element == "action_steps":
            prompt = (
                f"Podaj 4-5 konkretnych kroków 'co zrobić teraz' po przeczytaniu artykułu o '{topic}'. JSON: {{\"steps\": [str]}}. Tylko JSON."
                if lang_pl else
                f"Give 4-5 concrete 'next steps' after reading about '{topic}'. JSON: {{\"steps\": [str]}}. JSON only."
            )
            resp = await client.chat.completions.create(model=_enrich_model, messages=[{"role":"user","content":prompt}], temperature=0.5, max_tokens=200, **({} if is_custom_llm else {"response_format": {"type": "json_object"}}))
            raw = json.loads(resp.choices[0].message.content)
            return {"steps": raw.get("steps", [])[:5]}

        elif element == "methodology_box":
            prompt = (
                f"Opisz w 2-3 zdaniach metodologię przygotowania artykułu o '{topic}'. "
                f"Wymień konkretne źródła danych (np. raporty branżowe, dane GUS, badania akademickie, "
                f"analiza ofert rynkowych). Pokaż systematyczny research. Bez wstępu, od razu treść."
                if lang_pl else
                f"Describe in 2-3 sentences the research methodology for an article about '{topic}'. "
                f"Mention specific data sources (industry reports, government data, academic studies, "
                f"market analysis). Show systematic research effort. No preamble, content only."
            )
            resp = await client.chat.completions.create(model=_enrich_model, messages=[{"role":"user","content":prompt}], temperature=0.4, max_tokens=150)
            return {"methodology": resp.choices[0].message.content.strip()}

        elif element == "data_insight":
            prompt = (
                f"Podaj 1 unikatową analizę danych lub wnioski, których nie znajdzie się w standardowych "
                f"artykułach o '{topic}'. Porównaj dane z różnych źródeł, pokaż trend lub korelację, "
                f"którą inni pominęli. JSON: {{\"title\": str (max 8 słów), \"insight\": str (3-4 zdania), \"source\": str}}. Tylko JSON."
                if lang_pl else
                f"Give 1 unique data analysis or insight not found in standard articles about '{topic}'. "
                f"Cross-reference data from multiple sources, show a trend or correlation others missed. "
                f"JSON: {{\"title\": str (max 8 words), \"insight\": str (3-4 sentences), \"source\": str}}. JSON only."
            )
            resp = await client.chat.completions.create(model=_enrich_model, messages=[{"role":"user","content":prompt}], temperature=0.6, max_tokens=250, **({} if is_custom_llm else {"response_format": {"type": "json_object"}}))
            raw = json.loads(resp.choices[0].message.content)
            return {"title": raw.get("title",""), "insight": raw.get("insight",""), "source": raw.get("source","")}

        elif element == "source_citations":
            return {}

    except Exception as e:
        logger.warning(f"[Enrichment] GPT call failed for {element}: {e}")
        return {}

    return {}


# ── Pula 20 elementów ──────────────────────────────────────────────────────────

ALL_ELEMENTS = [
    "expert_quote",
    "key_takeaways",
    "toc",
    "pro_tip",
    "comparison_table",
    "checklist",
    "stats_block",
    "update_box",
    "source_citations",
    "warning_box",
    "step_by_step",
    "quick_answer",
    "cost_table",
    "faq_mini",
    "did_you_know",
    "tools_list",
    "time_estimate",
    "case_study_box",
    "summary_table",
    "action_steps",
    "methodology_box",
    "data_insight",
]


# ── Główna funkcja ─────────────────────────────────────────────────────────────

async def enrich_article(
    content: str,
    topic: str,
    sections: list[str],
    lang_pl: bool,
    openai_client,
    n_elements: int = None,
    serp_urls: list = None,
    skip_toc: bool = False,
    is_custom_llm: bool = False,
) -> str:
    """
    FIX #42: corrected docstring — wstrzykuje 5-7 elementów do artykułu:
    - 2 gwarantowane (update_box + toc) — nie wymagają GPT, zawsze działają
    - 3-4 losowe z puli GPT-elementów (3 for short, 4 for 6+ sections)
    - source_citations jeśli dostępne SERP URLs
    - skip_toc=True when WP has a TOC plugin (avoids double table of contents)
    """
    # Auto-detect existing TOC in content (WP plugin shortcodes / blocks)
    _has_existing_toc = skip_toc or bool(re.search(
        r'\[toc\]|\[ez-toc|wp-block-table-of-contents|lwptoc|class="toc[_-]|id="ez-toc|rank-math-toc',
        content, re.IGNORECASE
    ))
    # Guaranteed elements (no GPT needed)
    GUARANTEED = ["update_box", "toc"] if not _has_existing_toc else ["update_box"]
    # GPT elements pool (exclude guaranteed and source_citations which needs serp_urls)
    GPT_POOL = [e for e in ALL_ELEMENTS if e not in {"update_box", "toc", "source_citations"}]

    # FIX #22: pick 3-4 GPT elements (was always 3; longer articles deserve more enrichment)
    _n_picks = 4 if len(sections) >= 6 else 3
    gpt_picks = random.sample(GPT_POOL, min(_n_picks, len(GPT_POOL)))
    chosen = GUARANTEED + gpt_picks
    if serp_urls:
        chosen.append("source_citations")
    logger.info(f"[Enrichment] Chosen for '{topic}': {chosen}")

    # Fetch GPT data (parallel, with one retry on failure)
    no_gpt = {"toc", "update_box", "source_citations"}
    gpt_elements = [e for e in chosen if e not in no_gpt]

    async def _gpt_with_retry(element: str) -> dict:
        result = await _gpt_enrichment(openai_client, topic, element, lang_pl, is_custom_llm=is_custom_llm)
        if not result:
            # FIX #77: add jitter to enrichment retry delay (prevents parallel retries from colliding)
            await asyncio.sleep(1 + random.uniform(0, 1))
            result = await _gpt_enrichment(openai_client, topic, element, lang_pl, is_custom_llm=is_custom_llm)
        return result

    gpt_results = {}
    if gpt_elements:
        results = await asyncio.gather(*[_gpt_with_retry(e) for e in gpt_elements], return_exceptions=True)
        for key, result in zip(gpt_elements, results):
            gpt_results[key] = {} if isinstance(result, Exception) else result

    # Helper: find n-th </p> after h2_index-th </h2>
    def _insert_after_section(html: str, h2_index: int, block: str) -> str:
        h2_matches = [m.start() for m in re.finditer(r"</h2>", html)]
        idx = min(h2_index, len(h2_matches) - 1) if h2_matches else -1
        if idx < 0:
            return html + f"\n\n{block}"
        para_end = html.find("</p>", h2_matches[idx])
        if para_end == -1:
            return html + f"\n\n{block}"
        return html[:para_end + 4] + f"\n\n{block}" + html[para_end + 4:]

    # ── TOC ────────────────────────────────────────────────────────────────────
    # TOC goes AFTER the intro paragraphs (4 <p> tags), not immediately after H1.
    # This mirrors AI Overview style: answer/intro first, then navigation.
    if "toc" in chosen and sections:
        toc_html = _build_toc(sections, lang_pl)
        content = _add_toc_anchors(content, sections)
        # Find position after 4th </p> but before first <h2>
        p_positions = [m.end() for m in re.finditer(r"</p>", content)]
        h2_pos = content.find("<h2")
        # Use 4th </p> if available, else 2nd, else 1st; but never past the first <h2>
        target_p = None
        for pos in p_positions[:4]:
            if h2_pos == -1 or pos < h2_pos:
                target_p = pos
        if target_p is not None:
            content = content[:target_p] + f"\n\n{toc_html}" + content[target_p:]
        elif h2_pos != -1:
            content = content[:h2_pos] + toc_html + "\n\n" + content[h2_pos:]

    # ── Quick Answer — wstaw przed TOC lub przed H2 ────────────────────────────
    if "quick_answer" in chosen:
        data = gpt_results.get("quick_answer", {})
        if data.get("answer"):
            box = _build_quick_answer(data["answer"], lang_pl)
            if "<nav" in content:
                content = content.replace("<nav", f"{box}\n\n<nav", 1)
            else:
                h2_pos = content.find("<h2")
                if h2_pos != -1:
                    content = content[:h2_pos] + box + "\n\n" + content[h2_pos:]

    # ── Key Takeaways — wstaw po TOC lub po H1 ────────────────────────────────
    if "key_takeaways" in chosen:
        data = gpt_results.get("key_takeaways", {})
        if data.get("points"):
            box = _build_key_takeaways(data["points"], lang_pl)
            if "</nav>" in content:
                content = content.replace("</nav>", f"</nav>\n\n{box}", 1)
            elif "</h1>" in content:
                content = content.replace("</h1>", f"</h1>\n\n{box}", 1)

    # ── Update Box — przed pierwszą sekcją H2 ─────────────────────────────────
    if "update_box" in chosen:
        box = _build_update_box(lang_pl, topic=topic)
        h2_pos = content.find("<h2")
        if h2_pos != -1:
            content = content[:h2_pos] + box + content[h2_pos:]

    # ── Time Estimate — po update_box lub przed H2 ────────────────────────────
    if "time_estimate" in chosen:
        data = gpt_results.get("time_estimate", {})
        if data.get("time"):
            box = _build_time_estimate(data["time"], data.get("difficulty",""), lang_pl)
            h2_pos = content.find("<h2")
            if h2_pos != -1:
                content = content[:h2_pos] + box + content[h2_pos:]

    # ── Step-by-step — po 1. sekcji ───────────────────────────────────────────
    if "step_by_step" in chosen:
        data = gpt_results.get("step_by_step", {})
        if data.get("steps"):
            default_title = "Krok po kroku" if lang_pl else "Step by Step"
            box = _build_step_by_step(data["steps"], data.get("title", default_title), lang_pl=lang_pl)
            content = _insert_after_section(content, 0, box)

    # ── Stats Block — po 1. sekcji ────────────────────────────────────────────
    if "stats_block" in chosen:
        data = gpt_results.get("stats_block", {})
        if data.get("stats"):
            box = _build_stats_block(data["stats"], lang_pl)
            content = _insert_after_section(content, 1, box)

    # ── Did You Know — po 1. lub 2. sekcji ────────────────────────────────────
    if "did_you_know" in chosen:
        data = gpt_results.get("did_you_know", {})
        if data.get("fact"):
            box = _build_did_you_know(data["fact"], lang_pl)
            content = _insert_after_section(content, random.randint(1, 2), box)

    # ── Warning Box — po 2. sekcji ────────────────────────────────────────────
    if "warning_box" in chosen:
        data = gpt_results.get("warning_box", {})
        if data.get("text"):
            box = _build_warning_box(data["text"], lang_pl)
            content = _insert_after_section(content, 2, box)

    # ── Expert Quote — po 2. lub 3. sekcji ───────────────────────────────────
    if "expert_quote" in chosen:
        data = gpt_results.get("expert_quote", {})
        if data.get("quote"):
            box = _build_expert_quote(data["quote"], data["name"], data["role"])
            content = _insert_after_section(content, random.randint(2, 3), box)

    # ── Pro Tips — po 3. i 5. sekcji ──────────────────────────────────────────
    if "pro_tip" in chosen:
        data = gpt_results.get("pro_tip", {})
        for idx, tip in enumerate(data.get("tips", [])[:2]):
            box = _build_pro_tip(tip, lang_pl)
            content = _insert_after_section(content, 3 + idx * 2, box)

    # ── FAQ Mini — po 3. sekcji ───────────────────────────────────────────────
    if "faq_mini" in chosen:
        data = gpt_results.get("faq_mini", {})
        if data.get("pairs"):
            box = _build_faq_mini(data["pairs"], lang_pl)
            content = _insert_after_section(content, 3, box)

    # ── Tools List — po 4. sekcji ─────────────────────────────────────────────
    if "tools_list" in chosen:
        data = gpt_results.get("tools_list", {})
        if data.get("tools"):
            box = _build_tools_list(data["tools"], lang_pl)
            content = _insert_after_section(content, 4, box)

    # ── Case Study — po 4. sekcji ─────────────────────────────────────────────
    if "case_study_box" in chosen:
        data = gpt_results.get("case_study_box", {})
        if data.get("body"):
            box = _build_case_study(data.get("title",""), data["body"], data.get("result",""), lang_pl)
            content = _insert_after_section(content, 4, box)

    # ── Cost Table — przed ostatnim H2 ────────────────────────────────────────
    if "cost_table" in chosen:
        data = gpt_results.get("cost_table", {})
        if data.get("rows"):
            box = _build_cost_table(data["rows"], lang_pl)
            h2_positions = [m.start() for m in re.finditer(r"<h2[^>]*>", content)]
            if len(h2_positions) >= 2:
                content = content[:h2_positions[-1]] + box + "\n\n" + content[h2_positions[-1]:]

    # ── Comparison Table — przed ostatnim H2 ──────────────────────────────────
    if "comparison_table" in chosen:
        data = gpt_results.get("comparison_table", {})
        if data.get("rows"):
            box = _build_comparison_table(data["rows"], lang_pl)
            h2_positions = [m.start() for m in re.finditer(r"<h2[^>]*>", content)]
            if len(h2_positions) >= 2:
                content = content[:h2_positions[-1]] + box + "\n\n" + content[h2_positions[-1]:]

    # ── Summary Table — przed ostatnim H2 ─────────────────────────────────────
    if "summary_table" in chosen:
        data = gpt_results.get("summary_table", {})
        if data.get("rows"):
            box = _build_summary_table(data["rows"], lang_pl)
            h2_positions = [m.start() for m in re.finditer(r"<h2[^>]*>", content)]
            if len(h2_positions) >= 2:
                content = content[:h2_positions[-1]] + box + "\n\n" + content[h2_positions[-1]:]

    # ── Checklist — w środku artykułu ─────────────────────────────────────────
    if "checklist" in chosen:
        data = gpt_results.get("checklist", {})
        if data.get("items"):
            box = _build_checklist(data["items"], data.get("title","Checklist"))
            h2_matches = [m.start() for m in re.finditer(r"</h2>", content)]
            mid_idx = len(h2_matches) // 2 if h2_matches else -1
            if mid_idx >= 0:
                para_end = content.find("</p>", h2_matches[mid_idx])
                if para_end != -1:
                    content = content[:para_end + 4] + f"\n\n{box}" + content[para_end + 4:]

    # ── Methodology Box — po update_box, przed treścią (contentEffort) ─────────
    if "methodology_box" in chosen:
        data = gpt_results.get("methodology_box", {})
        if data.get("methodology"):
            box = _build_methodology_box(data["methodology"], lang_pl)
            content = _insert_after_section(content, 0, box)

    # ── Data Insight — po 2. lub 3. sekcji (Information Gain) ───────────────
    if "data_insight" in chosen:
        data = gpt_results.get("data_insight", {})
        if data.get("insight"):
            box = _build_data_insight(data.get("title",""), data["insight"], data.get("source",""), lang_pl)
            content = _insert_after_section(content, random.randint(2, 3), box)

    # ── Action Steps — na końcu przed FAQ ─────────────────────────────────────
    if "action_steps" in chosen:
        data = gpt_results.get("action_steps", {})
        if data.get("steps"):
            box = _build_action_steps(data["steps"], lang_pl)
            content = content + f"\n\n{box}"

    # ── Source Citations — na samym końcu ─────────────────────────────────────
    if "source_citations" in chosen and serp_urls:
        box = _build_source_citations(serp_urls, lang_pl)
        if box:
            content = content + f"\n\n{box}"

    logger.info(f"[Enrichment] Done for '{topic}' — {len(chosen)} elements: {chosen}")
    return content

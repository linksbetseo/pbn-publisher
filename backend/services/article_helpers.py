"""
Extracted helpers from openai_service.py — SERP cache, text utils, HTML conversion.
Keeps openai_service.py focused on article generation pipeline.
"""
import hashlib
import re
import time
import logging
import json as _json
import unicodedata
from typing import Optional

import aiosqlite
from config import DB_PATH

logger = logging.getLogger(__name__)

# FIX #78: extended SERP cache TTL from 24h to 48h — reduces DataForSEO API costs
# (SERP data for most keywords doesn't change significantly within 48h)
import os as _os_helpers
_SERP_CACHE_TTL = int(_os_helpers.getenv("SERP_CACHE_TTL", "172800"))  # default 48h, configurable

_SERP_CACHE_TABLE_CREATED = False


async def ensure_serp_cache_table() -> None:
    global _SERP_CACHE_TABLE_CREATED
    if _SERP_CACHE_TABLE_CREATED:
        return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS serp_cache (cache_key TEXT PRIMARY KEY, data_json TEXT, expires_at REAL)"
            )
            await db.commit()
        _SERP_CACHE_TABLE_CREATED = True
    except Exception as e:
        logger.warning(f"[SERP] Cache table init failed: {e}")


async def serp_cache_get(key: str) -> Optional[dict]:
    await ensure_serp_cache_table()
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


async def serp_cache_set(key: str, data: dict) -> None:
    await ensure_serp_cache_table()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO serp_cache (cache_key, data_json, expires_at) VALUES (?,?,?)",
                (key, _json.dumps(data, ensure_ascii=False), time.time() + _SERP_CACHE_TTL)
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"[SERP] Cache write failed: {e}")


# FIX #29: expanded skip list — added gov.pl, edu.pl, major e-commerce, social media, forums
BLOG_SKIP = re.compile(
    r"(youtube\.com|facebook\.com|twitter\.com|x\.com|instagram\.com|tiktok\.com"
    r"|wikipedia\.org|reddit\.com|pinterest\.com|allegro\.pl|amazon\.|ebay\."
    r"|olx\.pl|ceneo\.pl|linkedin\.com|quora\.com|wykop\.pl|money\.pl"
    r"|bankier\.pl|wp\.pl|onet\.pl|interia\.pl|gazeta\.pl|tvn24\.pl"
    r"|polsatnews\.pl|rmf24\.pl|trojmiasto\.pl|gratka\.pl|otodom\.pl"
    r"|otomoto\.pl|morele\.net|x-kom\.pl|mediaexpert\.pl"
    r"|gov\.pl|sejm\.gov\.pl|\.edu\.pl|empik\.com|zalando\.pl"
    r"|threads\.net|mastodon\.social|medium\.com|substack\.com)",
    re.IGNORECASE,
)


def is_blog_url(url: str) -> bool:
    return not BLOG_SKIP.search(url)


def count_words(text: str) -> int:
    # FIX #30: strip HTML tags before counting (was counting tag attribute words too)
    # FIX #79: also strip JSON-LD script blocks (schema data inflates word count)
    clean = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r'<[^>]+>', ' ', clean)
    return len(re.findall(r'\w+', clean))


def keyword_density(text: str, keyword: str) -> float:
    # FIX #31: strip HTML before computing density (was matching keywords inside tag attributes)
    clean = re.sub(r'<[^>]+>', ' ', text)
    # FIX #35: use 'clean' for word counting too (was calling count_words(text) which re-strips HTML)
    words = len(re.findall(r'\w+', clean))
    if not words:
        return 0.0
    count = len(re.findall(re.escape(keyword.lower()), clean.lower()))
    return round((count * len(keyword.split())) / words * 100, 2)


def extract_lsi(text: str, keyword: str, top_n: int = 20) -> list[str]:
    """Extract most frequent non-stopword unigrams + bigrams from text."""
    STOPWORDS = {
        "i", "w", "z", "na", "do", "po", "o", "a", "się", "nie", "jak", "co",
        "czy", "że", "to", "jest", "są", "dla", "przez", "przy", "za", "od",
        "ile", "ten", "ta", "te", "tego", "tej", "być", "mieć", "też", "już",
        "the", "is", "in", "of", "and", "to", "a", "for", "with", "that", "this",
        "it", "are", "was", "be", "as", "at", "by", "an", "or", "but", "not",
        "have", "from", "on", "your", "can", "we", "our", "you", "they", "their",
    }
    kw_words = set(keyword.lower().split())
    # FIX #36: strip HTML tags before extracting LSI words (was counting tag attribute words)
    clean = re.sub(r'<[^>]+>', ' ', text)
    words = re.findall(r'\b[a-ząćęłńóśźżA-ZĄĆĘŁŃÓŚŹŻ]{3,}\b', clean.lower())
    freq: dict[str, int] = {}
    for w in words:
        if w not in STOPWORDS and w not in kw_words and len(w) >= 4 and not w.isdigit():
            freq[w] = freq.get(w, 0) + 1
    for i in range(len(words) - 1):
        w1, w2 = words[i], words[i + 1]
        if (w1 not in STOPWORDS and w2 not in STOPWORDS
                and w1 not in kw_words and w2 not in kw_words
                and len(w1) >= 3 and len(w2) >= 3):
            bigram = f"{w1} {w2}"
            freq[bigram] = freq.get(bigram, 0) + 1
    unigrams = [(w, c) for w, c in freq.items() if " " not in w]
    bigrams = [(w, c) for w, c in freq.items() if " " in w]
    top_uni = [w for w, _ in sorted(unigrams, key=lambda x: -x[1])[:top_n // 2 + 5]]
    top_bi = [w for w, _ in sorted(bigrams, key=lambda x: -x[1])[:top_n // 2]]
    combined = top_uni + top_bi
    return combined[:top_n]


def content_fingerprint(content: str) -> str:
    # FIX #32: strip HTML before fingerprinting (was including schema JSON-LD in fingerprint)
    clean = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r'<[^>]+>', ' ', clean)
    words = re.findall(r'\w+', clean.lower())
    total = len(words)
    start = words[:100]
    mid_start = total // 3
    middle = words[mid_start:mid_start + 100]
    # FIX #33: also include end segment for better dedup (articles can share intros but differ at end)
    end_start = max(0, total - 100)
    end = words[end_start:]
    combined = start + middle + end
    return hashlib.md5(" ".join(combined).encode()).hexdigest()


def markdown_to_html(text: str) -> str:
    """Convert markdown to HTML."""
    def _convert_table(m: re.Match) -> str:
        rows = [r.strip() for r in m.group(0).strip().split("\n") if r.strip()]
        html = "<table>\n"
        for i, row in enumerate(rows):
            if re.match(r"^[\s|:-]+$", row):
                continue
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
    # FIX #37: wrap ordered list items in <ol> instead of leaving them unwrapped (was only <li> without parent)
    text = re.sub(r"(?m)^\d+\. (.+)$", r"<li>\1</li>", text)
    # Wrap consecutive <li> that came from ordered lists in <ol> (those not already inside <ul>)
    def _wrap_orphan_li(m: re.Match) -> str:
        if m.start() > 0 and text[max(0, m.start()-5):m.start()].endswith("</ul>"):
            return m.group(0)
        return f"<ol>{m.group(0)}</ol>" if "<ul>" not in m.group(0) else m.group(0)
    text = re.sub(r"(?:<li>.*?</li>\n?)+", _wrap_orphan_li, text, flags=re.DOTALL)
    lines = text.split("\n")
    result = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        result.append(line if line.startswith("<") else f"<p>{line}</p>")
    return "\n".join(result)


def slugify_heading(text: str) -> str:
    """FIX #38: generate URL-safe anchor slugs from headings (handles Polish chars)."""
    # SEO #73: explicit Polish char mapping (NFKD doesn't handle ł→l)
    _pl_map = str.maketrans({"ł": "l", "Ł": "L"})
    text_mapped = text.lower().translate(_pl_map)
    nfkd = unicodedata.normalize("NFKD", text_mapped)
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_str).strip("-")
    return slug[:60]


def strip_markdown_remnants(html: str) -> str:
    html = re.sub(r"```(?:html|HTML)?\s*", "", html)
    html = re.sub(r"```\s*$", "", html, flags=re.MULTILINE)
    # FIX #34: remove markdown horizontal rules (GPT sometimes inserts ---)
    html = re.sub(r"(?m)^\s*[-*_]{3,}\s*$", "", html)
    html = re.sub(r"(?m)^\s*####\s+(.+)$", r"<h4>\1</h4>", html)
    html = re.sub(r"(?m)^\s*###\s+(.+)$", r"<h3>\1</h3>", html)
    html = re.sub(r"(?m)^\s*##\s+(.+)$", r"<h2>\1</h2>", html)
    html = re.sub(r"(?m)^\s*#\s+(.+)$", r"<h1>\1</h1>", html)
    html = re.sub(r"<p>\s*####\s+(.+?)\s*</p>", r"<h4>\1</h4>", html, flags=re.DOTALL)
    html = re.sub(r"<p>\s*###\s+(.+?)\s*</p>", r"<h3>\1</h3>", html, flags=re.DOTALL)
    html = re.sub(r"<p>\s*##\s+(.+?)\s*</p>", r"<h2>\1</h2>", html, flags=re.DOTALL)
    html = re.sub(r"<p>\s*#\s+(.+?)\s*</p>", r"<h2>\1</h2>", html, flags=re.DOTALL)
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"\*(.+?)\*", r"<em>\1</em>", html)
    html = re.sub(r"`([^`]+?)`", r"<code>\1</code>", html)
    return html.strip()

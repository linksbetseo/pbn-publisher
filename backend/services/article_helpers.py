"""
Extracted helpers from openai_service.py — SERP cache, text utils, HTML conversion.
Keeps openai_service.py focused on article generation pipeline.
"""
import hashlib
import re
import time
import logging
import json as _json
from typing import Optional

import aiosqlite
from config import DB_PATH

logger = logging.getLogger(__name__)

_SERP_CACHE_TTL = 86400  # 24 hours

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


BLOG_SKIP = re.compile(
    r"(youtube\.com|facebook\.com|twitter\.com|instagram\.com|tiktok\.com"
    r"|wikipedia\.org|reddit\.com|pinterest\.com|allegro\.pl|amazon\.|ebay\."
    r"|olx\.pl|ceneo\.pl|linkedin\.com|quora\.com|wykop\.pl|money\.pl"
    r"|bankier\.pl|wp\.pl|onet\.pl|interia\.pl|gazeta\.pl|tvn24\.pl"
    r"|polsatnews\.pl|rmf24\.pl|trojmiasto\.pl|gratka\.pl|otodom\.pl"
    r"|otomoto\.pl|morele\.net|x-kom\.pl|mediaexpert\.pl)",
    re.IGNORECASE,
)


def is_blog_url(url: str) -> bool:
    return not BLOG_SKIP.search(url)


def count_words(text: str) -> int:
    return len(re.findall(r'\w+', text))


def keyword_density(text: str, keyword: str) -> float:
    words = count_words(text)
    if not words:
        return 0.0
    count = len(re.findall(re.escape(keyword.lower()), text.lower()))
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
    words = re.findall(r'\b[a-ząćęłńóśźżA-ZĄĆĘŁŃÓŚŹŻ]{3,}\b', text.lower())
    freq: dict[str, int] = {}
    for w in words:
        if w not in STOPWORDS and w not in kw_words and len(w) >= 4:
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
    words = re.findall(r'\w+', content.lower())
    total = len(words)
    start = words[:100]
    mid_start = total // 3
    middle = words[mid_start:mid_start + 100]
    combined = start + middle
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
    text = re.sub(r"(?m)^\d+\. (.+)$", r"<li>\1</li>", text)
    lines = text.split("\n")
    result = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        result.append(line if line.startswith("<") else f"<p>{line}</p>")
    return "\n".join(result)


def strip_markdown_remnants(html: str) -> str:
    html = re.sub(r"```(?:html|HTML)?\s*", "", html)
    html = re.sub(r"```\s*$", "", html, flags=re.MULTILINE)
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

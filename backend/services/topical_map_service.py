"""
Topical Map Generator for PBN Publisher.
Builds pillar + supporting page structure from a seed keyword.

Clustering strategy:
- Usuwa seed words z każdej frazy → zostają "differentiators"
- Grupuje po differentiator tokens (co wyróżnia frazę od seeda)
- Seed "prawo pracy" + fraza "prawo pracy urlop" → differentiator = "urlop"
- Frazy z tym samym differentiator trafiają do jednego klastra (pillar page)
"""
import hashlib
import json as _json
import logging
import re
import time
import unicodedata
from collections import Counter, defaultdict

import aiosqlite

from config import DB_PATH
from services.dataforseo_service import DataForSEOClient

logger = logging.getLogger(__name__)

_MAP_CACHE_TTL = 7 * 86400  # 7 days
_map_cache_table_created = False


async def _ensure_map_cache_table() -> None:
    global _map_cache_table_created
    if _map_cache_table_created:
        return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS topical_map_cache "
                "(cache_key TEXT PRIMARY KEY, data_json TEXT, expires_at REAL)"
            )
            await db.commit()
        _map_cache_table_created = True
    except Exception as e:
        logger.warning(f"[MapCache] table init failed: {e}")


async def _map_cache_get(key: str):
    await _ensure_map_cache_table()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT data_json FROM topical_map_cache WHERE cache_key=? AND expires_at > ?",
                (key, time.time())
            ) as cur:
                row = await cur.fetchone()
        if row:
            return _json.loads(row[0])
    except Exception:
        pass
    return None


async def _map_cache_set(key: str, data: dict) -> None:
    await _ensure_map_cache_table()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO topical_map_cache (cache_key, data_json, expires_at) VALUES (?,?,?)",
                (key, _json.dumps(data, ensure_ascii=False), time.time() + _MAP_CACHE_TTL)
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"[MapCache] write failed: {e}")

# Polskie stop words do ignorowania przy klastracji
STOP_WORDS = {
    "i", "w", "z", "na", "do", "po", "o", "a", "się", "nie", "jak", "co",
    "czy", "że", "to", "jest", "są", "dla", "przez", "przy", "za", "od",
    "ile", "kiedy", "kto", "gdzie", "gdy", "bez", "lub", "oraz", "ale",
    "który", "która", "które", "tego", "tej", "ten", "ta", "te", "być",
    "mieć", "móc", "by", "też", "już", "jeszcze", "tylko", "właśnie",
    "np", "tzw", "itp", "wg",
}


def _ascii_fold(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text)
    return "".join(c for c in normalized if unicodedata.category(c) != "Mn").lower()


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _dedupe(keywords: list[dict]) -> list[dict]:
    seen = {}
    for kw in keywords:
        k = _ascii_fold(_clean(kw["keyword"]))
        if not k:
            continue
        if k not in seen or kw.get("search_volume", 0) > seen[k].get("search_volume", 0):
            seen[k] = kw
    return list(seen.values())


def _seed_tokens(seed: str) -> set:
    """Zwraca zestaw tokenów seeda (z ascii-fold) do odejmowania."""
    folded = _ascii_fold(seed)
    return set(t for t in folded.split() if t not in STOP_WORDS and len(t) > 1)


def _differentiators(keyword: str, seed_toks: set) -> list[str]:
    """
    Wyciąga tokeny które RÓŻNIĄ frazę od seeda.
    Np. seed='prawo pracy', keyword='prawo pracy urlop' → ['urlop']
    """
    kw_folded = _ascii_fold(keyword)
    tokens = [t for t in kw_folded.split() if t not in STOP_WORDS and len(t) > 2]
    diff = [t for t in tokens if t not in seed_toks]
    return diff


def _cluster(keywords: list[dict], seed: str, max_clusters: int = 8) -> list[dict]:
    """
    Grupuje frazy po differentiator tokens.
    Każdy klaster = jeden pillar page (osobny temat w ramach seeda).
    """
    seed_toks = _seed_tokens(seed)

    # Zbierz wszystkie differentiator tokens i ich frazy
    token_to_kws: dict[str, list] = defaultdict(list)
    kw_to_diffs: dict[str, list] = {}

    for kw in keywords:
        diffs = _differentiators(kw["keyword"], seed_toks)
        kw_to_diffs[kw["keyword"]] = diffs
        for d in diffs:
            token_to_kws[d].append(kw)

    # Score każdego differentiator tokena: ile fraz ma go + ich łączny wolumen
    token_scores: dict[str, float] = {}
    for token, kws in token_to_kws.items():
        if len(kws) < 2:
            continue
        total_vol = sum(k.get("search_volume", 0) for k in kws)
        token_scores[token] = len(kws) * 2 + total_vol / 500

    # Wybierz top anchors — pillar page anchors
    top_tokens = sorted(token_scores, key=lambda x: token_scores[x], reverse=True)

    # Buduj klastry zachłannie, unikając nakładania się
    clusters: dict[str, list] = {}
    assigned: set[str] = set()
    selected_anchors: list[str] = []

    for token in top_tokens:
        if len(selected_anchors) >= max_clusters:
            break
        # Pomiń tokeny zbyt podobne do już wybranych (prefix match)
        too_similar = any(
            token.startswith(a[:4]) or a.startswith(token[:4])
            for a in selected_anchors
            if len(token) > 3 and len(a) > 3
        )
        if too_similar:
            continue
        selected_anchors.append(token)
        clusters[token] = []

    # Przypisz każdą frazę do najlepiej pasującego klastra
    for kw in sorted(keywords, key=lambda x: x.get("search_volume", 0), reverse=True):
        kw_text = kw["keyword"]
        if kw_text in assigned:
            continue
        diffs = kw_to_diffs.get(kw_text, [])

        best_anchor = None
        best_score = -1

        for anchor in selected_anchors:
            if anchor in diffs:
                # Anchor jest differenziatorem tej frazy → dopasowanie
                score = token_scores.get(anchor, 0)
                if score > best_score:
                    best_score = score
                    best_anchor = anchor

        if best_anchor:
            clusters[best_anchor].append(kw)
            assigned.add(kw_text)

    # Frazy bez dopasowania → "Inne" lub do największego klastra
    unassigned = [kw for kw in keywords if kw["keyword"] not in assigned]
    if unassigned and selected_anchors:
        # Dorzuć do klastra z największą liczbą fraz
        biggest = max(selected_anchors, key=lambda a: len(clusters[a]))
        for kw in unassigned:
            clusters[biggest].append(kw)

    # Buduj wynik
    result = []
    for anchor, kws in clusters.items():
        if not kws:
            continue
        total_vol = sum(k.get("search_volume", 0) for k in kws)
        avg_diff = sum(k.get("keyword_difficulty", 0) for k in kws) / len(kws)

        # Label: anchor zcapitalizowany, max 3 słowa
        label_words = anchor.split()[:3]
        label = " ".join(w.capitalize() for w in label_words)

        result.append({
            "anchor": anchor,
            "label": f"{' '.join(w.capitalize() for w in seed.split())} — {label}",
            "keywords": kws,
            "total_volume": total_vol,
            "avg_difficulty": round(avg_diff, 1),
        })

    return sorted(result, key=lambda x: x["total_volume"], reverse=True)


async def generate_topical_map(
    seed: str,
    location_code: int = 2616,
    language_code: str = "pl",
    min_volume: int = 10,
    max_clusters: int = 8,
    dfs_login: str = "",
    dfs_password: str = "",
    force_refresh: bool = False,
) -> dict:
    """
    Generate topical map: pillar pages + supporting pages.
    Każdy pillar = osobny aspekt/temat seeda.
    Wyniki cachowane 7 dni w SQLite (drogie DataForSEO calls).
    """
    cache_key = hashlib.md5(f"{seed.lower().strip()}:{location_code}:{language_code}:{min_volume}".encode()).hexdigest()
    if not force_refresh:
        cached = await _map_cache_get(cache_key)
        if cached:
            logger.info(f"[TopicalMap] Cache hit for '{seed}'")
            return cached

    client = DataForSEOClient(dfs_login, dfs_password)

    # Fetch keywords (parallel — saves ~5-10s vs sequential)
    raw = []
    import asyncio as _asyncio
    results_parallel = await _asyncio.gather(
        client.keyword_suggestions(seed, location_code, language_code, 500),
        client.keyword_ideas(seed, location_code, language_code, 300),
        return_exceptions=True,
    )
    for kws, name in zip(results_parallel, ["suggestions", "ideas"]):
        if isinstance(kws, Exception):
            logger.warning(f"[TopicalMap] {name} failed: {kws}")
        else:
            raw.extend(kws)
            logger.info(f"[TopicalMap] {name}: {len(kws)}")

    if not raw:
        raise ValueError(f"Brak wyników DataForSEO dla frazy: {seed}")

    # Dedupe
    keywords = _dedupe(raw)
    logger.info(f"[TopicalMap] after dedupe: {len(keywords)}")

    # Volume filter
    filtered = [k for k in keywords if k.get("search_volume", 0) >= min_volume]
    if not filtered:
        filtered = keywords  # fallback bez filtra
    keywords = filtered
    logger.info(f"[TopicalMap] after volume filter (>={min_volume}): {len(keywords)}")

    # Cluster
    clusters = _cluster(keywords, seed, max_clusters)
    logger.info(f"[TopicalMap] clusters: {len(clusters)}")

    # Fallback: jeśli wyszedł tylko 1 klaster, spróbuj z większą liczbą
    if len(clusters) <= 1 and max_clusters < 15:
        clusters = _cluster(keywords, seed, 15)
        logger.info(f"[TopicalMap] retry with max_clusters=15: {len(clusters)}")

    # Build pillar structure
    pillars = []
    for cluster in clusters:
        kws_sorted = sorted(cluster["keywords"], key=lambda x: x.get("search_volume", 0), reverse=True)
        pillar_kw = kws_sorted[0] if kws_sorted else {"keyword": cluster["anchor"], "search_volume": 0}
        supporting = kws_sorted[1:] if len(kws_sorted) > 1 else []

        pillars.append({
            "anchor": cluster["anchor"],
            "label": cluster["label"],
            "pillar_keyword": pillar_kw["keyword"],
            "pillar_volume": pillar_kw.get("search_volume", 0),
            "pillar_difficulty": pillar_kw.get("keyword_difficulty", 0),
            "supporting_keywords": [
                {
                    "keyword": k["keyword"],
                    "search_volume": k.get("search_volume", 0),
                    "keyword_difficulty": k.get("keyword_difficulty", 0),
                }
                for k in supporting[:20]
            ],
            "total_volume": cluster["total_volume"],
            "avg_difficulty": cluster["avg_difficulty"],
        })

    # Force Graph
    nodes = [{"id": "seed", "label": seed, "type": "seed", "size": 24, "color": "#1a2332"}]
    links = []

    for i, p in enumerate(pillars):
        pid = f"pillar_{i}"
        nodes.append({
            "id": pid,
            "label": p["label"],
            "type": "pillar",
            "size": 16,
            "color": "#1a73e8",
            "volume": p["total_volume"],
        })
        links.append({"source": "seed", "target": pid, "strength": 2.0})

        for j, sk in enumerate(p["supporting_keywords"][:8]):
            sid = f"sup_{i}_{j}"
            nodes.append({
                "id": sid,
                "label": sk["keyword"],
                "type": "supporting",
                "size": 8,
                "color": "#4285f4",
                "volume": sk.get("search_volume", 0),
            })
            links.append({"source": pid, "target": sid, "strength": 1.0})

    result = {
        "seed": seed,
        "total_keywords": len(keywords),
        "pillars": pillars,
        "nodes": nodes,
        "links": links,
    }
    await _map_cache_set(cache_key, result)
    return result

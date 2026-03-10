"""
Topical Map Generator for PBN Publisher.
Builds pillar + supporting page structure from a seed keyword.
"""
import logging
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Optional

from services.dataforseo_service import DataForSEOClient

logger = logging.getLogger(__name__)


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


def _cluster(keywords: list[dict], max_clusters: int = 8) -> list[dict]:
    """Group keywords into topic clusters by token frequency."""
    # Count bigrams and unigrams
    token_kws = defaultdict(list)
    for kw in keywords:
        tokens = kw["keyword"].split()
        for i, t in enumerate(tokens):
            if len(t) > 2:
                token_kws[t].append(kw)
            if i < len(tokens) - 1:
                bigram = f"{tokens[i]} {tokens[i+1]}"
                token_kws[bigram].append(kw)

    # Score: frequency * avg_volume
    scores = {}
    for token, kws in token_kws.items():
        if len(kws) < 2:
            continue
        avg_vol = sum(k.get("search_volume", 0) for k in kws) / len(kws)
        is_bigram = " " in token
        weight = 2.0 if is_bigram else 1.0
        scores[token] = len(kws) * weight * (1 + avg_vol / 1000)

    top_anchors = sorted(scores, key=lambda x: scores[x], reverse=True)[:max_clusters]

    # Assign keywords to best anchor
    clusters = {a: [] for a in top_anchors}
    assigned = set()

    for kw in sorted(keywords, key=lambda x: x.get("search_volume", 0), reverse=True):
        kw_text = kw["keyword"]
        best = None
        best_score = 0
        for anchor in top_anchors:
            if anchor in kw_text:
                score = len(anchor.split()) * 10 + scores.get(anchor, 0)
                if score > best_score:
                    best_score = score
                    best = anchor
        if best and kw_text not in assigned:
            clusters[best].append(kw)
            assigned.add(kw_text)

    # Remove empty
    result = []
    for anchor, kws in clusters.items():
        if not kws:
            continue
        total_vol = sum(k.get("search_volume", 0) for k in kws)
        avg_diff = sum(k.get("keyword_difficulty", 0) for k in kws) / len(kws) if kws else 0
        result.append({
            "anchor": anchor,
            "label": " ".join(w.capitalize() for w in anchor.split()),
            "keywords": kws,
            "total_volume": total_vol,
            "avg_difficulty": round(avg_diff, 1),
        })

    return sorted(result, key=lambda x: x["total_volume"], reverse=True)


async def generate_topical_map(
    seed: str,
    location_code: int = 2616,
    language_code: str = "pl",
    min_volume: int = 50,
    max_clusters: int = 8,
    dfs_login: str = "",
    dfs_password: str = "",
) -> dict:
    """
    Generate topical map: pillar pages + supporting pages.

    Returns:
        {
          seed, pillars: [{anchor, label, pillar_keyword, supporting_keywords, total_volume, avg_difficulty}],
          nodes, links  (for Force Graph frontend)
        }
    """
    client = DataForSEOClient(dfs_login, dfs_password)

    # Fetch keywords
    raw = []
    try:
        suggestions = await client.keyword_suggestions(seed, location_code, language_code, 300)
        raw.extend(suggestions)
    except Exception as e:
        logger.warning(f"keyword_suggestions failed: {e}")

    try:
        ideas = await client.keyword_ideas(seed, location_code, language_code, 200)
        raw.extend(ideas)
    except Exception as e:
        logger.warning(f"keyword_ideas failed: {e}")

    if not raw:
        raise ValueError(f"Brak wyników DataForSEO dla frazy: {seed}")

    # Dedupe + volume filter
    keywords = _dedupe(raw)
    keywords = [k for k in keywords if k.get("search_volume", 0) >= min_volume]

    if not keywords:
        # Relax volume filter
        keywords = _dedupe(raw)

    # Cluster
    clusters = _cluster(keywords, max_clusters)

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
                for k in supporting[:15]
            ],
            "total_volume": cluster["total_volume"],
            "avg_difficulty": cluster["avg_difficulty"],
        })

    # Build Force Graph nodes + links
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

    return {
        "seed": seed,
        "total_keywords": len(keywords),
        "pillars": pillars,
        "nodes": nodes,
        "links": links,
    }

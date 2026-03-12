"""
Topical Map Generator for PBN Publisher.
Builds pillar + supporting page structure from a seed keyword.

Architecture based on SiteFocus / SiteRadius principles (Koray Tugberk GUBUR / leak analysis):

SiteFocus  — semantic convergence of all content around a core topic.
             High SiteFocus = content stays tightly within one subject domain.
             Goal: maximize SiteFocus by keeping all clusters semantically adjacent.

SiteRadius — semantic drift of individual documents from the topical core.
             Low SiteRadius = every article stays close to the site's core topic.
             Goal: minimize SiteRadius by pruning outlier / opportunistic keywords.

Implementation:
- token_coherence_score: measures how close a keyword is to the seed (proxy for low SiteRadius)
- cluster_focus_score: measures how tight a cluster is (proxy for high SiteFocus)
- Outlier pruning: keywords with very low coherence (high SiteRadius) are dropped
- Cluster merging: small, semantically similar clusters are merged (preserves SiteFocus)
- Pillar selection: highest-volume keyword WITHIN coherence threshold (not just raw volume)
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


# ── Stopwords ──────────────────────────────────────────────────────────────────

STOP_WORDS = {
    "i", "w", "z", "na", "do", "po", "o", "a", "się", "nie", "jak", "co",
    "czy", "że", "to", "jest", "są", "dla", "przez", "przy", "za", "od",
    "ile", "kiedy", "kto", "gdzie", "gdy", "bez", "lub", "oraz", "ale",
    "który", "która", "które", "tego", "tej", "ten", "ta", "te", "być",
    "mieć", "móc", "by", "też", "już", "jeszcze", "tylko", "właśnie",
    "np", "tzw", "itp", "wg",
}


# ── Text helpers ───────────────────────────────────────────────────────────────

def _ascii_fold(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text)
    return "".join(c for c in normalized if unicodedata.category(c) != "Mn").lower()


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _tokenize(text: str) -> list[str]:
    folded = _ascii_fold(text)
    return [t for t in folded.split() if t not in STOP_WORDS and len(t) > 2]


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
    return set(_tokenize(seed))


# ── SiteRadius proxy: token coherence score ───────────────────────────────────

def _coherence_score(keyword: str, seed_toks: set, seed: str) -> float:
    """
    Measures semantic proximity of a keyword to the seed (SiteRadius proxy).
    Score in [0, 1]:
      1.0 = keyword is fully within the seed's semantic field (low SiteRadius)
      0.0 = keyword is a semantic outlier (high SiteRadius → should be pruned)

    Formula:
      overlap_ratio  = |intersection(kw_tokens, seed_tokens)| / |kw_tokens|
      length_penalty = penalises very short keywords (less topical depth)
      result = weighted average
    """
    kw_toks = set(_tokenize(keyword))
    if not kw_toks:
        return 0.0
    overlap = len(kw_toks & seed_toks)
    overlap_ratio = overlap / len(kw_toks)
    # Partial credit: seed words appearing as substrings in keyword tokens
    seed_str = _ascii_fold(seed)
    substring_bonus = 0.2 if any(st in _ascii_fold(keyword) for st in seed_toks if len(st) > 3) else 0.0
    score = min(1.0, overlap_ratio + substring_bonus)
    return round(score, 3)


def _differentiators(keyword: str, seed_toks: set) -> list[str]:
    """
    Tokens that DIFFERENTIATE the keyword from the seed.
    Seed='prawo pracy', keyword='prawo pracy urlop' → ['urlop']
    These become cluster anchors (topic facets).
    """
    tokens = _tokenize(keyword)
    return [t for t in tokens if t not in seed_toks]


# ── SiteFocus: cluster focus score ────────────────────────────────────────────

def _cluster_focus_score(kw_list: list[dict], anchor: str) -> float:
    """
    Measures how tight (focused) a cluster is around its anchor (SiteFocus proxy).
    High score = all keywords share the same differentiator → high topical focus.
    Low score = keywords are loosely related → cluster is diluting SiteFocus.

    Returns score in [0, 1].
    """
    if not kw_list:
        return 0.0
    anchor_toks = set(_tokenize(anchor))
    scores = []
    for kw in kw_list:
        kw_toks = set(_tokenize(kw["keyword"]))
        overlap = len(kw_toks & anchor_toks)
        score = overlap / max(1, len(anchor_toks))
        scores.append(min(1.0, score))
    return round(sum(scores) / len(scores), 3)


# ── Main clustering ────────────────────────────────────────────────────────────

def _cluster(
    keywords: list[dict],
    seed: str,
    max_clusters: int = 8,
    min_coherence: float = 0.0,  # SiteRadius cutoff — keywords below this are dropped
) -> list[dict]:
    """
    Groups keywords into topical clusters (pillar pages).

    SiteFocus strategy:
    - Only keep keywords with coherence >= min_coherence (prune high-SiteRadius outliers)
    - Score clusters by (keyword_count * focus_score * total_volume) — favours tight clusters
    - Merge clusters that are too similar (prefix overlap > 4 chars) to avoid duplication
    - Unassigned keywords go to their best-matching cluster by token overlap (not just biggest)
    """
    seed_toks = _seed_tokens(seed)

    # SiteRadius filter: remove semantic outliers (coherence < threshold)
    if min_coherence > 0:
        before = len(keywords)
        keywords = [k for k in keywords if _coherence_score(k["keyword"], seed_toks, seed) >= min_coherence]
        pruned = before - len(keywords)
        if pruned:
            logger.info(f"[TopicalMap] SiteRadius pruning: removed {pruned} outlier keywords (coherence < {min_coherence})")

    # Build differentiator index
    token_to_kws: dict[str, list] = defaultdict(list)
    kw_to_diffs: dict[str, list] = {}

    for kw in keywords:
        diffs = _differentiators(kw["keyword"], seed_toks)
        kw_to_diffs[kw["keyword"]] = diffs
        for d in diffs:
            token_to_kws[d].append(kw)

    # Score each differentiator token combining:
    # - breadth (how many keywords use it) → SiteFocus: more coverage = better
    # - volume (total search demand) → commercial value
    # - cluster_focus_score → how tight the cluster is (SiteFocus proxy)
    token_scores: dict[str, float] = {}
    for token, kws in token_to_kws.items():
        if len(kws) < 2:
            continue
        total_vol = sum(k.get("search_volume", 0) for k in kws)
        focus = _cluster_focus_score(kws, token)
        # SiteFocus-weighted score: focus score amplifies well-defined clusters
        token_scores[token] = (len(kws) * 2 + total_vol / 500) * (0.5 + focus)

    # Select top anchors, deduplicate via Jaccard similarity (not prefix-4)
    top_tokens = sorted(token_scores, key=lambda x: token_scores[x], reverse=True)

    selected_anchors: list[str] = []
    clusters: dict[str, list] = {}

    def _jaccard(a: str, b: str) -> float:
        sa, sb = set(a), set(b)
        inter = len(sa & sb)
        union = len(sa | sb)
        return inter / union if union else 0.0

    for token in top_tokens:
        if len(selected_anchors) >= max_clusters:
            break
        # Jaccard > 0.6 = too similar (covers inflections: urlop/urlopów, prawo/prawa)
        too_similar = any(
            _jaccard(token, a) > 0.6
            for a in selected_anchors
            if len(token) > 3 and len(a) > 3
        )
        if too_similar:
            continue
        selected_anchors.append(token)
        clusters[token] = []

    # Assign each keyword to best-matching cluster
    assigned: set[str] = set()
    for kw in sorted(keywords, key=lambda x: x.get("search_volume", 0), reverse=True):
        kw_text = kw["keyword"]
        if kw_text in assigned:
            continue
        diffs = kw_to_diffs.get(kw_text, [])
        best_anchor = None
        best_score = -1
        for anchor in selected_anchors:
            if anchor in diffs:
                score = token_scores.get(anchor, 0)
                if score > best_score:
                    best_score = score
                    best_anchor = anchor
        if best_anchor:
            clusters[best_anchor].append(kw)
            assigned.add(kw_text)

    # Unassigned keywords → best-matching cluster by token overlap
    # If zero overlap with all anchors → assign to largest cluster (safest fallback)
    unassigned = [kw for kw in keywords if kw["keyword"] not in assigned]
    if unassigned and selected_anchors:
        kw_toks_cache = {kw["keyword"]: set(_tokenize(kw["keyword"])) for kw in unassigned}
        anchor_toks_cache = {a: set(_tokenize(a)) for a in selected_anchors}
        largest_anchor = max(selected_anchors, key=lambda a: len(clusters[a]))
        for kw in unassigned:
            kw_toks = kw_toks_cache[kw["keyword"]]
            overlaps = {a: len(kw_toks & anchor_toks_cache[a]) for a in selected_anchors}
            max_overlap = max(overlaps.values())
            if max_overlap > 0:
                best_a = max(selected_anchors, key=lambda a: overlaps[a])
            else:
                best_a = largest_anchor  # zero overlap → safest fallback
            clusters[best_a].append(kw)

    # Merge tiny clusters (< 3 keywords) into the largest cluster
    MIN_CLUSTER_SIZE = 3
    if len(clusters) > 2:
        largest = max(clusters, key=lambda a: len(clusters[a]))
        tiny_anchors = [a for a in list(clusters) if a != largest and len(clusters[a]) < MIN_CLUSTER_SIZE]
        for ta in tiny_anchors:
            clusters[largest].extend(clusters[ta])
            del clusters[ta]
            if ta in selected_anchors:
                selected_anchors.remove(ta)

    # Build result with SiteFocus scores attached
    result = []
    for anchor, kws in clusters.items():
        if not kws:
            continue
        total_vol = sum(k.get("search_volume", 0) for k in kws)
        avg_diff = sum(k.get("keyword_difficulty", 0) for k in kws) / len(kws)
        focus_score = _cluster_focus_score(kws, anchor)

        # Label from top keyword in cluster (more natural than seed + anchor)
        top_kw = max(kws, key=lambda k: k.get("search_volume", 0))
        label = top_kw["keyword"].title()

        result.append({
            "anchor": anchor,
            "label": label,
            "keywords": kws,
            "total_volume": total_vol,
            "avg_difficulty": round(avg_diff, 1),
            "focus_score": focus_score,
        })

    return sorted(result, key=lambda x: x["total_volume"], reverse=True)


# ── Site-level SiteFocus / SiteRadius metrics ──────────────────────────────────

def _compute_site_metrics(pillars: list[dict], seed: str, all_keywords: list[dict]) -> dict:
    """
    Compute site-level SiteFocus and SiteRadius estimates for the topical map.

    SiteFocus  (0–1): average cluster focus_score weighted by volume.
                      1.0 = perfectly focused site, 0.0 = completely diluted.
    SiteRadius (0–1): average semantic drift across all keywords.
                      0.0 = all content close to core, 1.0 = high drift.
    Coverage   (int): total number of distinct topic facets (clusters) covered.

    These are shown in the UI to help the user understand topical authority potential.
    """
    seed_toks = _seed_tokens(seed)
    total_vol = sum(p["total_volume"] for p in pillars) or 1

    # SiteFocus: weighted average of cluster focus scores
    site_focus = sum(p["focus_score"] * p["total_volume"] for p in pillars) / total_vol

    # SiteRadius: average (1 - coherence) across all keywords
    coherence_scores = [_coherence_score(k["keyword"], seed_toks, seed) for k in all_keywords]
    avg_coherence = sum(coherence_scores) / len(coherence_scores) if coherence_scores else 0.0
    site_radius = round(1.0 - avg_coherence, 3)

    # Topical coverage depth: total supporting articles per pillar
    total_supporting = sum(len(p.get("supporting_keywords", [])) for p in pillars)

    return {
        "site_focus": round(site_focus, 3),      # 0–1, higher is better
        "site_radius": round(site_radius, 3),     # 0–1, lower is better
        "coverage": len(pillars),                  # number of topic facets
        "total_articles": len(pillars) + total_supporting,
        "focus_rating": (
            "excellent" if site_focus >= 0.7 else
            "good"      if site_focus >= 0.5 else
            "fair"      if site_focus >= 0.3 else
            "weak"
        ),
        "radius_rating": (
            "tight"    if site_radius <= 0.3 else
            "moderate" if site_radius <= 0.5 else
            "wide"     if site_radius <= 0.7 else
            "drifting"
        ),
    }


# ── Main entry point ───────────────────────────────────────────────────────────

async def generate_topical_map(
    seed: str,
    location_code: int = 2616,
    language_code: str = "pl",
    min_volume: int = 10,
    max_clusters: int = 8,
    dfs_login: str = "",
    dfs_password: str = "",
    force_refresh: bool = False,
    min_coherence: float = 0.0,  # SiteRadius filter threshold (0 = off, 0.1 = light prune)
) -> dict:
    """
    Generate topical map: pillar pages + supporting pages.
    Each pillar = a distinct topic facet of the seed (SiteFocus cluster).
    Results cached 7 days in SQLite (expensive DataForSEO calls).

    Returns site-level SiteFocus + SiteRadius metrics alongside the map.
    """
    cache_key = hashlib.md5(
        f"{seed.lower().strip()}:{location_code}:{language_code}:{min_volume}:{min_coherence}:{max_clusters}".encode()
    ).hexdigest()
    if not force_refresh:
        cached = await _map_cache_get(cache_key)
        if cached:
            logger.info(f"[TopicalMap] Cache hit for '{seed}'")
            return cached

    client = DataForSEOClient(dfs_login, dfs_password)

    # Fetch keywords in parallel (saves ~5-10s vs sequential)
    import asyncio as _asyncio
    raw = []
    results_parallel = await _asyncio.gather(
        client.keyword_suggestions(seed, location_code, language_code, 500),
        client.keyword_ideas(seed, location_code, language_code, 300),
        client.related_keywords(seed, location_code, language_code, 150),
        return_exceptions=True,
    )
    for kws, name in zip(results_parallel, ["suggestions", "ideas", "related"]):
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
    keywords = filtered if filtered else keywords
    logger.info(f"[TopicalMap] after volume filter (>={min_volume}): {len(keywords)}")

    # Add coherence score to each keyword (SiteRadius proxy)
    seed_toks = _seed_tokens(seed)
    for k in keywords:
        k["coherence"] = _coherence_score(k["keyword"], seed_toks, seed)

    # Cluster with SiteFocus-aware algorithm
    clusters = _cluster(keywords, seed, max_clusters, min_coherence)
    logger.info(f"[TopicalMap] clusters: {len(clusters)}")

    # Fallback: if only 1 cluster, retry with higher max
    if len(clusters) <= 1 and max_clusters < 15:
        clusters = _cluster(keywords, seed, 15, min_coherence)
        logger.info(f"[TopicalMap] retry with max_clusters=15: {len(clusters)}")

    # Pillar score: volume / (KD + 1) — favours rankable keywords over raw volume
    def _pillar_score(k):
        vol = k.get("search_volume", 0)
        kd = k.get("keyword_difficulty", 50)
        coherence = k.get("coherence", 0.5)
        return (vol / (kd + 1)) * (0.5 + coherence)

    # Build pillar structure
    pillars = []
    for cluster in clusters:
        # Separate by intent: informational keywords preferred for pillar pages
        informational = [k for k in cluster["keywords"] if k.get("intent", "informational") in ("informational", "")]

        pillar_candidates = informational if informational else cluster["keywords"]
        pillar_candidates_sorted = sorted(pillar_candidates, key=_pillar_score, reverse=True)
        pillar_kw = pillar_candidates_sorted[0] if pillar_candidates_sorted else {"keyword": cluster["anchor"], "search_volume": 0}

        # Supporting = everything except pillar, informational first
        supporting = [k for k in cluster["keywords"] if k["keyword"] != pillar_kw["keyword"]]

        # Sort supporting by: search_volume DESC, coherence DESC (high SiteRadius ones go last)
        supporting_sorted = sorted(
            supporting,
            key=lambda x: (x.get("search_volume", 0) * (0.5 + x.get("coherence", 0.5))),
            reverse=True
        )

        # Proportional limit: at least 10, at most 30, scaled to cluster size
        sup_limit = min(30, max(10, len(supporting)))
        pillars.append({
            "anchor": cluster["anchor"],
            "label": cluster["label"],
            "pillar_keyword": pillar_kw["keyword"],
            "pillar_volume": pillar_kw.get("search_volume", 0),
            "pillar_difficulty": pillar_kw.get("keyword_difficulty", 0),
            "pillar_coherence": round(pillar_kw.get("coherence", 0), 3),
            "focus_score": cluster["focus_score"],
            "pillar_intent": pillar_kw.get("intent", "informational"),
            "supporting_keywords": [
                {
                    "keyword": k["keyword"],
                    "search_volume": k.get("search_volume", 0),
                    "keyword_difficulty": k.get("keyword_difficulty", 0),
                    "coherence": round(k.get("coherence", 0), 3),
                    "intent": k.get("intent", "informational"),
                }
                for k in supporting_sorted[:sup_limit]
            ],
            "total_volume": cluster["total_volume"],
            "avg_difficulty": cluster["avg_difficulty"],
        })

    # Compute site-level SiteFocus / SiteRadius
    site_metrics = _compute_site_metrics(pillars, seed, keywords)
    logger.info(
        f"[TopicalMap] SiteFocus={site_metrics['site_focus']} ({site_metrics['focus_rating']}), "
        f"SiteRadius={site_metrics['site_radius']} ({site_metrics['radius_rating']}), "
        f"coverage={site_metrics['coverage']}"
    )

    # Force Graph
    nodes = [{"id": "seed", "label": seed, "type": "seed", "size": 24, "color": "#1a2332"}]
    links = []

    for i, p in enumerate(pillars):
        pid = f"pillar_{i}"
        # Node size reflects cluster volume; color intensity reflects focus_score
        focus = p.get("focus_score", 0.5)
        nodes.append({
            "id": pid,
            "label": p["label"],
            "type": "pillar",
            "size": 16,
            "color": "#1a73e8",
            "volume": p["total_volume"],
            "focus_score": focus,
        })
        links.append({"source": "seed", "target": pid, "strength": 1.0 + focus})

        for j, sk in enumerate(p["supporting_keywords"][:8]):
            sid = f"sup_{i}_{j}"
            nodes.append({
                "id": sid,
                "label": sk["keyword"],
                "type": "supporting",
                "size": 8,
                "color": "#4285f4",
                "volume": sk.get("search_volume", 0),
                "coherence": sk.get("coherence", 0),
            })
            links.append({"source": pid, "target": sid, "strength": sk.get("coherence", 0.5)})

    result = {
        "seed": seed,
        "total_keywords": len(keywords),
        "pillars": pillars,
        "nodes": nodes,
        "links": links,
        "site_metrics": site_metrics,
    }
    await _map_cache_set(cache_key, result)
    return result

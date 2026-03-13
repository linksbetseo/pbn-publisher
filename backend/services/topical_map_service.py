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
import asyncio
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


def _stem_pl(token: str) -> str:
    """Naive Polish suffix stripper — handles most common case inflections."""
    # FIX #27: expanded suffix list for better Polish stemming (added common noun/verb endings)
    for suffix in ("owania", "owego", "owej", "owym", "nych", "nego", "nemu",
                   "eniu", "anie", "enie", "ości",
                   "ach", "ami", "iem", "ego", "emu", "owi",
                   "ie", "ej", "ów", "ą", "ę"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[:-len(suffix)]
    return token


def _tokenize(text: str) -> list[str]:
    folded = _ascii_fold(text)
    return [_stem_pl(t) for t in folded.split() if t not in STOP_WORDS and len(t) > 2]


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
      containment    = bonus if seed is contained within keyword (compound queries)
      substring_bonus = partial credit for substring matches
      result = weighted combination
    """
    kw_toks = set(_tokenize(keyword))
    if not kw_toks:
        return 0.0
    overlap = len(kw_toks & seed_toks)
    overlap_ratio = overlap / len(kw_toks)
    # Partial credit: seed words appearing as substrings in keyword tokens
    substring_bonus = 0.2 if any(st in _ascii_fold(keyword) for st in seed_toks if len(st) > 3) else 0.0
    # Containment bonus: if the seed phrase is fully contained in the keyword
    # e.g. seed="prawo pracy" → keyword="prawo pracy urlop" → high relevance
    containment_bonus = 0.0
    seed_folded = _ascii_fold(seed)
    kw_folded = _ascii_fold(keyword)
    if seed_folded in kw_folded:
        containment_bonus = 0.3
    elif all(st in kw_folded for st in seed_folded.split() if len(st) > 2):
        containment_bonus = 0.15
    score = min(1.0, overlap_ratio + substring_bonus + containment_bonus)
    return round(score, 3)


def _differentiators(keyword: str, seed_toks: set) -> list[str]:
    """
    Tokens and bigrams that DIFFERENTIATE the keyword from the seed.
    Returns both single tokens and consecutive 2-token bigrams.
    Seed='prawo pracy', keyword='prawo pracy urlop macierzyński' → ['urlop', 'macierzynski', 'urlop macierzynski']
    Bigrams produce more precise cluster anchors (e.g. 'urlop macierzynski' vs just 'urlop').
    """
    tokens = _tokenize(keyword)
    diff_tokens = [t for t in tokens if t not in seed_toks]
    # Build bigrams from consecutive diff tokens in original order
    bigrams = []
    for i in range(len(tokens) - 1):
        a, b = tokens[i], tokens[i + 1]
        if a not in seed_toks and b not in seed_toks:
            bigrams.append(f"{a} {b}")
    return diff_tokens + bigrams


# ── SiteFocus: cluster focus score ────────────────────────────────────────────

def _cluster_focus_score(kw_list: list[dict], anchor: str) -> float:
    """
    Measures how tight (focused) a cluster is around its anchor (SiteFocus proxy).
    High score = all keywords share the same differentiator → high topical focus.
    Low score = keywords are loosely related → cluster is diluting SiteFocus.
    Supports both single-token and bigram anchors (e.g. 'urlop macierzynski').

    Returns score in [0, 1].
    """
    if not kw_list:
        return 0.0
    anchor_toks = set(anchor.split()) if " " in anchor else set(_tokenize(anchor))
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

    # Score each differentiator (token or bigram) combining:
    # - breadth (how many keywords use it) → SiteFocus: more coverage = better
    # - volume (total search demand) → commercial value
    # - cluster_focus_score → how tight the cluster is (SiteFocus proxy)
    # Bigrams get a 1.5x bonus because they produce more precise clusters
    token_scores: dict[str, float] = {}
    for token, kws in token_to_kws.items():
        if len(kws) < 2:
            continue
        total_vol = sum(k.get("search_volume", 0) for k in kws)
        focus = _cluster_focus_score(kws, token)
        # SiteFocus-weighted score: focus score amplifies well-defined clusters
        base_score = (len(kws) * 2 + total_vol / 500) * (0.5 + focus)
        # Bigram bonus: multi-token anchors are more specific → better SiteFocus
        bigram_bonus = 1.5 if " " in token else 1.0
        token_scores[token] = base_score * bigram_bonus

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
        # Check similarity: Jaccard on chars OR bigram subsumes single token
        too_similar = False
        for a in selected_anchors:
            # Bigram subsumption: "urlop macierzynski" subsumes "urlop"
            if " " in token and a in token.split():
                too_similar = True
                break
            if " " in a and token in a.split():
                too_similar = True
                break
            # Jaccard > 0.6 = too similar (covers inflections: urlop/urlopów, prawo/prawa)
            if len(token) > 3 and len(a) > 3 and _jaccard(token, a) > 0.6:
                too_similar = True
                break
        if too_similar:
            continue
        selected_anchors.append(token)
        clusters[token] = []

    # Assign each keyword to best-matching cluster
    # For bigram anchors: check if all bigram tokens appear in keyword's differentiators
    assigned: set[str] = set()
    for kw in sorted(keywords, key=lambda x: x.get("search_volume", 0), reverse=True):
        kw_text = kw["keyword"]
        if kw_text in assigned:
            continue
        diffs = kw_to_diffs.get(kw_text, [])
        diff_tokens_set = set(t for t in diffs if " " not in t)  # only single tokens
        best_anchor = None
        best_score = -1
        for anchor in selected_anchors:
            if " " in anchor:
                # Bigram anchor: all parts must be in diff tokens
                parts = anchor.split()
                if all(p in diff_tokens_set for p in parts):
                    score = token_scores.get(anchor, 0)
                    if score > best_score:
                        best_score = score
                        best_anchor = anchor
            else:
                # Single token anchor
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

    # Cluster completeness per pillar (content gap indicator)
    # A pillar with high supporting count relative to SERP competition = well-covered
    max_supporting = max((len(p.get("supporting_keywords", [])) for p in pillars), default=1) or 1
    cluster_completeness = [
        round(len(p.get("supporting_keywords", [])) / max_supporting, 2) for p in pillars
    ]
    avg_completeness = round(sum(cluster_completeness) / len(cluster_completeness), 3) if cluster_completeness else 0

    # FIX #28: Firefly-safe velocity — scale by total content needed, not just pillar count
    # New sites: 2-3/week, established (>50 total articles): up to 5/week
    _total_articles = len(pillars) + total_supporting
    if _total_articles > 50:
        recommended_weekly = min(5, max(3, len(pillars)))
    else:
        recommended_weekly = min(3, max(2, len(pillars)))

    return {
        "site_focus": round(site_focus, 3),      # 0–1, higher is better
        "site_radius": round(site_radius, 3),     # 0–1, lower is better
        "coverage": len(pillars),                  # number of topic facets
        "total_articles": len(pillars) + total_supporting,
        "avg_cluster_completeness": avg_completeness,  # 0–1, higher = fewer content gaps
        "recommended_weekly_velocity": recommended_weekly,  # Firefly-safe max articles/week
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
        "completeness_rating": (
            "comprehensive" if avg_completeness >= 0.7 else
            "good"          if avg_completeness >= 0.5 else
            "developing"    if avg_completeness >= 0.3 else
            "sparse"
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

    raw = []
    results_parallel = await asyncio.gather(
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

        # Intent distribution for this cluster
        all_cluster_kws = cluster["keywords"]
        intent_counts = Counter(k.get("intent", "informational") for k in all_cluster_kws)
        intent_dist = {intent: count for intent, count in intent_counts.most_common()}

        # FIX #26: proportional limit — at least 5, at most 25 (was 10-30, too many for small clusters)
        sup_limit = min(25, max(5, len(supporting)))
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
            "intent_distribution": intent_dist,
            # Content gap indicators — how many subtopics need coverage
            "content_gap": {
                "total_subtopics": len(supporting),
                "high_volume_gaps": len([k for k in supporting if k.get("search_volume", 0) >= 100]),
                "low_kd_opportunities": len([k for k in supporting if k.get("keyword_difficulty", 50) < 30]),
                "quick_wins": len([
                    k for k in supporting
                    if k.get("search_volume", 0) >= 50 and k.get("keyword_difficulty", 50) < 25
                ]),
            },
        })

    # Compute publishing priority score per pillar:
    # Formula: (volume / (KD + 1)) * coherence * focus — favours high-volume, low-KD, tightly-focused clusters
    for p in pillars:
        vol = p["total_volume"] or 1
        kd = p["avg_difficulty"] or 1
        coherence = p.get("pillar_coherence", 0.5)
        focus = p.get("focus_score", 0.5)
        p["priority_score"] = round((vol / (kd + 1)) * (0.5 + coherence) * (0.5 + focus), 1)

    # Sort pillars by priority_score DESC — publish highest-opportunity clusters first
    pillars.sort(key=lambda p: p["priority_score"], reverse=True)

    # Compute site-level SiteFocus / SiteRadius
    site_metrics = _compute_site_metrics(pillars, seed, keywords)
    logger.info(
        f"[TopicalMap] SiteFocus={site_metrics['site_focus']} ({site_metrics['focus_rating']}), "
        f"SiteRadius={site_metrics['site_radius']} ({site_metrics['radius_rating']}), "
        f"coverage={site_metrics['coverage']}"
    )

    # Cross-pillar interlinking map: compute pillar-to-pillar token similarity
    # Each pillar gets a list of related pillars it should link to
    pillar_token_sets = {}
    for i, p in enumerate(pillars):
        all_kws = [p["pillar_keyword"]] + [sk["keyword"] for sk in p["supporting_keywords"]]
        tokens = set()
        for kw_text in all_kws:
            tokens.update(_tokenize(kw_text))
        tokens -= seed_toks  # remove seed tokens, only differentiation matters
        pillar_token_sets[i] = tokens

    for i, p in enumerate(pillars):
        related = []
        for j, p2 in enumerate(pillars):
            if i == j:
                continue
            shared = pillar_token_sets[i] & pillar_token_sets[j]
            union = pillar_token_sets[i] | pillar_token_sets[j]
            similarity = len(shared) / len(union) if union else 0
            if similarity > 0.05:  # minimal topical overlap
                related.append({
                    "pillar_index": j,
                    "pillar_keyword": p2["pillar_keyword"],
                    "similarity": round(similarity, 3),
                })
        # Sort by similarity DESC, keep top 3 related pillars
        related.sort(key=lambda x: x["similarity"], reverse=True)
        p["related_pillars"] = related[:3]

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

    # Add cross-pillar links to force graph (dashed lines between related pillars)
    seen_pairs = set()
    for i, p in enumerate(pillars):
        for rel in p.get("related_pillars", []):
            j = rel["pillar_index"]
            pair = (min(i, j), max(i, j))
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                links.append({
                    "source": f"pillar_{i}",
                    "target": f"pillar_{j}",
                    "strength": rel["similarity"],
                    "type": "cross_pillar",
                })

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

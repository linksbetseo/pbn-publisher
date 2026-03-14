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

FIX-30: comprehensive rewrite addressing 30 identified issues.
"""
import asyncio
import hashlib
import json as _json
import logging
import re
import time
import unicodedata
import zlib
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
            raw = row[0]
            # FIX #29: support both compressed (bytes) and legacy plain JSON
            if isinstance(raw, bytes):
                try:
                    raw = zlib.decompress(raw).decode("utf-8")
                except zlib.error:
                    raw = raw.decode("utf-8")
            return _json.loads(raw)
    except Exception:
        pass
    return None


async def _map_cache_set(key: str, data: dict) -> None:
    """Store cache entry with zlib compression (FIX #29) and clean expired (FIX #28)."""
    await _ensure_map_cache_table()
    try:
        json_str = _json.dumps(data, ensure_ascii=False)
        # FIX #29: compress JSON before storing — typically 3-5x smaller
        compressed = zlib.compress(json_str.encode("utf-8"), level=6)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO topical_map_cache (cache_key, data_json, expires_at) VALUES (?,?,?)",
                (key, compressed, time.time() + _MAP_CACHE_TTL)
            )
            # FIX #28: clean expired cache entries to prevent unbounded growth
            await db.execute(
                "DELETE FROM topical_map_cache WHERE expires_at < ?", (time.time(),)
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"[MapCache] write failed: {e}")


# ── Stopwords ──────────────────────────────────────────────────────────────────

# FIX #3: expanded stopwords with pronouns, numerals, demonstratives
STOP_WORDS = {
    "i", "w", "z", "na", "do", "po", "o", "a", "się", "nie", "jak", "co",
    "czy", "że", "to", "jest", "są", "dla", "przez", "przy", "za", "od",
    "ile", "kiedy", "kto", "gdzie", "gdy", "bez", "lub", "oraz", "ale",
    "który", "która", "które", "tego", "tej", "ten", "ta", "te", "być",
    "mieć", "móc", "by", "też", "już", "jeszcze", "tylko", "właśnie",
    "np", "tzw", "itp", "wg",
    # pronouns / demonstratives
    "tym", "tych", "ich", "jego", "jej", "sam", "sama", "sobie", "swój",
    "każdy", "każda", "inne", "innych", "inny", "inna", "wszystkie", "wszystko",
    # numerals
    "jeden", "dwa", "trzy", "cztery", "pięć",
    # misc function words
    "więc", "jednak", "ponad", "między", "przed", "nad", "pod", "temu",
    "bardzo", "może", "nawet", "gdyż", "choć", "mimo", "aby", "żeby",
    "sie",  # ascii-folded version of "się"
}


# ── Text helpers ───────────────────────────────────────────────────────────────

def _ascii_fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(c for c in normalized if unicodedata.category(c) != "Mn").lower()


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


# FIX #4: ó→o normalization for Polish alternation (urlop/urlopów)
def _normalize_pl(token: str) -> str:
    """Normalize common Polish vowel alternations before stemming."""
    return token.replace("ó", "o").replace("ą", "a").replace("ę", "e")


# FIX #1: removed dangerous prefix stripping, only strip "nie" (negation) which is safe
# FIX #2: added adjectival suffixes -owy/-owa/-owe/-ski/-ska/-czny/-czna/-ny/-na
def _stem_pl(token: str) -> str:
    """Polish suffix stripper — handles case inflections and adjectival forms."""
    _core = _normalize_pl(token)

    # Only strip "nie" prefix (negation) — other prefixes are too destructive
    # "niebezpieczny" → "bezpieczny", but "naprawa" stays "naprawa"
    if _core.startswith("nie") and len(_core) >= 7:
        _core = _core[3:]

    for suffix in (
        # nominal/verbal long suffixes (longest first)
        "owania", "owego", "owej", "owym", "owych",
        "nych", "nego", "nemu",
        "eniu", "anie", "enie", "enia", "osci",
        # adjectival suffixes (FIX #2)
        "iczne", "iczny", "iczna", "owego", "cznym", "cznej",
        "czny", "czna", "czne",
        "skim", "skie", "skiej",
        "owym", "owej", "owe", "owy", "owa",
        "ski", "ska",
        # case suffixes (expanded for Polish declensions)
        "ach", "ami", "iem", "ego", "emu", "owi",
        "nia", "niu",
        "ie", "ej", "ow", "om",
        "cy", "ce", "ca",
        "tu", "ty", "ta",
        "ki", "ka", "ku", "ke",
        "ny", "na", "ne",
    ):
        if _core.endswith(suffix) and len(_core) - len(suffix) >= 3:
            return _core[:-len(suffix)]
    return _core


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

# FIX #10: coherence now uses weighted overlap that doesn't penalize long-tail keywords
def _coherence_score(keyword: str, seed_toks: set, seed: str) -> float:
    """
    Measures semantic proximity of a keyword to the seed (SiteRadius proxy).
    FIX #10: uses seed_coverage (how much of seed is in kw) instead of kw_coverage
    (how much of kw is seed). This stops penalizing informative long-tail phrases.
    """
    kw_toks = set(_tokenize(keyword))
    if not kw_toks or not seed_toks:
        return 0.0
    overlap = len(kw_toks & seed_toks)
    # How much of the SEED is covered by this keyword (not the other way around)
    seed_coverage = overlap / len(seed_toks)
    # Bonus for extra relevant tokens (long tail is fine if seed is covered)
    length_bonus = min(0.15, 0.03 * len(kw_toks - seed_toks)) if seed_coverage > 0.5 else 0.0
    # Partial credit: seed words appearing as substrings in keyword tokens
    substring_bonus = 0.15 if any(st in _ascii_fold(keyword) for st in seed_toks if len(st) > 3) else 0.0
    # Containment bonus: if the seed phrase is fully contained in the keyword
    containment_bonus = 0.0
    seed_folded = _ascii_fold(seed)
    kw_folded = _ascii_fold(keyword)
    if seed_folded in kw_folded:
        containment_bonus = 0.3
    elif all(st in kw_folded for st in seed_folded.split() if len(st) > 2):
        containment_bonus = 0.15
    score = min(1.0, seed_coverage + substring_bonus + containment_bonus + length_bonus)
    return round(score, 3)


def _differentiators(keyword: str, seed_toks: set) -> list[str]:
    """
    Tokens and bigrams that DIFFERENTIATE the keyword from the seed.
    """
    tokens = _tokenize(keyword)
    diff_tokens = [t for t in tokens if t not in seed_toks]
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
    min_coherence: float = 0.0,
) -> list[dict]:
    """
    Groups keywords into topical clusters (pillar pages).
    FIX #5: lowered singleton threshold to 1
    FIX #6: zero-overlap keywords go to highest-coherence cluster, not largest
    FIX #7: tiny cluster merge considers volume, not just count
    FIX #8: Jaccard always uses stemmed tokens, never raw chars
    FIX #9: focus_score recalculated after merge
    FIX #30: uses pre-computed coherence from kw dict instead of recalculating
    """
    seed_toks = _seed_tokens(seed)

    # FIX #30: SiteRadius filter uses pre-computed coherence
    if min_coherence > 0:
        before = len(keywords)
        keywords = [k for k in keywords if k.get("coherence", 0) >= min_coherence]
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

    # FIX #11: normalize volume by median instead of magic /500
    all_volumes = [k.get("search_volume", 0) for k in keywords]
    all_volumes.sort()
    median_vol = all_volumes[len(all_volumes) // 2] if all_volumes else 100
    median_vol = max(median_vol, 10)  # floor

    token_scores: dict[str, float] = {}
    for token, kws in token_to_kws.items():
        # FIX #5: allow singletons with high volume (was < 2, now < 1 never triggers; instead score them lower)
        if len(kws) < 1:
            continue
        total_vol = sum(k.get("search_volume", 0) for k in kws)
        focus = _cluster_focus_score(kws, token)
        # FIX #11: volume normalized to median
        base_score = (len(kws) * 2 + total_vol / median_vol) * (0.5 + focus)
        # Singleton penalty: single-keyword anchors score lower
        if len(kws) == 1:
            base_score *= 0.3
        bigram_bonus = 1.5 if " " in token else 1.0
        token_scores[token] = base_score * bigram_bonus

    top_tokens = sorted(token_scores, key=lambda x: token_scores[x], reverse=True)

    selected_anchors: list[str] = []
    clusters: dict[str, list] = {}

    # FIX #8: Jaccard always on stemmed tokens, never on raw chars
    def _jaccard(a: str, b: str) -> float:
        sa = set(_tokenize(a))
        sb = set(_tokenize(b))
        if not sa or not sb:
            return 0.0
        inter = len(sa & sb)
        union = len(sa | sb)
        return inter / union if union else 0.0

    for token in top_tokens:
        if len(selected_anchors) >= max_clusters:
            break
        too_similar = False
        for a in selected_anchors:
            if " " in token and a in token.split():
                too_similar = True
                break
            if " " in a and token in a.split():
                too_similar = True
                break
            if _jaccard(token, a) > 0.6:
                too_similar = True
                break
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
        diff_tokens_set = set(t for t in diffs if " " not in t)
        best_anchor = None
        best_score = -1
        for anchor in selected_anchors:
            if " " in anchor:
                parts = anchor.split()
                if all(p in diff_tokens_set for p in parts):
                    score = token_scores.get(anchor, 0)
                    if score > best_score:
                        best_score = score
                        best_anchor = anchor
            else:
                if anchor in diffs:
                    score = token_scores.get(anchor, 0)
                    if score > best_score:
                        best_score = score
                        best_anchor = anchor
        if best_anchor:
            clusters[best_anchor].append(kw)
            assigned.add(kw_text)

    # FIX #6: unassigned keywords go to highest-coherence cluster, not largest
    unassigned = [kw for kw in keywords if kw["keyword"] not in assigned]
    if unassigned and selected_anchors:
        kw_toks_cache = {kw["keyword"]: set(_tokenize(kw["keyword"])) for kw in unassigned}
        anchor_toks_cache = {a: set(_tokenize(a)) for a in selected_anchors}
        # Fallback: cluster with highest avg coherence (not largest)
        avg_coherence_by_anchor = {}
        for a in selected_anchors:
            coh_vals = [k.get("coherence", 0) for k in clusters[a]]
            avg_coherence_by_anchor[a] = sum(coh_vals) / len(coh_vals) if coh_vals else 0
        best_coherence_anchor = max(selected_anchors, key=lambda a: avg_coherence_by_anchor[a])

        for kw in unassigned:
            kw_toks = kw_toks_cache[kw["keyword"]]
            overlaps = {a: len(kw_toks & anchor_toks_cache[a]) for a in selected_anchors}
            max_overlap = max(overlaps.values())
            if max_overlap > 0:
                best_a = max(selected_anchors, key=lambda a: overlaps[a])
            else:
                best_a = best_coherence_anchor
            clusters[best_a].append(kw)

    # FIX #7: merge considers volume, not just count
    # FIX #9: focus_score recalculated after merge
    MIN_CLUSTER_VOL = 50  # volume threshold instead of count
    MIN_CLUSTER_SIZE = 2
    if len(clusters) > 2:
        def _should_merge(anchor: str) -> bool:
            kws = clusters[anchor]
            total_vol = sum(k.get("search_volume", 0) for k in kws)
            return len(kws) < MIN_CLUSTER_SIZE and total_vol < MIN_CLUSTER_VOL

        tiny_anchors = [a for a in list(clusters) if _should_merge(a)]
        remaining_anchors = [a for a in selected_anchors if a not in tiny_anchors]
        if remaining_anchors:
            for ta in tiny_anchors:
                ta_toks = set(_tokenize(ta))
                best_target = remaining_anchors[0]
                best_overlap = -1
                for ra in remaining_anchors:
                    ra_toks = set(_tokenize(ra))
                    overlap = len(ta_toks & ra_toks)
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_target = ra
                clusters[best_target].extend(clusters[ta])
                del clusters[ta]
                if ta in selected_anchors:
                    selected_anchors.remove(ta)

    # Build result — FIX #9: recalculate focus_score after merges
    result = []
    for anchor, kws in clusters.items():
        if not kws:
            continue
        total_vol = sum(k.get("search_volume", 0) for k in kws)
        avg_diff = sum(k.get("keyword_difficulty", 0) for k in kws) / len(kws)
        focus_score = _cluster_focus_score(kws, anchor)
        avg_cpc = sum(k.get("cpc", 0) for k in kws) / len(kws)

        top_kw = max(kws, key=lambda k: k.get("search_volume", 0))
        label = top_kw["keyword"].title()

        result.append({
            "anchor": anchor,
            "label": label,
            "keywords": kws,
            "total_volume": total_vol,
            "avg_difficulty": round(avg_diff, 1),
            "avg_cpc": round(avg_cpc, 2),
            "focus_score": focus_score,
        })

    return sorted(result, key=lambda x: x["total_volume"], reverse=True)


# ── Site-level SiteFocus / SiteRadius metrics ──────────────────────────────────

# FIX #30: uses pre-computed coherence from kw dict
def _compute_site_metrics(pillars: list[dict], seed: str, all_keywords: list[dict]) -> dict:
    total_vol = sum(p["total_volume"] for p in pillars) or 1

    site_focus = sum(p["focus_score"] * p["total_volume"] for p in pillars) / total_vol

    # FIX #30: use pre-computed coherence
    coherence_scores = [k.get("coherence", 0) for k in all_keywords]
    avg_coherence = sum(coherence_scores) / len(coherence_scores) if coherence_scores else 0.0
    site_radius = round(1.0 - avg_coherence, 3)

    total_supporting = sum(len(p.get("supporting_keywords", [])) for p in pillars)

    max_supporting = max((len(p.get("supporting_keywords", [])) for p in pillars), default=1) or 1
    cluster_completeness = [
        round(len(p.get("supporting_keywords", [])) / max_supporting, 2) for p in pillars
    ]
    avg_completeness = round(sum(cluster_completeness) / len(cluster_completeness), 3) if cluster_completeness else 0

    _total_articles = len(pillars) + total_supporting
    if _total_articles > 50:
        recommended_weekly = min(5, max(3, len(pillars)))
    else:
        recommended_weekly = min(3, max(2, len(pillars)))

    return {
        "site_focus": round(site_focus, 3),
        "site_radius": round(site_radius, 3),
        "coverage": len(pillars),
        "total_articles": len(pillars) + total_supporting,
        "avg_cluster_completeness": avg_completeness,
        "recommended_weekly_velocity": recommended_weekly,
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

async def _gpt_relevance_filter(
    keywords: list[dict],
    seed: str,
    site_description: str,
    domain_url: str = "",
    language_code: str = "pl",
    batch_size: int = 40,
) -> list[dict]:
    """
    GPT-based semantic relevance filter.
    Sends keyword batches to GPT and asks which ones are relevant
    to the seed topic (and optionally domain's business).
    Always runs — uses seed alone if no site_description provided.
    Returns only the relevant keywords.
    """
    from openai import AsyncOpenAI as _AO
    from services.openai_service import get_gpt_model
    from config import OPENAI_API_KEY

    client = _AO(api_key=OPENAI_API_KEY)
    model = await get_gpt_model()
    logger.info(f"[TopicalMap] GPT relevance filter starting: {len(keywords)} keywords, model={model}, seed='{seed}'")

    is_pl = language_code == "pl"

    # Build context: use site_description if available, otherwise seed is the context
    if site_description:
        context_block = f"DOMENA: {domain_url}\nOPIS BIZNESU: {site_description}" if is_pl else f"DOMAIN: {domain_url}\nBUSINESS: {site_description}"
    elif domain_url:
        context_block = f"DOMENA: {domain_url}" if is_pl else f"DOMAIN: {domain_url}"
    else:
        context_block = ""

    from datetime import datetime
    current_year = datetime.now().year

    system_prompt = (
        "Jesteś ekspertem SEO. Oceniasz trafność fraz kluczowych dla topical map.\n\n"
        f"TEMAT (SEED): {seed}\n"
        f"{context_block}\n"
        f"ROK: {current_year}\n\n"
        "Fraza jest TRAFNA (1) jeśli:\n"
        "- Dotyczy DOKŁADNIE tego tematu co seed\n"
        "- Osoba szukająca tego seeda mogłaby naturalnie szukać też tej frazy\n"
        "- Jest przydatna dla bloga/strony o tym temacie\n"
        f"- Jeśli zawiera rok — tylko {current_year} lub {current_year+1} są aktualne\n\n"
        "Fraza jest NIETRAFNA (0) jeśli:\n"
        "- Dotyczy zupełnie innego tematu (np. seed='muzyka country' a fraza='piramidy finansowe')\n"
        "- Zawiera słowa z seeda ale w innym kontekście (np. seed='kamień naturalny' a fraza='parafia kamień')\n"
        "- Jest o nazwie miejsca, grze, filmie, medycynie, sporcie — niezwiązanej z tematem seeda\n"
        "- Tylko przypadkowo zawiera te same słowa co seed\n"
        f"- Zawiera przestarzały rok (2018, 2019, 2020, 2021, 2022, 2023, 2024) — odrzuć\n"
        "- Jest hasłem krzyżówkowym, pytaniem quizowym, lub encyklopedycznym niezwiązanym z tematem\n\n"
        "Odpowiedz TYLKO tablicą JSON z 1 i 0, np: [1,1,0,1,0]\n"
        "Bez wyjaśnień, bez dodatkowego tekstu."
    ) if is_pl else (
        "You are an SEO expert evaluating keyword relevance for a topical map.\n\n"
        f"TOPIC (SEED): {seed}\n"
        f"{context_block}\n"
        f"YEAR: {current_year}\n\n"
        "A keyword is RELEVANT (1) if:\n"
        "- It's directly about the seed topic\n"
        "- Someone interested in the seed would also search for it\n"
        "- It's useful for a blog/site about this topic\n"
        f"- If it contains a year, only {current_year} or {current_year+1} are current\n\n"
        "A keyword is IRRELEVANT (0) if:\n"
        "- It's about a completely different topic\n"
        "- It contains seed words but in a different context (e.g., place names, games, movies)\n"
        "- It only accidentally shares words with the seed\n"
        "- It contains an outdated year (2018-2024) — reject\n"
        "- It's a crossword clue, quiz question, or unrelated encyclopedic query\n\n"
        "Respond ONLY with a JSON array of 1s and 0s, e.g: [1,1,0,1,0]\n"
        "No explanations, no extra text."
    )

    # Pre-compute seed tokens for coherence fallback
    seed_toks = _seed_tokens(seed)

    relevant = []
    for i in range(0, len(keywords), batch_size):
        batch = keywords[i:i + batch_size]
        kw_list = "\n".join(f"{j+1}. {kw['keyword']}" for j, kw in enumerate(batch))
        batch_num = i // batch_size + 1

        gpt_ok = False
        for attempt in range(5):
            try:
                logger.info(f"[TopicalMap] GPT relevance batch {batch_num} attempt {attempt+1}, {len(batch)} keywords")
                resp = await client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": kw_list},
                    ],
                    temperature=0.0,
                    max_tokens=len(batch) * 3 + 20,
                )
                text = resp.choices[0].message.content.strip()
                logger.info(f"[TopicalMap] GPT response batch {batch_num}: {text[:200]}")

                # Parse JSON array from response
                match = re.search(r'\[[\d\s,]+\]', text)
                if match:
                    verdicts = _json.loads(match.group())
                else:
                    # Fallback: try to parse individual numbers
                    verdicts = [int(c) for c in text if c in '01']

                if len(verdicts) >= len(batch):
                    verdicts = verdicts[:len(batch)]
                elif len(verdicts) < len(batch):
                    logger.warning(f"[TopicalMap] GPT returned {len(verdicts)} verdicts for batch of {len(batch)} — padding with REJECT (0)")
                    verdicts.extend([0] * (len(batch) - len(verdicts)))

                kept = 0
                for kw, v in zip(batch, verdicts):
                    if v == 1:
                        relevant.append(kw)
                        kept += 1

                logger.info(f"[TopicalMap] GPT relevance batch {batch_num}: {kept}/{len(batch)} kept")
                gpt_ok = True
                break
            except Exception as e:
                logger.warning(f"[TopicalMap] GPT relevance batch {batch_num} attempt {attempt+1} failed: {e}")
                if attempt == 4:
                    # GPT completely failed — use coherence-based fallback instead of keeping everything
                    logger.warning(f"[TopicalMap] GPT filter failed for batch {batch_num}, using coherence fallback (>=0.15)")
                    for kw in batch:
                        score = _coherence_score(kw["keyword"], seed_toks, seed)
                        if score >= 0.15:
                            relevant.append(kw)
                else:
                    import random
                    await asyncio.sleep(2 ** attempt + random.uniform(0, 1))

        # Small delay between batches to avoid rate limits
        if i + batch_size < len(keywords):
            await asyncio.sleep(1.0)

    logger.info(f"[TopicalMap] GPT relevance filter: {len(relevant)}/{len(keywords)} keywords kept")
    return relevant


async def generate_topical_map(
    seed: str,
    location_code: int = 2616,
    language_code: str = "pl",
    min_volume: int = 10,
    max_clusters: int = 8,
    dfs_login: str = "",
    dfs_password: str = "",
    force_refresh: bool = False,
    min_coherence: float = 0.0,
    competitor_domain: str = "",
    domain_url: str = "",
    site_description: str = "",
) -> dict:
    """
    Generate topical map: pillar pages + supporting pages.
    FIX #17: competitor_domain — check what domain already ranks for.
    FIX #25/#26: force_refresh and min_coherence now exposed via API.
    FIX #14: monthly_searches trend data extracted from DataForSEO.
    FIX #12: CPC used in priority scoring.
    FIX #13: unified pillar_score formula used everywhere.
    """
    # Include domain context + filter version in cache key
    # v2: GPT relevance filter always runs (invalidates all pre-filter caches)
    _desc_hash = hashlib.md5(site_description.encode()).hexdigest()[:8] if site_description else ""
    cache_key = hashlib.md5(
        f"v2:{seed.lower().strip()}:{location_code}:{language_code}:{min_volume}:{min_coherence}:{max_clusters}:{competitor_domain}:{domain_url}:{_desc_hash}".encode()
    ).hexdigest()
    if not force_refresh:
        cached = await _map_cache_get(cache_key)
        if cached:
            logger.info(f"[TopicalMap] Cache hit for '{seed}'")
            return cached

    client = DataForSEOClient(dfs_login, dfs_password)

    raw = []
    # FIX #16: related_keywords depth increased to 2
    # FIX #17: optionally fetch keywords_for_site in parallel
    coros = [
        client.keyword_suggestions(seed, location_code, language_code, 500),
        client.keyword_ideas(seed, location_code, language_code, 300),
        client.related_keywords(seed, location_code, language_code, 150),
    ]
    coro_names = ["suggestions", "ideas", "related"]
    if competitor_domain:
        coros.append(client.keywords_for_site(competitor_domain, location_code, language_code, 200))
        coro_names.append("keywords_for_site")

    results_parallel = await asyncio.gather(*coros, return_exceptions=True)

    # FIX #17: build set of keywords the competitor already ranks for
    competitor_kws: dict[str, int] = {}  # keyword → position
    for kws, name in zip(results_parallel, coro_names):
        if isinstance(kws, Exception):
            logger.warning(f"[TopicalMap] {name} failed: {kws}")
        elif name == "keywords_for_site":
            for k in kws:
                competitor_kws[_ascii_fold(_clean(k["keyword"]))] = k.get("position", 0)
            logger.info(f"[TopicalMap] keywords_for_site: {len(kws)} (domain: {competitor_domain})")
        else:
            raw.extend(kws)
            logger.info(f"[TopicalMap] {name}: {len(kws)}")

    if not raw:
        raise ValueError(f"Brak wyników DataForSEO dla frazy: {seed}")

    keywords = _dedupe(raw)
    logger.info(f"[TopicalMap] after dedupe: {len(keywords)}")

    filtered = [k for k in keywords if k.get("search_volume", 0) >= min_volume]
    keywords = filtered if filtered else keywords
    logger.info(f"[TopicalMap] after volume filter (>={min_volume}): {len(keywords)}")

    # GPT relevance filter — removes keywords semantically irrelevant to the topic/domain
    # Always runs: uses site_description if available, otherwise seed alone is enough context
    keywords = await _gpt_relevance_filter(
        keywords, seed, site_description, domain_url, language_code,
    )
    logger.info(f"[TopicalMap] after GPT relevance filter: {len(keywords)}")

    # Add coherence score to each keyword (computed once, reused everywhere — FIX #30)
    # FIX #17: mark keywords the competitor domain already ranks for
    seed_toks = _seed_tokens(seed)
    for k in keywords:
        k["coherence"] = _coherence_score(k["keyword"], seed_toks, seed)
        kw_key = _ascii_fold(_clean(k["keyword"]))
        if kw_key in competitor_kws:
            k["already_ranking"] = True
            k["current_position"] = competitor_kws[kw_key]
        else:
            k["already_ranking"] = False
            k["current_position"] = 0

    clusters = _cluster(keywords, seed, max_clusters, min_coherence)
    logger.info(f"[TopicalMap] clusters: {len(clusters)}")

    if len(clusters) <= 1 and max_clusters < 15:
        clusters = _cluster(keywords, seed, 15, min_coherence)
        logger.info(f"[TopicalMap] retry with max_clusters=15: {len(clusters)}")

    # FIX #13: unified pillar score formula used for both pillar selection and priority
    def _pillar_score(k):
        vol = k.get("search_volume", 0)
        kd = k.get("keyword_difficulty", 50)
        coherence = k.get("coherence", 0.5)
        cpc = k.get("cpc", 0)
        # FIX #12: CPC bonus for commercially valuable keywords
        cpc_bonus = 1.0 + min(0.3, cpc / 10.0) if cpc > 0 else 1.0
        return (vol / (kd + 1)) * (0.5 + coherence) * cpc_bonus

    pillars = []
    for cluster in clusters:
        informational = [k for k in cluster["keywords"] if k.get("intent", "informational") in ("informational", "")]

        pillar_candidates = informational if informational else cluster["keywords"]
        pillar_candidates_sorted = sorted(pillar_candidates, key=_pillar_score, reverse=True)
        pillar_kw = pillar_candidates_sorted[0] if pillar_candidates_sorted else {"keyword": cluster["anchor"], "search_volume": 0}

        supporting = [k for k in cluster["keywords"] if k["keyword"] != pillar_kw["keyword"]]

        supporting_sorted = sorted(
            supporting,
            key=lambda x: (x.get("search_volume", 0) * (0.5 + x.get("coherence", 0.5))),
            reverse=True
        )

        all_cluster_kws = cluster["keywords"]
        intent_counts = Counter(k.get("intent", "informational") for k in all_cluster_kws)
        intent_dist = {intent: count for intent, count in intent_counts.most_common()}

        sup_limit = min(25, max(5, len(supporting)))

        # FIX #14: detect trend from monthly_searches
        def _detect_trend(k: dict) -> str:
            ms = k.get("monthly_searches", [])
            if not ms or len(ms) < 3:
                return "stable"
            recent = sum(m.get("search_volume", 0) for m in ms[:3])
            older = sum(m.get("search_volume", 0) for m in ms[3:6]) if len(ms) >= 6 else recent
            if older == 0:
                return "new" if recent > 0 else "stable"
            ratio = recent / older
            if ratio > 1.3:
                return "rising"
            elif ratio < 0.7:
                return "declining"
            return "stable"

        # FIX #17: cannibalization warnings per cluster
        cannibal_count = sum(1 for k in all_cluster_kws if k.get("already_ranking"))

        pillars.append({
            "anchor": cluster["anchor"],
            "label": cluster["label"],
            "pillar_keyword": pillar_kw["keyword"],
            "pillar_volume": pillar_kw.get("search_volume", 0),
            "pillar_difficulty": pillar_kw.get("keyword_difficulty", 0),
            "pillar_coherence": round(pillar_kw.get("coherence", 0), 3),
            "pillar_cpc": round(pillar_kw.get("cpc", 0), 2),
            "pillar_trend": _detect_trend(pillar_kw),
            "pillar_already_ranking": pillar_kw.get("already_ranking", False),
            "pillar_current_position": pillar_kw.get("current_position", 0),
            "focus_score": cluster["focus_score"],
            "pillar_intent": pillar_kw.get("intent", "informational"),
            "supporting_keywords": [
                {
                    "keyword": k["keyword"],
                    "search_volume": k.get("search_volume", 0),
                    "keyword_difficulty": k.get("keyword_difficulty", 0),
                    "coherence": round(k.get("coherence", 0), 3),
                    "intent": k.get("intent", "informational"),
                    "cpc": round(k.get("cpc", 0), 2),
                    "trend": _detect_trend(k),
                    "already_ranking": k.get("already_ranking", False),
                    "current_position": k.get("current_position", 0),
                }
                for k in supporting_sorted[:sup_limit]
            ],
            "total_volume": cluster["total_volume"],
            "avg_difficulty": cluster["avg_difficulty"],
            "avg_cpc": cluster.get("avg_cpc", 0),
            "intent_distribution": intent_dist,
            "cannibalization_risk": cannibal_count,
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

    # FIX #13: unified priority score (same formula as pillar selection + intent weight)
    _intent_weight = {"informational": 1.2, "": 1.0, "commercial": 0.8, "transactional": 0.7, "navigational": 0.6}
    for p in pillars:
        vol = p["total_volume"] or 1
        kd = p["avg_difficulty"] or 1
        coherence = p.get("pillar_coherence", 0.5)
        focus = p.get("focus_score", 0.5)
        cpc = p.get("pillar_cpc", 0)
        _iw = _intent_weight.get(p.get("pillar_intent", "informational"), 1.0)
        # FIX #12: CPC in priority score
        cpc_bonus = 1.0 + min(0.3, cpc / 10.0) if cpc > 0 else 1.0
        p["priority_score"] = round((vol / (kd + 1)) * (0.5 + coherence) * (0.5 + focus) * _iw * cpc_bonus, 1)

    pillars.sort(key=lambda p: p["priority_score"], reverse=True)

    site_metrics = _compute_site_metrics(pillars, seed, keywords)
    logger.info(
        f"[TopicalMap] SiteFocus={site_metrics['site_focus']} ({site_metrics['focus_rating']}), "
        f"SiteRadius={site_metrics['site_radius']} ({site_metrics['radius_rating']}), "
        f"coverage={site_metrics['coverage']}"
    )

    # Cross-pillar interlinking
    pillar_token_sets = {}
    for i, p in enumerate(pillars):
        all_kws = [p["pillar_keyword"]] + [sk["keyword"] for sk in p["supporting_keywords"]]
        tokens = set()
        for kw_text in all_kws:
            tokens.update(_tokenize(kw_text))
        tokens -= seed_toks
        pillar_token_sets[i] = tokens

    for i, p in enumerate(pillars):
        related = []
        for j, p2 in enumerate(pillars):
            if i == j:
                continue
            shared = pillar_token_sets[i] & pillar_token_sets[j]
            union = pillar_token_sets[i] | pillar_token_sets[j]
            similarity = len(shared) / len(union) if union else 0
            if similarity > 0.05:
                related.append({
                    "pillar_index": j,
                    "pillar_keyword": p2["pillar_keyword"],
                    "label": p2["label"],
                    "similarity": round(similarity, 3),
                })
        related.sort(key=lambda x: x["similarity"], reverse=True)
        p["related_pillars"] = related[:3]

    # Force Graph
    nodes = [{"id": "seed", "label": seed, "type": "seed", "size": 24, "color": "#1a2332"}]
    links = []

    for i, p in enumerate(pillars):
        pid = f"pillar_{i}"
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

        for j, sk in enumerate(p["supporting_keywords"][:10]):
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

    # FIX #17: cannibalization summary
    cannibal_summary = None
    if competitor_domain:
        total_ranking = sum(1 for k in keywords if k.get("already_ranking"))
        top10_ranking = sum(1 for k in keywords if k.get("already_ranking") and k.get("current_position", 99) <= 10)
        cannibal_summary = {
            "domain": competitor_domain,
            "total_already_ranking": total_ranking,
            "in_top10": top10_ranking,
            "total_checked": len(keywords),
            "coverage_pct": round(total_ranking / len(keywords) * 100, 1) if keywords else 0,
        }

    result = {
        "seed": seed,
        "total_keywords": len(keywords),
        "pillars": pillars,
        "nodes": nodes,
        "links": links,
        "site_metrics": site_metrics,
        "competitor_analysis": cannibal_summary,
    }
    await _map_cache_set(cache_key, result)
    return result

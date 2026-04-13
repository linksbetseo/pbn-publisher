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

# ── Strategy presets ──────────────────────────────────────────────────────────
# Each strategy overrides defaults for generate_topical_map.
# The caller can still override individual params on top of the preset.
STRATEGY_PRESETS: dict[str, dict] = {
    # Szeroka mapa — dużo pillars, luźna semantyka.
    # Dla nowych domen, które chcą budować widoczność szeroko.
    "breadth": {
        "max_clusters": 10,
        "min_coherence": 0.05,
        "min_volume": 30,
        "_description": "Szeroka mapa: dużo pillars, niska kohezja — dla nowych domen.",
        "_pillar_min_word_count": None,  # no restriction
        "_quick_wins_only": False,
    },
    # Głęboka mapa — mało pillars, dużo supporting.
    # Dla domen, które chcą budować autorytet w jednej niszy.
    "depth": {
        "max_clusters": 5,
        "min_coherence": 0.25,
        "min_volume": 20,
        "_description": "Głęboka mapa: mało pillars, gęste supporting — dla autorytatywnych domen.",
        "_pillar_min_word_count": None,
        "_quick_wins_only": False,
    },
    # Gap analysis vs. konkurent — wymaga competitor_domain.
    # Skupia się na keyword gaps: frazy których konkurent NIE ma.
    "competitor_gap": {
        "max_clusters": 8,
        "min_coherence": 0.10,
        "min_volume": 50,
        "_description": "Gap analysis: frazy których konkurent nie pokrywa. Wymaga competitor_domain.",
        "_pillar_min_word_count": None,
        "_quick_wins_only": False,
    },
    # Tylko łatwe frazy — niski KD, przyzwoity wolumen.
    # Szybkie rankingi dla nowych lub słabych domen.
    "quick_wins": {
        "max_clusters": 6,
        "min_coherence": 0.10,
        "min_volume": 100,
        "_description": "Szybkie rankingi: tylko frazy z niskim KD i solidnym wolumenem.",
        "_pillar_min_word_count": None,
        "_quick_wins_only": True,   # extra KD filter applied post-clustering
    },
    # Pełne pokrycie tematu — ścisła semantyka, wszystkie sub-tematy.
    # Dla E-E-A-T / YMYL, gdzie trzeba pokazać topical authority.
    "topical_authority": {
        "max_clusters": 12,
        "min_coherence": 0.20,
        "min_volume": 20,
        "_description": "Pełne pokrycie: ścisła semantyka, wszystkie sub-tematy — dla E-E-A-T/YMYL.",
        "_pillar_min_word_count": None,
        "_quick_wins_only": False,
    },
}

_STRATEGY_INTERNAL_KEYS = {"_description", "_pillar_min_word_count", "_quick_wins_only"}

def apply_strategy(strategy: str, kwargs: dict) -> dict:
    """
    Merge strategy preset into kwargs dict.
    Caller-supplied values take precedence over preset defaults.
    Returns updated kwargs + adds '_strategy_meta' key.
    """
    if not strategy or strategy == "default":
        kwargs.setdefault("_strategy_meta", {"name": "default", "description": "Domyślna mapa bez presetu."})
        return kwargs
    preset = STRATEGY_PRESETS.get(strategy)
    if not preset:
        raise ValueError(f"Nieznana strategia: '{strategy}'. Dostępne: {list(STRATEGY_PRESETS)}")
    meta = {
        "name": strategy,
        "description": preset.get("_description", ""),
        "quick_wins_only": preset.get("_quick_wins_only", False),
    }
    for k, v in preset.items():
        if k not in _STRATEGY_INTERNAL_KEYS:
            kwargs.setdefault(k, v)  # preset fills in only if not already set
    kwargs["_strategy_meta"] = meta
    return kwargs


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
    except Exception as e:
        logger.debug(f"[TopicalMap] Cache read failed: {e}")
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


# ── Semantic clustering via GPT ───────────────────────────────────────────────

async def _cluster_semantic(
    keywords: list[dict],
    seed: str,
    max_clusters: int = 8,
    language_code: str = "pl",
    min_coherence: float = 0.0,
) -> list[dict]:
    """
    GPT-based semantic clustering — groups keywords by real sub-topics, not token differentiators.
    Sends top-80 keywords to GPT, asks for sub-topic grouping, maps back to full keyword dicts.
    Falls back to _cluster() on any GPT failure.
    """
    from openai import AsyncOpenAI as _AO
    from services.openai_service import get_gpt_model
    from config import OPENAI_API_KEY

    if len(keywords) < 20:
        return _cluster(keywords, seed, max_clusters, min_coherence)

    client = _AO(api_key=OPENAI_API_KEY)
    model = await get_gpt_model()
    is_pl = language_code == "pl"

    # Use top-80 by volume for GPT clustering (rest assigned by token proximity)
    top_kws = sorted(keywords, key=lambda k: k.get("search_volume", 0), reverse=True)[:80]
    kw_lines = "\n".join(f"{i+1}. {k['keyword']}" for i, k in enumerate(top_kws))

    if is_pl:
        system = (
            f"Jesteś ekspertem SEO tworzącym topical authority map.\n"
            f"SEED (główny temat): {seed}\n\n"
            f"Podziel poniższe frazy na maksymalnie {max_clusters} semantycznych sub-tematów.\n"
            f"Każdy sub-temat = osobny artykuł-pillar na stronie.\n\n"
            f"ZASADY GRUPOWANIA:\n"
            f"- Grupuj po RZECZYWISTYM aspekcie tematu: 'konserwacja', 'rodzaje', 'jak wybrać', 'cena', 'dla kogo'\n"
            f"- NIE grupuj po modifierach: 'tani', 'online', 'ranking', 'najlepszy', '2024'\n"
            f"- Każdy cluster_name to krótka nazwa sub-tematu (2-4 słowa), nie fraza kluczowa\n"
            f"- Frazy niepasujące do żadnego sub-tematu wrzuć do 'inne'\n\n"
            f"Odpowiedz TYLKO valid JSON:\n"
            f'[{{"cluster_name": "nazwa sub-tematu", "keywords": ["fraza1", "fraza2"]}}, ...]\n'
            f"Bez wyjaśnień, bez dodatkowego tekstu."
        )
    else:
        system = (
            f"You are an SEO expert building a topical authority map.\n"
            f"SEED (main topic): {seed}\n\n"
            f"Group the following phrases into max {max_clusters} semantic sub-topics.\n"
            f"Each sub-topic = a separate pillar article.\n\n"
            f"GROUPING RULES:\n"
            f"- Group by REAL topic aspect: 'maintenance', 'types', 'how to choose', 'pricing', 'for whom'\n"
            f"- Do NOT group by modifiers: 'cheap', 'online', 'ranking', 'best', '2024'\n"
            f"- Each cluster_name is a short sub-topic name (2-4 words), not a keyword\n"
            f"- Phrases not fitting any sub-topic go into 'other'\n\n"
            f"Respond ONLY with valid JSON:\n"
            f'[{{"cluster_name": "sub-topic name", "keywords": ["phrase1", "phrase2"]}}, ...]\n'
            f"No explanations, no extra text."
        )

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": kw_lines},
            ],
            temperature=0.1,
            max_tokens=2000,
        )
        raw = resp.choices[0].message.content.strip()
        # Extract JSON array
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not match:
            raise ValueError(f"No JSON array in GPT response: {raw[:200]}")
        gpt_clusters = _json.loads(match.group())
    except Exception as e:
        logger.warning(f"[TopicalMap] _cluster_semantic GPT failed: {e} — falling back to _cluster()")
        return _cluster(keywords, seed, max_clusters, min_coherence)

    # Build keyword lookup dict (keyword text → full dict)
    kw_lookup: dict[str, dict] = {k["keyword"]: k for k in keywords}

    result_clusters = []
    assigned_kw_keys: set = set()

    for gpt_cl in gpt_clusters:
        cluster_name = gpt_cl.get("cluster_name", "inne")
        if cluster_name.lower() in ("inne", "other", "pozostałe", "pozostale"):
            continue
        matched_kws = []
        for kw_text in gpt_cl.get("keywords", []):
            # Exact match first
            if kw_text in kw_lookup and kw_text not in assigned_kw_keys:
                matched_kws.append(kw_lookup[kw_text])
                assigned_kw_keys.add(kw_text)
            else:
                # Fuzzy: find closest unassigned keyword by token overlap
                kw_fold = _ascii_fold(kw_text.lower())
                for orig_kw, kw_dict in kw_lookup.items():
                    if orig_kw in assigned_kw_keys:
                        continue
                    if _ascii_fold(orig_kw.lower()) == kw_fold:
                        matched_kws.append(kw_dict)
                        assigned_kw_keys.add(orig_kw)
                        break

        if not matched_kws:
            continue

        total_vol = sum(k.get("search_volume", 0) for k in matched_kws)
        avg_kd = sum(k.get("keyword_difficulty", 50) for k in matched_kws) / len(matched_kws)
        avg_cpc = sum(k.get("cpc", 0) for k in matched_kws) / len(matched_kws)
        focus = _cluster_focus_score(matched_kws, cluster_name)

        result_clusters.append({
            "anchor": cluster_name,
            "label": cluster_name.title(),
            "keywords": matched_kws,
            "total_volume": total_vol,
            "avg_difficulty": round(avg_kd, 1),
            "avg_cpc": round(avg_cpc, 2),
            "focus_score": round(focus, 3),
        })

    # Assign remaining unmatched keywords to closest cluster by token overlap
    unmatched = [k for k in keywords if k["keyword"] not in assigned_kw_keys]
    if unmatched and result_clusters:
        seed_toks = _seed_tokens(seed)
        for kw in unmatched:
            kw_toks = set(_tokenize(kw["keyword"])) - seed_toks
            best_cl = max(
                result_clusters,
                key=lambda cl: len(kw_toks & set(
                    t for kw2 in cl["keywords"] for t in _tokenize(kw2["keyword"])
                ) - seed_toks) / max(len(kw_toks), 1)
            )
            best_cl["keywords"].append(kw)
            best_cl["total_volume"] += kw.get("search_volume", 0)

    if not result_clusters:
        logger.warning("[TopicalMap] _cluster_semantic returned 0 clusters — falling back to _cluster()")
        return _cluster(keywords, seed, max_clusters, min_coherence)

    logger.info(f"[TopicalMap] _cluster_semantic: {len(result_clusters)} semantic clusters (GPT-based)")
    return sorted(result_clusters, key=lambda x: x["total_volume"], reverse=True)


def _check_intent_coherence(clusters: list[dict]) -> list[dict]:
    """
    Flags clusters with mixed search intent (<60% dominant intent).
    Adds 'dominant_intent', 'intent_coherence', 'mixed_intent' fields.
    """
    for cluster in clusters:
        intents = [k.get("intent", "informational") for k in cluster.get("keywords", [])]
        total = len(intents)
        if total == 0:
            cluster["dominant_intent"] = "informational"
            cluster["intent_coherence"] = 1.0
            cluster["mixed_intent"] = False
            continue
        most_common_intent, count = Counter(intents).most_common(1)[0]
        coherence_ratio = count / total
        cluster["dominant_intent"] = most_common_intent
        cluster["intent_coherence"] = round(coherence_ratio, 2)
        cluster["mixed_intent"] = coherence_ratio < 0.60
        if cluster["mixed_intent"]:
            logger.info(
                f"[TopicalMap] Mixed-intent cluster: '{cluster.get('anchor', '?')}' "
                f"({most_common_intent} {coherence_ratio:.0%}, {total} kws)"
            )
    return clusters


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
    batch_size: int = 25,
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
        "Odpowiedz TYLKO tablicą JSON obiektów, np:\n"
        '[{"score":1,"subtopic":"konserwacja"},{"score":0,"subtopic":null},{"score":1,"subtopic":"jak wybrać"}]\n'
        "Dla score=0 subtopic zawsze null. Subtopic to prawdziwy aspekt tematu (2-4 słowa), NIE modifier ('tani','online').\n"
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
        "Respond ONLY with a JSON array of objects, e.g:\n"
        '[{"score":1,"subtopic":"maintenance"},{"score":0,"subtopic":null},{"score":1,"subtopic":"how to choose"}]\n'
        "For score=0 subtopic is always null. Subtopic is a real topic aspect (2-4 words), NOT a modifier ('cheap','online').\n"
        "No explanations, no extra text."
    )

    # Pre-compute seed tokens for coherence fallback
    # For multi-seed, compute tokens for each seed separately and take max score
    _seeds_list = [s.strip() for s in seed.split(",") if s.strip()] or [seed]
    _seeds_toks_list = [_seed_tokens(s) for s in _seeds_list]
    seed_toks = _seed_tokens(seed)  # keep for single-seed compat

    def _multi_coherence(keyword: str) -> float:
        """Return max coherence score across all seeds (for multi-seed support)."""
        return max(_coherence_score(keyword, toks, s) for toks, s in zip(_seeds_toks_list, _seeds_list))

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
                text = resp.choices[0].message.content or ""
                text = text.strip()
                logger.info(f"[TopicalMap] GPT response batch {batch_num}: {text[:200]}")

                # Parse JSON array — supports new object format [{score,subtopic}] and legacy [1,0,1]
                match = re.search(r'\[.*?\]', text, re.DOTALL)
                verdicts = []
                subtopics = []
                if match:
                    parsed = _json.loads(match.group())
                    if parsed and isinstance(parsed[0], dict):
                        # New format: [{"score": 1, "subtopic": "konserwacja"}, ...]
                        verdicts = [int(item.get("score", 1)) for item in parsed]
                        subtopics = [item.get("subtopic") for item in parsed]
                    else:
                        # Legacy format: [1, 0, 1, ...]
                        verdicts = [int(v) for v in parsed]
                        subtopics = [None] * len(verdicts)
                else:
                    # Char-level fallback
                    digit_ratio = sum(1 for c in text if c in '01') / max(len(text), 1)
                    if digit_ratio > 0.3:
                        verdicts = [int(c) for c in text if c in '01']
                        subtopics = [None] * len(verdicts)
                    else:
                        raise ValueError(f"GPT response not parseable as verdicts: {text[:100]}")

                if len(verdicts) >= len(batch):
                    verdicts = verdicts[:len(batch)]
                    subtopics = (subtopics + [None] * len(batch))[:len(batch)]
                elif len(verdicts) < len(batch):
                    logger.warning(f"[TopicalMap] GPT returned {len(verdicts)} verdicts for batch of {len(batch)} — padding with KEEP (1)")
                    verdicts.extend([1] * (len(batch) - len(verdicts)))
                    subtopics.extend([None] * (len(batch) - len(subtopics)))

                kept = 0
                for kw, v, subtopic in zip(batch, verdicts, subtopics):
                    if v == 1:
                        if subtopic:
                            kw["gpt_subtopic"] = subtopic
                        relevant.append(kw)
                        kept += 1

                logger.info(f"[TopicalMap] GPT relevance batch {batch_num}: {kept}/{len(batch)} kept")
                gpt_ok = True
                break
            except Exception as e:
                logger.warning(f"[TopicalMap] GPT relevance batch {batch_num} attempt {attempt+1} failed: {e}")
                if attempt == 4:
                    # GPT completely failed — use coherence-based fallback instead of keeping everything
                    logger.warning(f"[TopicalMap] GPT filter failed for batch {batch_num}, using multi-seed coherence fallback (>=0.10)")
                    for kw in batch:
                        score = _multi_coherence(kw["keyword"])
                        if score >= 0.10:
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
    strategy: str = "default",
) -> dict:
    """
    Generate topical map: pillar pages + supporting pages.

    strategy — named preset that overrides defaults:
      'breadth'          — wide map, many pillars, loose semantics (new domains)
      'depth'            — few pillars, deep supporting (niche authority)
      'competitor_gap'   — gap analysis vs competitor_domain
      'quick_wins'       — low-KD, decent-volume keywords only
      'topical_authority'— full topic coverage, strict semantics (E-E-A-T/YMYL)
      'default'          — no preset applied

    Caller-supplied params always override strategy defaults.
    """
    # Apply strategy preset (caller params take precedence)
    _kw: dict = {}
    _kw = apply_strategy(strategy, _kw)
    _strategy_meta = _kw.pop("_strategy_meta", {"name": "default", "description": ""})
    _quick_wins_only: bool = _kw.pop("_quick_wins_only", False) if "_quick_wins_only" in _kw else \
        STRATEGY_PRESETS.get(strategy, {}).get("_quick_wins_only", False)
    # Apply preset defaults only where caller used the function default value
    if min_volume == 10 and "min_volume" in _kw:
        min_volume = _kw["min_volume"]
    if max_clusters == 8 and "max_clusters" in _kw:
        max_clusters = _kw["max_clusters"]
    if min_coherence == 0.0 and "min_coherence" in _kw:
        min_coherence = _kw["min_coherence"]

    # Include domain context + filter version in cache key
    # v2: GPT relevance filter always runs (invalidates all pre-filter caches)
    _desc_hash = hashlib.md5(site_description.encode(), usedforsecurity=False).hexdigest()[:8] if site_description else ""
    cache_key = hashlib.md5(
        f"v2:{seed.lower().strip()}:{location_code}:{language_code}:{min_volume}:{min_coherence}:{max_clusters}:{competitor_domain}:{domain_url}:{_desc_hash}".encode(),
        usedforsecurity=False,
    ).hexdigest()
    if not force_refresh:
        cached = await _map_cache_get(cache_key)
        if cached:
            logger.info(f"[TopicalMap] Cache hit for '{seed}'")
            return cached

    client = DataForSEOClient(dfs_login, dfs_password)

    # Support multi-seed: "zdrowie,sport,fitness" → query each seed separately and merge
    seeds = [s.strip() for s in seed.split(",") if s.strip()]
    if not seeds:
        seeds = [seed]
    primary_seed = seeds[0]  # used for cache key label and pillar scoring
    logger.info(f"[TopicalMap] Seeds parsed: {seeds} (force_refresh={force_refresh})")

    raw = []
    # FIX #16: related_keywords depth increased to 2
    # FIX #17: optionally fetch keywords_for_site in parallel
    # Multi-seed: build coros for ALL seeds, split limits across seeds
    # Full limits per seed — multi-seed means MORE data, not divided data
    per_seed_suggestions = 500
    per_seed_ideas = 300
    per_seed_related = 150
    logger.info(f"[TopicalMap] Per-seed limits: suggestions={per_seed_suggestions}, ideas={per_seed_ideas}, related={per_seed_related} x {len(seeds)} seeds = up to {(per_seed_suggestions+per_seed_ideas+per_seed_related)*len(seeds)} raw")

    coros = []
    coro_names = []
    for s in seeds:
        coros += [
            client.keyword_suggestions(s, location_code, language_code, per_seed_suggestions),
            client.keyword_ideas(s, location_code, language_code, per_seed_ideas),
            client.related_keywords(s, location_code, language_code, per_seed_related),
        ]
        coro_names += [f"suggestions:{s}", f"ideas:{s}", f"related:{s}"]
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
            logger.info(f"[TopicalMap] {name}: {len(kws) if not isinstance(kws, Exception) else 'ERR'}")

    logger.info(f"[TopicalMap] Total raw from DataForSEO: {len(raw)} keywords")
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
    # Multi-seed: use max coherence across all seeds
    _all_seed_toks = [(_seed_tokens(s), s) for s in ([s.strip() for s in seed.split(",") if s.strip()] or [seed])]
    seed_toks = _seed_tokens(seed)
    for k in keywords:
        k["coherence"] = max(_coherence_score(k["keyword"], toks, s) for toks, s in _all_seed_toks)
        kw_key = _ascii_fold(_clean(k["keyword"]))
        if kw_key in competitor_kws:
            k["already_ranking"] = True
            k["current_position"] = competitor_kws[kw_key]
        else:
            k["already_ranking"] = False
            k["current_position"] = 0

    # Strategy: quick_wins — keep only keywords with KD < 30 (easy to rank)
    # Applied before clustering so cluster composition reflects the filter
    if _quick_wins_only:
        pre_qw = len(keywords)
        keywords = [k for k in keywords if k.get("keyword_difficulty", 100) < 30]
        if not keywords:
            logger.warning("[TopicalMap] quick_wins filter removed ALL keywords — reverting to volume-filtered list")
            keywords = list(filtered)  # restore post-volume filtered list
        logger.info(f"[TopicalMap] quick_wins filter: {pre_qw} → {len(keywords)} (KD<30)")

    # Use GPT semantic clustering if enough keywords, else fall back to token-based
    if len(keywords) >= 20:
        clusters = await _cluster_semantic(keywords, seed, max_clusters, language_code, min_coherence)
    else:
        clusters = _cluster(keywords, seed, max_clusters, min_coherence)
    logger.info(f"[TopicalMap] clusters: {len(clusters)}")

    if len(clusters) <= 1 and max_clusters < 15:
        if len(keywords) >= 20:
            clusters = await _cluster_semantic(keywords, seed, 15, language_code, min_coherence)
        else:
            clusters = _cluster(keywords, seed, 15, min_coherence)
        logger.info(f"[TopicalMap] retry with max_clusters=15: {len(clusters)}")

    # Check intent coherence per cluster — flags mixed-intent clusters
    clusters = _check_intent_coherence(clusters)

    # Pillar score: breadth-first (broad informational topics preferred)
    # REMOVED cpc_bonus — CPC favours commercial keywords as pillar (wrong)
    # ADDED breadth_bonus — shorter phrases are broader topics (better pillar)
    # ADDED intent_bonus — informational strongly preferred as pillar
    def _pillar_score(k):
        vol = k.get("search_volume", 0)
        kd = k.get("keyword_difficulty", 50)
        coherence = k.get("coherence", 0.5)
        word_count = len(k.get("keyword", "").split())
        breadth_bonus = 1.3 if word_count <= 3 else (1.1 if word_count <= 4 else 0.85)
        intent = k.get("intent", "informational")
        intent_bonus = 1.2 if intent == "informational" else (1.0 if intent == "commercial" else 0.75)
        return (vol / (kd + 1)) * (0.5 + coherence) * breadth_bonus * intent_bonus

    pillars = []
    for cluster in clusters:
        all_cluster_kws = cluster["keywords"]
        informational = [k for k in all_cluster_kws if k.get("intent", "informational") in ("informational", "")]

        pillar_candidates = informational if informational else all_cluster_kws
        pillar_candidates_sorted = sorted(pillar_candidates, key=_pillar_score, reverse=True)
        pillar_kw = pillar_candidates_sorted[0] if pillar_candidates_sorted else {"keyword": cluster["anchor"], "search_volume": 0}

        # 3-tier: remaining keywords after pillar → cluster pages + supporting
        remaining = [k for k in all_cluster_kws if k["keyword"] != pillar_kw["keyword"]]
        remaining_sorted = sorted(
            remaining,
            key=lambda x: (x.get("search_volume", 0) * (0.5 + x.get("coherence", 0.5))),
            reverse=True,
        )

        # Build cluster pages (mid-tier): top 3-5 keywords by volume×coherence
        # Prefer informational; each cluster page gets 2-4 supporting keywords
        cluster_pages_raw = remaining_sorted[:5]  # up to 5 cluster pages
        cluster_page_keys = {k["keyword"] for k in cluster_pages_raw}
        leftover = [k for k in remaining_sorted[5:] if k["keyword"] not in cluster_page_keys]

        # Assign leftover supporting keywords to nearest cluster page by token overlap
        seed_toks_local = _seed_tokens(seed)
        cluster_pages_built = []
        for cp_kw in cluster_pages_raw:
            cp_toks = set(_tokenize(cp_kw["keyword"])) - seed_toks_local
            # Find 2-4 supporting: lower volume, semantically closest to this cluster page
            sup_for_cp = sorted(
                leftover,
                key=lambda x: len((set(_tokenize(x["keyword"])) - seed_toks_local) & cp_toks) / max(len(cp_toks), 1),
                reverse=True,
            )[:4]
            used_sup = {k["keyword"] for k in sup_for_cp}
            leftover = [k for k in leftover if k["keyword"] not in used_sup]

            cluster_pages_built.append({
                "keyword": cp_kw["keyword"],
                "search_volume": cp_kw.get("search_volume", 0),
                "keyword_difficulty": cp_kw.get("keyword_difficulty", 0),
                "coherence": round(cp_kw.get("coherence", 0), 3),
                "intent": cp_kw.get("intent", "informational"),
                "cpc": round(cp_kw.get("cpc", 0), 2),
                "gpt_subtopic": cp_kw.get("gpt_subtopic", ""),
                "supporting_keywords": [
                    {
                        "keyword": s["keyword"],
                        "search_volume": s.get("search_volume", 0),
                        "keyword_difficulty": s.get("keyword_difficulty", 0),
                        "coherence": round(s.get("coherence", 0), 3),
                        "intent": s.get("intent", "informational"),
                        "cpc": round(s.get("cpc", 0), 2),
                        "gpt_subtopic": s.get("gpt_subtopic", ""),
                    }
                    for s in sup_for_cp
                ],
            })

        # Backward-compat flat supporting_keywords = all non-pillar kws (cluster pages + their supporting)
        all_supporting_flat = remaining_sorted
        intent_counts = Counter(k.get("intent", "informational") for k in all_cluster_kws)
        intent_dist = {intent: count for intent, count in intent_counts.most_common()}

        sup_limit = min(20, max(4, len(all_supporting_flat)))

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
            # 3-tier hierarchy: cluster pages (mid-tier) each with their supporting pages
            "clusters": cluster_pages_built,
            # Backward-compat flat list: all non-pillar keywords
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
                for k in all_supporting_flat[:sup_limit]
            ],
            "total_volume": cluster["total_volume"],
            "avg_difficulty": cluster["avg_difficulty"],
            "avg_cpc": cluster.get("avg_cpc", 0),
            "intent_distribution": intent_dist,
            "cannibalization_risk": cannibal_count,
            "content_gap": {
                "total_subtopics": len(all_supporting_flat),
                "high_volume_gaps": len([k for k in all_supporting_flat if k.get("search_volume", 0) >= 100]),
                "low_kd_opportunities": len([k for k in all_supporting_flat if k.get("keyword_difficulty", 50) < 30]),
                "quick_wins": len([
                    k for k in all_supporting_flat
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
        "strategy": _strategy_meta.get("name", "default"),
        "strategy_description": _strategy_meta.get("description", ""),
        "total_keywords": len(keywords),
        "pillars": pillars,
        "nodes": nodes,
        "links": links,
        "site_metrics": site_metrics,
        "competitor_analysis": cannibal_summary,
    }
    await _map_cache_set(cache_key, result)
    return result

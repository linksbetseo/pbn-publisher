"""
CEO Evaluator dla Topical Authority Map.
Ocenia wygenerowane mapy wg 10 kryteriów jakości SEO.
Akceptuje tylko wynik 10/10 (każde kryterium >= 7/10, suma >= 85/100).
"""
import json, sys, os
os.environ["PYTHONIOENCODING"] = "utf-8"

OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")

CRITERIA = """
Oceniasz topical authority map pod kątem SEO. 10 kryteriów, każde 0-10:

1. PILLAR_BREADTH: Pillar keyword jest szeroki, informacyjny, broad topic (2-3 słowa, high volume). NIE commercial, NIE long-tail, NIE branded. (10=idealny pillar, 0=specificzny long-tail jako pillar)

2. CLUSTER_SEMANTICS: Każdy cluster pokrywa OSOBNY semantyczny sub-temat pillar page. NIE modifier-based ("tani X", "online X"). Cluster anchor = real sub-topic. (10=osobne sub-tematy, 0=klastry po modifierach)

3. INTENT_COHERENCE: W każdym klastrze dominuje jedna intencja (>70% jednej). Brak mieszania informational+transactional w tym samym klastrze. (10=spójna intencja, 0=chaos intencji)

4. HIERARCHY_3TIER: Wyraźna struktura: Pillar → Cluster pages (3-5) → Supporting pages (2-3 per cluster). NIE płaska lista 20+ supporting. (10=trójpoziomowa, 0=płaska)

5. SUPPORTING_SPECIFICITY: Supporting pages = long-tail, specific question, konkretny aspekt. Mają narrower intent niż cluster page. (10=bardzo specyficzne, 0=duplikują cluster)

6. ENTITY_COVERAGE: Każdy klaster wprowadza inne encje (marki, instytucje, produkty). Brak kręcenia się wokół tych samych 3-4 encji w całej mapie. (10=bogate encje, 0=te same wszędzie)

7. SERP_ALIGNMENT: Keywordy odpowiadają temu co Google faktycznie indeksuje dla tego tematu. NIE frazy bez wolumenu, NIE frazy z intencją nawigacyjną. (10=SERP-aligned, 0=oderwane od SERP)

8. INTERNAL_LINK_LOGIC: Widoczna logika linkowania: supporting → cluster → pillar. Każda strona jasno linkuje do "parent" w hierarchii. (10=wyraźna logika, 0=brak hierarchii)

9. TOPICAL_COVERAGE: Mapa pokrywa główne aspekty tematu (co, jak, dlaczego, kiedy, ile, dla kogo). Brak oczywistych luk (np. brak "cena" dla tematu komercyjnego). (10=kompletne pokrycie, 0=fragmentaryczne)

10. KEYWORD_QUALITY: Keywordy mają realistyczny search volume (>50/mies), KD odpowiednie do pozycji w hierarchii (pillar wyższy KD OK, supporting niski KD). Brak śmieciowych fraz. (10=wysokiej jakości, 0=spam/zero volume)

AKCEPTACJA: suma >= 85/100 I każde kryterium >= 7/10.

Odpowiedz TYLKO valid JSON:
{
  "scores": {
    "pillar_breadth": X,
    "cluster_semantics": X,
    "intent_coherence": X,
    "hierarchy_3tier": X,
    "supporting_specificity": X,
    "entity_coverage": X,
    "serp_alignment": X,
    "internal_link_logic": X,
    "topical_coverage": X,
    "keyword_quality": X
  },
  "verdict": "ACCEPT" | "REJECT",
  "weak_points": ["konkretny problem 1", "konkretny problem 2"],
  "fix_instructions": ["co dokładnie zmienić w algorytmie aby naprawić słabe punkty"]
}
"""

import urllib.request

def evaluate_map(topical_map: dict) -> dict:
    """
    topical_map: dict z kluczami 'seed', 'pillars' (lista pillar objectów)
    Każdy pillar ma: keyword, search_volume, keyword_difficulty, intent, supporting_keywords (lista)
    """
    map_summary = _summarize_map(topical_map)

    payload = {
        "model": "gpt-4.1",
        "temperature": 0,
        "max_tokens": 800,
        "messages": [
            {"role": "system", "content": CRITERIA},
            {"role": "user", "content": f"Oceń tę topical authority map:\n\n{map_summary}"}
        ]
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read().decode("utf-8"))
    raw = resp["choices"][0]["message"]["content"].strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        _criteria_keys = [
            "pillar_breadth", "cluster_semantics", "intent_coherence", "hierarchy_3tier",
            "supporting_specificity", "entity_coverage", "serp_alignment",
            "internal_link_logic", "topical_coverage", "keyword_quality",
        ]
        return {
            "scores": {k: 0 for k in _criteria_keys},
            "verdict": "REJECT",
            "weak_points": [f"CEO evaluation failed — GPT returned invalid JSON: {str(e)[:120]}"],
            "fix_instructions": ["Re-run CEO evaluation (GPT response was malformed)"],
        }


def _summarize_map(m: dict) -> str:
    seed = m.get("seed", "unknown")
    pillars = m.get("pillars", [])
    lines = [f"SEED: {seed}", f"LICZBA PILLARÓW: {len(pillars)}", ""]
    for i, p in enumerate(pillars, 1):
        kw = p.get("keyword", p.get("pillar_keyword", "?"))
        vol = p.get("search_volume", p.get("pillar_volume", 0))
        kd = p.get("keyword_difficulty", p.get("pillar_difficulty", "?"))
        intent = p.get("intent", p.get("pillar_intent", "?"))
        lines.append(f"PILLAR {i}: {kw} (vol={vol}, KD={kd}, intent={intent})")
        # clusters / supporting
        clusters = p.get("clusters", [])
        supporting = p.get("supporting_keywords", [])
        if clusters:
            for c in clusters:
                ckw = c.get("keyword", c.get("cluster_keyword", "?"))
                cvol = c.get("search_volume", 0)
                cintent = c.get("intent", "?")
                lines.append(f"  CLUSTER: {ckw} (vol={cvol}, intent={cintent})")
                sups = c.get("supporting_keywords", [])
                for s in sups[:5]:
                    skw = s.get("keyword", "?") if isinstance(s, dict) else str(s)
                    svol = s.get("search_volume", 0) if isinstance(s, dict) else 0
                    lines.append(f"    SUPPORTING: {skw} (vol={svol})")
        elif supporting:
            # flat structure — all supporting under pillar
            lines.append(f"  SUPPORTING ({len(supporting)} total, flat):")
            for s in supporting[:8]:
                skw = s.get("keyword", "?") if isinstance(s, dict) else str(s)
                svol = s.get("search_volume", 0) if isinstance(s, dict) else 0
                sintent = s.get("intent", "?") if isinstance(s, dict) else "?"
                lines.append(f"    - {skw} (vol={svol}, intent={sintent})")
            if len(supporting) > 8:
                lines.append(f"    ... i {len(supporting)-8} więcej")
        lines.append("")
    return "\n".join(lines)


def print_result(result: dict):
    scores = result["scores"]
    total = sum(scores.values())
    verdict = result["verdict"]
    verdict_color = "\033[92m" if verdict == "ACCEPT" else "\033[91m"
    reset = "\033[0m"

    print(f"\n{'='*60}")
    print(f"  CEO VERDICT: {verdict_color}{verdict}{reset}  ({total}/100)")
    print(f"{'='*60}")
    for k, v in sorted(scores.items(), key=lambda x: x[1]):
        bar = "#" * v + "." * (10 - v)
        color = "\033[92m" if v >= 8 else ("\033[93m" if v >= 6 else "\033[91m")
        print(f"  {k:<25} [{color}{bar}{reset}] {v}/10")
    print()
    if result.get("weak_points"):
        print("  PROBLEMY:")
        for w in result["weak_points"]:
            print(f"    - {w}")
    print()
    if result.get("fix_instructions"):
        print("  CO NAPRAWIC W ALGORYTMIE:")
        for f in result["fix_instructions"]:
            print(f"    >> {f}")
    print(f"{'='*60}\n")
    return total, verdict


if __name__ == "__main__":
    # Test z przykładową mapą — dla walidacji CEO
    TEST_MAP = {
        "seed": "ekspres do kawy",
        "pillars": [
            {
                "keyword": "ekspres do kawy",
                "search_volume": 8100,
                "keyword_difficulty": 45,
                "intent": "informational",
                "clusters": [
                    {
                        "keyword": "jak wybrać ekspres do kawy",
                        "search_volume": 1600,
                        "intent": "informational",
                        "supporting_keywords": [
                            {"keyword": "ekspres do kawy dla początkujących", "search_volume": 320, "intent": "informational"},
                            {"keyword": "na co zwrócić uwagę przy zakupie ekspresu", "search_volume": 210, "intent": "informational"},
                        ]
                    },
                    {
                        "keyword": "rodzaje ekspresów do kawy",
                        "search_volume": 1200,
                        "intent": "informational",
                        "supporting_keywords": [
                            {"keyword": "ekspres kolbowy vs ciśnieniowy różnice", "search_volume": 590, "intent": "informational"},
                            {"keyword": "ekspres automatyczny do domu opinie", "search_volume": 480, "intent": "commercial"},
                        ]
                    },
                    {
                        "keyword": "ekspres do kawy konserwacja",
                        "search_volume": 880,
                        "intent": "informational",
                        "supporting_keywords": [
                            {"keyword": "jak odkamieniać ekspres do kawy krok po kroku", "search_volume": 720, "intent": "informational"},
                            {"keyword": "jak czyścić ekspres do kawy", "search_volume": 390, "intent": "informational"},
                        ]
                    },
                ]
            },
            {
                "keyword": "tani ekspres do kawy",
                "search_volume": 2400,
                "keyword_difficulty": 35,
                "intent": "commercial",
                "clusters": [
                    {
                        "keyword": "najtańszy ekspres do kawy ranking",
                        "search_volume": 890,
                        "intent": "commercial",
                        "supporting_keywords": [
                            {"keyword": "ekspres do kawy do 200 zł", "search_volume": 480, "intent": "commercial"},
                            {"keyword": "dobry ekspres do kawy do 500 zł", "search_volume": 320, "intent": "commercial"},
                        ]
                    },
                ]
            }
        ]
    }

    print("Testowanie CEO evaluatora na wzorcowej mapie...")
    result = evaluate_map(TEST_MAP)
    total, verdict = print_result(result)
    print(f"Test zakończony: {total}/100 — {verdict}")
    print("(Wzorcowa mapa powinna dostać ACCEPT >= 85/100)")

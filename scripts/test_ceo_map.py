"""
Test topical map dla CEO evaluatora.
Mapa odpowiada nowemu formatowi 3-tier (pillar_keyword, clusters, supporting_keywords).
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["PYTHONIOENCODING"] = "utf-8"

from scripts.topical_map_ceo import evaluate_map, print_result

TEST_MAP = {
    "seed": "zdrowie",
    "pillars": [
        {
            "pillar_keyword": "zdrowie psychiczne",
            "pillar_volume": 6600,
            "pillar_difficulty": 42,
            "pillar_intent": "informational",
            "clusters": [
                {
                    "keyword": "jak dbac o zdrowie psychiczne",
                    "search_volume": 1300,
                    "intent": "informational",
                    "supporting_keywords": [
                        {"keyword": "techniki relaksacji dla doroslych", "search_volume": 480, "intent": "informational"},
                        {"keyword": "medytacja mindfulness jak zaczac", "search_volume": 590, "intent": "informational"},
                        {"keyword": "cwiczenia oddechowe na stres krok po kroku", "search_volume": 320, "intent": "informational"},
                    ]
                },
                {
                    "keyword": "objawy depresji u doroslych",
                    "search_volume": 2400,
                    "intent": "informational",
                    "supporting_keywords": [
                        {"keyword": "depresja sezonowa objawy i leczenie", "search_volume": 880, "intent": "informational"},
                        {"keyword": "jak rozpoznac depresje u bliskiej osoby", "search_volume": 720, "intent": "informational"},
                        {"keyword": "kiedy zglosic sie do psychiatry pierwsze kroki", "search_volume": 390, "intent": "informational"},
                    ]
                },
                {
                    "keyword": "stres przewlekly skutki zdrowotne",
                    "search_volume": 1100,
                    "intent": "informational",
                    "supporting_keywords": [
                        {"keyword": "kortyzol wysoki objawy i norma", "search_volume": 660, "intent": "informational"},
                        {"keyword": "jak obnizych poziom stresu w pracy", "search_volume": 540, "intent": "informational"},
                    ]
                },
                {
                    "keyword": "psychoterapia rodzaje i ceny",
                    "search_volume": 890,
                    "intent": "commercial",
                    "supporting_keywords": [
                        {"keyword": "terapia CBT co to jest i jak dziala", "search_volume": 750, "intent": "informational"},
                        {"keyword": "psycholog online ile kosztuje sesja", "search_volume": 480, "intent": "commercial"},
                    ]
                },
            ]
        },
        {
            "pillar_keyword": "dieta i odzywianie",
            "pillar_volume": 5400,
            "pillar_difficulty": 38,
            "pillar_intent": "informational",
            "clusters": [
                {
                    "keyword": "zbilansowana dieta zasady",
                    "search_volume": 1800,
                    "intent": "informational",
                    "supporting_keywords": [
                        {"keyword": "piramida zywienia 2024 zalecenia", "search_volume": 720, "intent": "informational"},
                        {"keyword": "makroskaldniki proporcje w diecie", "search_volume": 540, "intent": "informational"},
                        {"keyword": "jak liczyc kalorie krok po kroku", "search_volume": 890, "intent": "informational"},
                    ]
                },
                {
                    "keyword": "niedobory witamin objawy",
                    "search_volume": 2200,
                    "intent": "informational",
                    "supporting_keywords": [
                        {"keyword": "niedobor witaminy D objawy i suplementacja", "search_volume": 1100, "intent": "informational"},
                        {"keyword": "niedobor zelaza objawy u kobiet", "search_volume": 880, "intent": "informational"},
                        {"keyword": "magnez kiedy brac i ile", "search_volume": 660, "intent": "informational"},
                    ]
                },
                {
                    "keyword": "dieta przeciwzapalna co jesc",
                    "search_volume": 980,
                    "intent": "informational",
                    "supporting_keywords": [
                        {"keyword": "produkty przeciwzapalne lista", "search_volume": 720, "intent": "informational"},
                        {"keyword": "omega 3 zrodla w diecie", "search_volume": 540, "intent": "informational"},
                    ]
                },
                {
                    "keyword": "odchudzanie zdrowe metody",
                    "search_volume": 3300,
                    "intent": "informational",
                    "supporting_keywords": [
                        {"keyword": "deficyt kaloryczny jak wyliczyc", "search_volume": 1200, "intent": "informational"},
                        {"keyword": "post przerywany 16 8 dla poczatkujacych", "search_volume": 880, "intent": "informational"},
                        {"keyword": "plateau odchudzanie jak przerwac", "search_volume": 390, "intent": "informational"},
                    ]
                },
            ]
        },
        {
            "pillar_keyword": "aktywnosc fizyczna i sport",
            "pillar_volume": 4800,
            "pillar_difficulty": 35,
            "pillar_intent": "informational",
            "clusters": [
                {
                    "keyword": "cwiczenia dla poczatkujacych w domu",
                    "search_volume": 2100,
                    "intent": "informational",
                    "supporting_keywords": [
                        {"keyword": "trening FBW plan na 3 dni w tygodniu", "search_volume": 880, "intent": "informational"},
                        {"keyword": "pompki technika dla poczatkujacych", "search_volume": 660, "intent": "informational"},
                        {"keyword": "rozgrzewka przed cwiczeniami schemat", "search_volume": 540, "intent": "informational"},
                    ]
                },
                {
                    "keyword": "bieganie dla zdrowia korzysci",
                    "search_volume": 1400,
                    "intent": "informational",
                    "supporting_keywords": [
                        {"keyword": "jak zaczac biegac od zera plan 5km", "search_volume": 1100, "intent": "informational"},
                        {"keyword": "bieganie a stawy ryzyko kontuzji", "search_volume": 480, "intent": "informational"},
                        {"keyword": "tetno przy bieganiu strefa tluszczowa", "search_volume": 390, "intent": "informational"},
                    ]
                },
                {
                    "keyword": "silownia trening sily podstawy",
                    "search_volume": 1600,
                    "intent": "informational",
                    "supporting_keywords": [
                        {"keyword": "martwy ciag technika dla poczatkujacych", "search_volume": 720, "intent": "informational"},
                        {"keyword": "progresja obciazen jak planowac", "search_volume": 390, "intent": "informational"},
                        {"keyword": "regeneracja miesni po treningu ile czasu", "search_volume": 540, "intent": "informational"},
                    ]
                },
            ]
        },
    ]
}

if __name__ == "__main__":
    print("Uruchamiam CEO evaluator na mapie 'zdrowie' (3-tier format)...")
    result = evaluate_map(TEST_MAP)
    total, verdict = print_result(result)
    if verdict == "ACCEPT":
        print(f"SUKCES: Mapa zaakceptowana {total}/100 — mozna pushowac do projektu.")
    else:
        print(f"ODRZUCONO {total}/100 — iteruj dalej.")

#!/usr/bin/env python3
"""
Quality Iteration Agent — CEO Loop
====================================
Uses Claude Opus as CEO to score articles against the 10-criterion audit.
Iterates until CEO scores 80/100 by modifying openai_service.py prompts,
committing to git, waiting for Railway deploy, and re-testing.

Usage:
    python scripts/quality_agent.py [--domain zdrowosportowo.pl] [--max-iter 10]

Requirements:
    pip install anthropic httpx beautifulsoup4
    ANTHROPIC_API_KEY env var set
"""

import argparse
import asyncio
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Force UTF-8 output on Windows (avoids UnicodeEncodeError for Polish chars / arrows)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import httpx

try:
    import anthropic
except ImportError:
    print("ERROR: pip install anthropic")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: pip install beautifulsoup4")
    sys.exit(1)


# ─── CONFIG ──────────────────────────────────────────────────────────────────
RAILWAY_URL = "https://pbn-publisher-production.up.railway.app"
REPO_ROOT = Path(__file__).parent.parent


def _load_env_file(path: Path):
    """Load key=value pairs from .env file into os.environ (skip if already set)."""
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


# Load env files so ANTHROPIC_API_KEY can be read from backend/.env
_load_env_file(REPO_ROOT / "backend" / ".env")
_load_env_file(REPO_ROOT / ".env")
OPENAI_SERVICE = REPO_ROOT / "backend" / "services" / "openai_service.py"
CONTENT_WRITER_SERVICE = REPO_ROOT / "backend" / "services" / "content_writer_service.py"
RAILWAY_DEPLOY_WAIT = 240  # seconds to wait after git push (Railway ~3-4 min)
JOB_POLL_INTERVAL = 15   # seconds between job status checks
JOB_MAX_WAIT = 300        # max seconds to wait for article generation
DEFAULT_DOMAIN = "zdrowosportowo.pl"
DEFAULT_MAX_ITER = 8

AUDIT_CRITERIA = """
KRYTERIA OCENY ARTYKUŁU (każde 0-10 punktów, max 100):

1. CSI (Content Satisfaction Index) — Czy artykuł w pełni odpowiada na zapytanie?
   - 0: Artykuł nie odpowiada na główne pytanie
   - 5: Odpowiada częściowo, brakuje kluczowych informacji
   - 10: Pełna, wyczerpująca odpowiedź z konkretnymi detalami

2. Information Density — Gęstość wartościowych informacji
   - 0: Puste ogólniki, padding, powtórzenia
   - 5: Mix konkretów i ogólników
   - 10: Każde zdanie wnosi nową, konkretną informację

3. EAV (Entity-Attribute-Value) Richness — Bogactwo encji
   - 0: Żadnych konkretnych nazw własnych, marek, badań
   - 5: Kilka encji, ale powtarzane przez cały artykuł
   - 10: Różnorodne, nowe encje w każdej sekcji (marki, instytucje, badania, produkty)

4. BLUF Structure (Bottom Line Up Front) — Struktura odpowiedzi bezpośredniej
   - 0: Odpowiedź zakopana, wstęp ogólnikowy
   - 5: Odpowiedź w pierwszym akapicie, ale nieprecyzyjna
   - 10: Pierwsze zdanie = definicja + bezpośrednia odpowiedź, format AI Overview

5. E-E-A-T Signals — Sygnały doświadczenia i autorytetu
   - 0: Brak sygnałów eksperckości
   - 5: Ogólne sformułowania eksperckie
   - 10: Konkretne przykłady z praktyki, liczby, badania, kontrargumenty

6. AI Footprints — Ślady AI w tekście
   - 0: Pełno szablonowych zwrotów AI ("Warto zauważyć", "Nie ulega wątpliwości")
   - 5: Kilka śladów AI
   - 10: Żadnych rozpoznawalnych zwrotów AI, naturalny ludzki styl

7. Keyword Stuffing — Naturalne użycie słów kluczowych
   - 0: Fraza kluczowa powtórzona 20+ razy, nienaturalnie
   - 5: 8-15 powtórzeń, niektóre nienaturalne
   - 10: 4-7 powtórzeń, zawsze naturalne, synonimy i odmiany

8. Duplicate Content — Powtarzające się treści w artykule
   - 0: Te same fakty, zdania lub encje powtórzone w wielu sekcjach
   - 5: Niektóre powtórzenia, ale w większości świeża treść
   - 10: Każda sekcja wnosi wyłącznie nową treść, zero powtórzeń

9. Bold/Strong Abuse — Nadużycie pogrubień
   - 0: Każde trzecie słowo pogrubione, chaos wizualny
   - 5: 8-15 pogrubień, niektóre niezasadne
   - 10: Max 6-8 pogrubień na cały artykuł, tylko dla najważniejszych terminów

10. Hallucination Risk — Ryzyko hallucynacji
    - 0: Konkretne zmyślone statystyki (np. "30% mniejsze ryzyko"), fałszywe cytaty
    - 5: Ogólne sformułowania bezpieczne, brak konkretnych liczb
    - 10: Wszystkie twierdzenia weryfikowalne lub celowo ogólne ("badania wskazują")
"""

CEO_SYSTEM = f"""Jesteś CEO firmy SEO przeprowadzającym audyt jakości treści.
Twoja rola: ocenić artykuł według 10 kryteriów i wydać werdykt.

{AUDIT_CRITERIA}

FORMAT ODPOWIEDZI (ŚCISŁY):
```json
{{
  "scores": {{
    "csi": <0-10>,
    "information_density": <0-10>,
    "eav_richness": <0-10>,
    "bluf_structure": <0-10>,
    "eeat_signals": <0-10>,
    "ai_footprints": <0-10>,
    "keyword_stuffing": <0-10>,
    "duplicate_content": <0-10>,
    "bold_abuse": <0-10>,
    "hallucination_risk": <0-10>
  }},
  "total": <suma 0-100>,
  "verdict": "PASS" lub "FAIL",
  "top_issues": ["konkretny problem 1", "konkretny problem 2", "konkretny problem 3"],
  "prompt_fixes": ["konkretna instrukcja do prompta 1", "konkretna instrukcja do prompta 2"]
}}
```

WAŻNE:
- verdict = "PASS" tylko jeśli total >= 80
- top_issues: konkretne, aktywne problemy znalezione w TEKŚCIE (nie abstrakcyjne)
- prompt_fixes: konkretne zmiany do systemowych promptów GPT które naprawią te problemy
- Bądź surowy. 80/100 to wysoki standard — nie zaokrąglaj w górę.
"""


def _run(cmd: str, cwd: Optional[Path] = None) -> tuple[int, str, str]:
    """Run shell command, return (returncode, stdout, stderr)."""
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True,
        cwd=str(cwd or REPO_ROOT)
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


async def trigger_article(domain: str) -> Optional[str]:
    """Trigger article generation via Railway API. Returns job_id or None."""
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(f"{RAILWAY_URL}/api/cron/publish-one/{domain}")
            data = resp.json()
            job_id = data.get("job_id")
            print(f"  [+] Job started: {job_id}")
            return job_id
        except Exception as e:
            print(f"  [!] Failed to trigger article: {e}")
            return None


async def wait_for_job(job_id: str) -> Optional[str]:
    """Poll job status until done. Returns published URL or None."""
    print(f"  [~] Waiting for job {job_id}...", end="", flush=True)
    elapsed = 0
    async with httpx.AsyncClient(timeout=30) as client:
        while elapsed < JOB_MAX_WAIT:
            await asyncio.sleep(JOB_POLL_INTERVAL)
            elapsed += JOB_POLL_INTERVAL
            try:
                resp = await client.get(f"{RAILWAY_URL}/api/cron/job-status/{job_id}")
                data = resp.json()
                status = data.get("status", "")
                print(".", end="", flush=True)
                if data.get("done") or status in ("done", "error", "completed"):
                    print()
                    # job-status returns published_details list
                    pub_details = data.get("published_details", [])
                    if pub_details:
                        url = pub_details[0].get("url")
                        if url:
                            return url
                    # fallback: results_json
                    results = data.get("results_json", "[]")
                    try:
                        results_list = json.loads(results) if isinstance(results, str) else results
                        if isinstance(results_list, list) and results_list:
                            url = results_list[0].get("url") or results_list[0].get("link")
                            if url:
                                return url
                    except Exception:
                        pass
                    print(f"  [!] Job done but no URL found. published={data.get('published')}, data={str(data)[:200]}")
                    return None
            except Exception as e:
                print(f"\n  [!] Status poll failed: {e}")
    print()
    print(f"  [!] Job timed out after {JOB_MAX_WAIT}s")
    return None


async def fetch_article_text(url: str) -> str:
    """Fetch published article text via WP REST API (slug-based), fallback to scrape."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    slug = parsed.path.strip("/").split("/")[-1]

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        # Try WordPress REST API first (gets full rendered content even with JS themes)
        try:
            api_url = f"{base}/wp-json/wp/v2/posts?slug={slug}&_fields=title,content,excerpt"
            resp = await client.get(api_url)
            data = resp.json()
            if isinstance(data, list) and data:
                post = data[0]
                rendered = post.get("content", {}).get("rendered", "")
                if rendered:
                    soup = BeautifulSoup(rendered, "html.parser")
                    text = soup.get_text(separator="\n", strip=True)
                    if len(text.split()) > 100:
                        return text
        except Exception:
            pass

        # Fallback: direct page scrape
        try:
            resp = await client.get(url)
            soup = BeautifulSoup(resp.text, "html.parser")
            article = (
                soup.find("article") or
                soup.find(class_=re.compile(r"entry-content|post-content|article-content")) or
                soup.find("main")
            )
            if article:
                for el in article.find_all(["nav", "aside", "footer", "script", "style"]):
                    el.decompose()
                text = article.get_text(separator="\n", strip=True)
                if len(text.split()) > 50:
                    return text
            return soup.get_text(separator="\n", strip=True)[:8000]
        except Exception as e:
            print(f"  [!] Failed to fetch article: {e}")
            return ""


def score_article_with_ceo(article_text: str, iteration: int) -> dict:
    """Send article to Claude Opus (or GPT-4o fallback) for scoring. Returns parsed JSON."""
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    raw = None

    if anthropic_key:
        print(f"  [~] CEO (claude-opus-4-6) scoring iteration {iteration}...")
        ceo_client = anthropic.Anthropic(api_key=anthropic_key)
        message = ceo_client.messages.create(
            model="claude-opus-4-6",
            max_tokens=2000,
            system=CEO_SYSTEM,
            messages=[{"role": "user", "content": f"Oceń poniższy artykuł:\n\n{article_text[:7000]}"}]
        )
        raw = message.content[0].text

    elif openai_key:
        print(f"  [~] CEO (gpt-4o — fallback, no ANTHROPIC_API_KEY) scoring iteration {iteration}...")
        try:
            from openai import OpenAI as _OAI
            oai = _OAI(api_key=openai_key)
            resp = oai.chat.completions.create(
                model="gpt-4o",
                max_tokens=2000,
                messages=[
                    {"role": "system", "content": CEO_SYSTEM},
                    {"role": "user", "content": f"Oceń poniższy artykuł:\n\n{article_text[:7000]}"}
                ]
            )
            raw = resp.choices[0].message.content
        except ImportError:
            print("  [!] pip install openai")
            sys.exit(1)
    else:
        print("\n  [!] Neither ANTHROPIC_API_KEY nor OPENAI_API_KEY is set!")
        print("  [!] Add to backend/.env:  ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    if not raw:
        return {"total": 0, "verdict": "FAIL", "top_issues": ["Empty CEO response"], "prompt_fixes": []}

    # Extract JSON block
    json_match = re.search(r'```(?:json)?\s*(\{[\s\S]+?\})\s*```', raw)
    if not json_match:
        json_match = re.search(r'(\{[\s\S]+\})', raw)
    if not json_match:
        print(f"  [!] CEO response not parseable:\n{raw[:500]}")
        return {"total": 0, "verdict": "FAIL", "top_issues": [raw[:200]], "prompt_fixes": []}

    try:
        result = json.loads(json_match.group(1))
        return result
    except json.JSONDecodeError as e:
        print(f"  [!] JSON parse error: {e}\n{json_match.group(1)[:300]}")
        return {"total": 0, "verdict": "FAIL", "top_issues": ["JSON parse failed"], "prompt_fixes": []}


def apply_prompt_fixes(fixes: list[str], top_issues: list[str], iteration: int) -> bool:
    """
    Use Claude Opus (or GPT-4o fallback) to modify openai_service.py based on CEO feedback.
    Returns True if changes were made.
    """
    if not fixes and not top_issues:
        return False

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    # Read current service file
    current_code = OPENAI_SERVICE.read_text(encoding="utf-8")

    print(f"  [~] Applying prompt fixes (iteration {iteration})...")

    fix_instructions = "\n".join(f"- {f}" for f in fixes)
    issue_descriptions = "\n".join(f"- {i}" for i in top_issues)

    prompt = f"""Masz za zadanie zmodyfikować prompty GPT w pliku Python aby poprawić jakość generowanych artykułów SEO.

PROBLEMY ZNALEZIONE W ARTYKULE:
{issue_descriptions}

KONKRETNE POPRAWKI DO ZAIMPLEMENTOWANIA W PROMPTACH:
{fix_instructions}

AKTUALNY KOD (openai_service.py — tylko fragmenty z promptami):
Sekcje do modyfikacji:
- intro_system (linie ~906-960) — prompt wstępu
- section_system (linie ~986-1063) — prompt sekcji
- section_user (linie ~1073-1091) — user message sekcji
- _concl_system (linie ~1153) — prompt zakończenia
- _faq_system (linie ~1212) — prompt FAQ

INSTRUKCJE:
1. Wygeneruj TYLKO bloki "old_string" i "new_string" dla każdej zmiany
2. Zmieniaj TYLKO prompty/stringi/instrukcje — NIE logikę Pythona
3. Zmiany muszą być konkretne i dotyczyć wykrytych problemów
4. Format odpowiedzi (JSON lista zmian):
```json
[
  {{
    "description": "krótki opis zmiany",
    "old_string": "dokładny fragment do zastąpienia (min 3 słowa kontekstu)",
    "new_string": "nowy fragment"
  }}
]
```

WAŻNE: old_string musi być DOKŁADNY fragment z kodu (case-sensitive, ze spacjami).
Podaj max 4 zmiany, każda skoncentrowana na najważniejszym problemie.

KOD PLIKU (fragment z promptami — znaki 38000-75000):
{current_code[38000:75000]}
"""

    raw = None
    if anthropic_key:
        fix_client = anthropic.Anthropic(api_key=anthropic_key)
        message = fix_client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = message.content[0].text
    elif openai_key:
        try:
            from openai import OpenAI as _OAI
            oai = _OAI(api_key=openai_key)
            resp = oai.chat.completions.create(
                model="gpt-4o",
                max_tokens=4000,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = resp.choices[0].message.content
        except ImportError:
            return False
    else:
        return False

    if not raw:
        return False
    json_match = re.search(r'```(?:json)?\s*(\[[\s\S]+?\])\s*```', raw)
    if not json_match:
        json_match = re.search(r'(\[[\s\S]+\])', raw)

    if not json_match:
        print(f"  [!] Fix agent response not parseable:\n{raw[:400]}")
        return False

    try:
        changes = json.loads(json_match.group(1))
    except json.JSONDecodeError as e:
        print(f"  [!] Changes JSON parse error: {e}")
        return False

    applied = 0
    for change in changes:
        old = change.get("old_string", "")
        new = change.get("new_string", "")
        desc = change.get("description", "")
        if not old or not new or old == new:
            continue
        if old in current_code:
            current_code = current_code.replace(old, new, 1)
            print(f"    [+] Applied: {desc}")
            applied += 1
        else:
            print(f"    [~] Not found (skipped): {desc[:60]}")

    if applied > 0:
        OPENAI_SERVICE.write_text(current_code, encoding="utf-8")
        print(f"  [+] Wrote {applied} change(s) to openai_service.py")
        return True

    print("  [~] No changes applied (strings not found in file)")
    return False


def git_commit_and_push(iteration: int, score: int) -> bool:
    """Commit changes and push to trigger Railway deploy."""
    rc, _, err = _run("git add backend/services/openai_service.py backend/services/content_writer_service.py")
    if rc != 0:
        print(f"  [!] git add failed: {err}")
        return False

    rc, stdout, _ = _run("git diff --cached --stat")
    if "0 files changed" in stdout or not stdout:
        print("  [~] No staged changes to commit")
        return False

    commit_msg = f"quality-agent iter {iteration}: score={score} — auto prompt improvement"
    rc, _, err = _run(f'git commit -m "{commit_msg}"')
    if rc != 0:
        print(f"  [!] git commit failed: {err}")
        return False

    rc, stdout, err = _run("git push")
    if rc != 0:
        print(f"  [!] git push failed: {err}")
        return False

    print(f"  [+] Pushed to GitHub — Railway deploy started")
    return True


def wait_for_railway_deploy():
    """Wait for Railway to finish deploying."""
    print(f"  [~] Waiting {RAILWAY_DEPLOY_WAIT}s for Railway deploy...", end="", flush=True)
    for i in range(RAILWAY_DEPLOY_WAIT // 10):
        time.sleep(10)
        print(".", end="", flush=True)
    print()


async def run_iteration(domain: str, iteration: int) -> Optional[dict]:
    """Run one full iteration: generate → score. Returns score dict."""
    print(f"\n{'='*60}")
    print(f"ITERATION {iteration}")
    print(f"{'='*60}")

    # 1. Trigger article generation
    job_id = await trigger_article(domain)
    if not job_id:
        return None

    # 2. Wait for article to be published
    url = await wait_for_job(job_id)
    if not url:
        print("  [!] No URL — skipping score for this iteration")
        return None

    print(f"  [+] Article published: {url}")

    # 3. Fetch article content
    article_text = await fetch_article_text(url)
    if not article_text:
        print("  [!] Could not fetch article text")
        return None

    word_count = len(article_text.split())
    print(f"  [+] Fetched {word_count} words")

    # 4. CEO scores the article
    result = score_article_with_ceo(article_text, iteration)

    total = result.get("total", 0)
    scores = result.get("scores", {})
    verdict = result.get("verdict", "FAIL")
    issues = result.get("top_issues", [])
    fixes = result.get("prompt_fixes", [])

    print(f"\n  CEO VERDICT: {verdict} -- Total: {total}/100")
    print("  Scores:")
    for k, v in scores.items():
        bar = "#" * v + "." * (10 - v)
        print(f"    {k:25s} [{bar}] {v}/10")
    print(f"\n  Top issues:")
    for issue in issues:
        print(f"    • {issue}")
    print(f"\n  Prompt fixes proposed:")
    for fix in fixes:
        print(f"    → {fix}")

    result["url"] = url
    return result


async def main():
    parser = argparse.ArgumentParser(description="Quality Iteration Agent")
    parser.add_argument("--domain", default=DEFAULT_DOMAIN)
    parser.add_argument("--max-iter", type=int, default=DEFAULT_MAX_ITER)
    parser.add_argument("--skip-deploy-wait", action="store_true",
                        help="Skip waiting for Railway deploy (testing only)")
    args = parser.parse_args()

    print(f"Quality Iteration Agent")
    print(f"Domain: {args.domain}")
    print(f"Max iterations: {args.max_iter}")
    print(f"Target score: 80/100")
    print(f"CEO model: claude-opus-4-6")

    history = []

    for iteration in range(1, args.max_iter + 1):
        result = await run_iteration(args.domain, iteration)
        if not result:
            print("  [!] Iteration failed — skipping fix, retrying...")
            continue

        total = result.get("total", 0)
        history.append({"iteration": iteration, "score": total, "url": result.get("url")})

        if total >= 80 or result.get("verdict") == "PASS":
            print(f"\n{'='*60}")
            print(f"✓ CEO VERDICT: PASS — Score {total}/100 >= 80")
            print(f"URL: {result.get('url')}")
            print(f"{'='*60}")
            break

        if iteration == args.max_iter:
            print(f"\n[!] Max iterations reached. Best score: {max(h['score'] for h in history)}/100")
            break

        # Apply fixes to prompts
        changed = apply_prompt_fixes(
            result.get("prompt_fixes", []),
            result.get("top_issues", []),
            iteration
        )

        if changed:
            pushed = git_commit_and_push(iteration, total)
            if pushed and not args.skip_deploy_wait:
                wait_for_railway_deploy()
        else:
            print("  [~] No prompt changes — Railway deploy not needed")

    print("\nScore history:")
    for h in history:
        print(f"  Iter {h['iteration']:2d}: {h['score']:3d}/100  {h.get('url', '')}")


if __name__ == "__main__":
    asyncio.run(main())

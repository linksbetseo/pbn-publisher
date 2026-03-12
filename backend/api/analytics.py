"""
Analytics API — anchor text diversity, link velocity monitoring, content uniqueness scoring.
"""
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite
from fastapi import APIRouter, Query

from config import DB_PATH

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ANCHOR_RE = re.compile(
    r'<a[^>]*href=["\'][^"\']*["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


def _extract_anchors(html: str) -> list[str]:
    """Return list of anchor texts found in *html*."""
    if not html:
        return []
    return [a.strip() for a in _ANCHOR_RE.findall(html) if a.strip()]


def _content_shingles(text: str, n: int = 3) -> set:
    """Extract n-word shingles from *text* for similarity comparison."""
    words = re.findall(r"\w+", text.lower())
    if len(words) < n:
        return set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _jaccard_similarity(set_a: set, set_b: set) -> float:
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def _strip_html(html: str) -> str:
    """Rough HTML tag removal for plain-text extraction."""
    return re.sub(r"<[^>]+>", " ", html or "")


# ---------------------------------------------------------------------------
# Feature #6 — Anchor Text Diversity
# ---------------------------------------------------------------------------

@router.get("/anchor-diversity")
async def anchor_diversity(
    client_domain: Optional[str] = Query(None, description="Filter by client domain"),
):
    """Anchor-text distribution and diversity score per client domain."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Gather content from posts table
        if client_domain:
            async with db.execute(
                "SELECT client_domain, content FROM posts WHERE client_domain = ?",
                (client_domain,),
            ) as cur:
                posts_rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT client_domain, content FROM posts"
            ) as cur:
                posts_rows = await cur.fetchall()

        # Gather content from domain_keywords table
        try:
            if client_domain:
                async with db.execute(
                    "SELECT client_domain, content FROM domain_keywords WHERE client_domain = ?",
                    (client_domain,),
                ) as cur:
                    dk_rows = await cur.fetchall()
            else:
                async with db.execute(
                    "SELECT client_domain, content FROM domain_keywords"
                ) as cur:
                    dk_rows = await cur.fetchall()
        except Exception:
            dk_rows = []

    # Aggregate anchors per client_domain
    domain_anchors: dict[str, list[str]] = defaultdict(list)
    for row in list(posts_rows) + list(dk_rows):
        cd = row["client_domain"]
        if not cd:
            continue
        anchors = _extract_anchors(row["content"] or "")
        domain_anchors[cd].extend(anchors)

    results = []
    for cd, anchors in sorted(domain_anchors.items()):
        total = len(anchors)
        if total == 0:
            results.append({
                "client_domain": cd,
                "total_links": 0,
                "unique_anchors": 0,
                "diversity_score": 0.0,
                "distribution": [],
                "warnings": [],
            })
            continue

        counts: dict[str, int] = defaultdict(int)
        for a in anchors:
            counts[a.lower()] += 1

        unique = len(counts)
        diversity = round(unique / total, 4)

        distribution = sorted(
            [
                {
                    "anchor": anchor,
                    "count": cnt,
                    "pct": round(cnt / total * 100, 1),
                }
                for anchor, cnt in counts.items()
            ],
            key=lambda d: d["count"],
            reverse=True,
        )

        warnings: list[str] = []
        for item in distribution:
            if item["pct"] > 15:
                warnings.append(
                    f"Over-optimized: '{item['anchor']}' used {item['pct']}% (target <15%)"
                )

        results.append({
            "client_domain": cd,
            "total_links": total,
            "unique_anchors": unique,
            "diversity_score": diversity,
            "distribution": distribution,
            "warnings": warnings,
        })

    # If filtering by a single domain, return the object directly; otherwise list
    if client_domain:
        return results[0] if results else {
            "client_domain": client_domain,
            "total_links": 0,
            "unique_anchors": 0,
            "diversity_score": 0.0,
            "distribution": [],
            "warnings": [],
        }
    return {"domains": results}


# ---------------------------------------------------------------------------
# Feature #8 — Link Velocity Monitoring
# ---------------------------------------------------------------------------

@router.get("/link-velocity")
async def link_velocity(
    client_domain: Optional[str] = Query(None, description="Filter by client domain"),
):
    """Weekly publishing velocity per client domain with spike detection."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Posts table — group by week + client_domain
        params: tuple = ()
        where_clause = ""
        if client_domain:
            where_clause = " AND client_domain = ?"
            params = (client_domain,)

        async with db.execute(
            f"""SELECT client_domain,
                       strftime('%Y-W%W', created_at) AS week,
                       COUNT(*) AS links
                FROM posts
                WHERE status = 'published'{where_clause}
                GROUP BY client_domain, week
                ORDER BY client_domain, week""",
            params,
        ) as cur:
            post_rows = await cur.fetchall()

        # domain_keywords table (autopilot)
        try:
            async with db.execute(
                f"""SELECT client_domain,
                           strftime('%Y-W%W', published_at) AS week,
                           COUNT(*) AS links
                    FROM domain_keywords
                    WHERE status = 'published'{where_clause}
                    GROUP BY client_domain, week
                    ORDER BY client_domain, week""",
                params,
            ) as cur:
                dk_rows = await cur.fetchall()
        except Exception:
            dk_rows = []

    # Merge both sources
    velocity_map: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in list(post_rows) + list(dk_rows):
        cd = row["client_domain"]
        week = row["week"]
        if cd and week:
            velocity_map[cd][week] += row["links"]

    # Current ISO week label
    now = datetime.now(timezone.utc)
    current_week_label = now.strftime("%Y-W%W")

    domains_out = []
    for cd in sorted(velocity_map):
        weeks = velocity_map[cd]
        weekly_data = [
            {"week": w, "links": c}
            for w, c in sorted(weeks.items())
        ]
        totals = [d["links"] for d in weekly_data]
        avg_weekly = round(sum(totals) / len(totals), 2) if totals else 0

        current_week_links = weeks.get(current_week_label, 0)

        # Determine velocity status
        if avg_weekly == 0:
            status = "low"
        elif current_week_links > 2 * avg_weekly:
            status = "spike"
        elif current_week_links > 1.5 * avg_weekly:
            status = "high"
        elif current_week_links < 0.3 * avg_weekly:
            status = "low"
        else:
            status = "normal"

        warnings: list[str] = []
        if status == "spike":
            ratio = round(current_week_links / avg_weekly, 1) if avg_weekly else 0
            warnings.append(
                f"Current week {current_week_links} links is {ratio}x average ({avg_weekly}) "
                f"— consider slowing down"
            )
        elif status == "high":
            ratio = round(current_week_links / avg_weekly, 1) if avg_weekly else 0
            warnings.append(
                f"Current week {current_week_links} links is {ratio}x average ({avg_weekly}) "
                f"— approaching spike territory"
            )

        domains_out.append({
            "client_domain": cd,
            "weekly_velocity": weekly_data,
            "avg_weekly": avg_weekly,
            "current_week": current_week_links,
            "velocity_status": status,
            "warnings": warnings,
        })

    return {"domains": domains_out}


# ---------------------------------------------------------------------------
# Feature #9 — Content Uniqueness Scoring
# ---------------------------------------------------------------------------

@router.get("/content-uniqueness")
async def content_uniqueness(
    my_domain_id: Optional[int] = Query(None, description="Filter by PBN domain ID"),
    limit: int = Query(50, ge=1, le=500, description="Max articles to analyse per domain"),
):
    """Shingling-based content uniqueness per PBN domain."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Fetch published posts grouped by my_domain_id
        if my_domain_id is not None:
            async with db.execute(
                """SELECT p.id, p.title, p.content, p.my_domain_id, md.domain
                   FROM posts p
                   JOIN my_domains md ON md.id = p.my_domain_id
                   WHERE p.status = 'published' AND p.my_domain_id = ?
                   ORDER BY p.created_at DESC
                   LIMIT ?""",
                (my_domain_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                """SELECT p.id, p.title, p.content, p.my_domain_id, md.domain
                   FROM posts p
                   JOIN my_domains md ON md.id = p.my_domain_id
                   WHERE p.status = 'published'
                   ORDER BY p.my_domain_id, p.created_at DESC""",
            ) as cur:
                rows = await cur.fetchall()

    # Group by domain
    domain_posts: dict[int, list[dict]] = defaultdict(list)
    domain_names: dict[int, str] = {}
    for row in rows:
        did = row["my_domain_id"]
        domain_names[did] = row["domain"]
        domain_posts[did].append({
            "id": row["id"],
            "title": row["title"],
            "content": row["content"] or "",
        })

    results = []
    for did in sorted(domain_posts):
        posts = domain_posts[did]
        if my_domain_id is None:
            posts = posts[:limit]  # cap per domain when listing all

        # Pre-compute shingles
        shingles_cache: list[tuple[dict, set]] = []
        for p in posts:
            plain = _strip_html(p["content"])
            shingles_cache.append((p, _content_shingles(plain)))

        similar_pairs: list[dict] = []
        n = len(shingles_cache)
        # Single O(n²/2) pass — compute pairwise similarities once, track max per article
        max_sims: list[float] = [0.0] * n
        for i in range(n):
            for j in range(i + 1, n):
                sim = _jaccard_similarity(shingles_cache[i][1], shingles_cache[j][1])
                # Track max similarity per article (for uniqueness score)
                if sim > max_sims[i]:
                    max_sims[i] = sim
                if sim > max_sims[j]:
                    max_sims[j] = sim
                if sim > 0.4:
                    similar_pairs.append({
                        "post_a": {"id": shingles_cache[i][0]["id"], "title": shingles_cache[i][0]["title"]},
                        "post_b": {"id": shingles_cache[j][0]["id"], "title": shingles_cache[j][0]["title"]},
                        "similarity": round(sim, 4),
                        "status": "warning",
                    })

        # Average uniqueness: 1 - avg(max similarity per article)
        if n >= 2:
            avg_uniqueness = round(1 - (sum(max_sims) / len(max_sims)), 4)
        else:
            avg_uniqueness = 1.0

        similar_pairs.sort(key=lambda p: p["similarity"], reverse=True)

        results.append({
            "my_domain_id": did,
            "domain": domain_names[did],
            "total_articles": len(posts),
            "avg_uniqueness": avg_uniqueness,
            "similar_pairs": similar_pairs,
        })

    if my_domain_id is not None:
        return results[0] if results else {
            "my_domain_id": my_domain_id,
            "domain": None,
            "total_articles": 0,
            "avg_uniqueness": 1.0,
            "similar_pairs": [],
        }
    return {"domains": results}

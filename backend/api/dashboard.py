"""
Dashboard stats API — aggregated KPIs for the main dashboard view.
"""
import time
import aiosqlite
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter
from config import DB_PATH

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# Simple in-memory cache (60s TTL) to avoid 8 SQL queries per dashboard load
_cache: dict = {"data": None, "ts": 0}
_CACHE_TTL = 60


@router.get("/stats")
async def dashboard_stats():
    """Returns all KPIs needed by the dashboard in one query."""
    now_ts = time.time()
    if _cache["data"] and (now_ts - _cache["ts"]) < _CACHE_TTL:
        return _cache["data"]
    today = datetime.now(timezone.utc).date().isoformat()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    prev_month_start = (datetime.now(timezone.utc) - timedelta(days=60)).date().isoformat()
    prev_month_end = (datetime.now(timezone.utc) - timedelta(days=31)).date().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Batch 1: core counts in a single query
        async with db.execute(
            """SELECT
                SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) as total_domains,
                SUM(CASE WHEN wp_ok=1 THEN 1 ELSE 0 END) as wp_ok_domains
               FROM my_domains"""
        ) as cur:
            row = await cur.fetchone()
            total_domains = row["total_domains"] or 0
            wp_ok_domains = row["wp_ok_domains"] or 0

        async with db.execute("SELECT COUNT(*) FROM clients") as cur:
            total_clients = (await cur.fetchone())[0]

        # Batch 2: post stats — combine manual posts + autopilot domain_keywords
        async with db.execute(
            """SELECT
                SUM(CASE WHEN status='published' THEN 1 ELSE 0 END) as total_published,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as total_failed,
                SUM(CASE WHEN status='published' AND DATE(created_at)=? THEN 1 ELSE 0 END) as posts_today,
                SUM(CASE WHEN status='published' AND DATE(created_at)>=? THEN 1 ELSE 0 END) as posts_week,
                SUM(CASE WHEN status='published' AND DATE(created_at)>=? THEN 1 ELSE 0 END) as posts_month
               FROM posts""",
            (today, week_ago, month_ago)
        ) as cur:
            row = await cur.fetchone()
            manual_published = row["total_published"] or 0
            total_failed = row["total_failed"] or 0
            manual_today = row["posts_today"] or 0
            manual_week = row["posts_week"] or 0
            manual_month = row["posts_month"] or 0

        # Add autopilot published counts
        try:
            async with db.execute(
                """SELECT
                    SUM(CASE WHEN status='published' THEN 1 ELSE 0 END) as ap_published,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as ap_failed,
                    SUM(CASE WHEN status='published' AND DATE(published_at)=? THEN 1 ELSE 0 END) as ap_today,
                    SUM(CASE WHEN status='published' AND DATE(published_at)>=? THEN 1 ELSE 0 END) as ap_week,
                    SUM(CASE WHEN status='published' AND DATE(published_at)>=? THEN 1 ELSE 0 END) as ap_month
                   FROM domain_keywords""",
                (today, week_ago, month_ago)
            ) as cur:
                row = await cur.fetchone()
                ap_published = row["ap_published"] or 0
                ap_failed = row["ap_failed"] or 0
                ap_today = row["ap_today"] or 0
                ap_week = row["ap_week"] or 0
                ap_month = row["ap_month"] or 0
        except Exception:
            ap_published = ap_failed = ap_today = ap_week = ap_month = 0

        total_published = manual_published + ap_published
        total_failed = total_failed + ap_failed
        posts_today = manual_today + ap_today
        posts_week = manual_week + ap_week
        posts_month = manual_month + ap_month

        # Autopilot stats
        try:
            async with db.execute(
                """SELECT
                    SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) as active_schedules,
                    SUM(CASE WHEN map_generated=1 THEN 1 ELSE 0 END) as maps_generated
                   FROM domain_schedules"""
            ) as cur:
                row = await cur.fetchone()
                active_schedules = row["active_schedules"] or 0
                maps_generated = row["maps_generated"] or 0

            async with db.execute(
                """SELECT
                    SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending_keywords,
                    SUM(CASE WHEN status='published' THEN 1 ELSE 0 END) as published_keywords,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed_keywords
                   FROM domain_keywords"""
            ) as cur:
                row = await cur.fetchone()
                pending_keywords = row["pending_keywords"] or 0
                published_keywords = row["published_keywords"] or 0
                failed_keywords = row["failed_keywords"] or 0
        except Exception:
            active_schedules = pending_keywords = published_keywords = failed_keywords = maps_generated = 0

        # Domain health summary
        try:
            async with db.execute(
                """SELECT health_score, COUNT(*) as cnt
                   FROM domain_health_snapshots
                   WHERE id IN (SELECT MAX(id) FROM domain_health_snapshots GROUP BY my_domain_id)
                   GROUP BY health_score"""
            ) as cur:
                health_rows = await cur.fetchall()
            health_dist = {r["health_score"]: r["cnt"] for r in health_rows}

            async with db.execute(
                """SELECT COUNT(*) FROM domain_health_snapshots
                   WHERE id IN (SELECT MAX(id) FROM domain_health_snapshots GROUP BY my_domain_id)
                   AND days_to_expiry IS NOT NULL AND days_to_expiry < 30"""
            ) as cur:
                expiring_soon = (await cur.fetchone())[0]
        except Exception:
            health_dist = {}
            expiring_soon = 0

        # Daily trend — last 30 days (manual + autopilot combined)
        try:
            async with db.execute(
                """SELECT day, SUM(count) as count FROM (
                    SELECT DATE(created_at) as day, COUNT(*) as count
                    FROM posts WHERE status='published' AND DATE(created_at) >= ?
                    GROUP BY DATE(created_at)
                    UNION ALL
                    SELECT DATE(published_at) as day, COUNT(*) as count
                    FROM domain_keywords WHERE status='published' AND DATE(published_at) >= ?
                    GROUP BY DATE(published_at)
                ) GROUP BY day ORDER BY day""",
                (month_ago, month_ago)
            ) as cur:
                trend_rows = await cur.fetchall()
            trend = [{"day": r["day"], "count": r["count"]} for r in trend_rows]
        except Exception:
            trend = []

        # Top 5 most active domains (manual + autopilot combined)
        try:
            async with db.execute(
                """SELECT domain, SUM(count) as count FROM (
                    SELECT md.domain, COUNT(p.id) as count
                    FROM posts p JOIN my_domains md ON md.id = p.my_domain_id
                    WHERE p.status='published' GROUP BY md.domain
                    UNION ALL
                    SELECT ds.domain, COUNT(dk.id) as count
                    FROM domain_keywords dk JOIN domain_schedules ds ON ds.id = dk.schedule_id
                    WHERE dk.status='published' GROUP BY ds.domain
                ) GROUP BY domain ORDER BY count DESC LIMIT 5"""
            ) as cur:
                top_domains_rows = await cur.fetchall()
            top_domains = [{"domain": r["domain"], "count": r["count"]} for r in top_domains_rows]
        except Exception:
            top_domains = []

        # Recent autopilot jobs (last 5)
        try:
            async with db.execute(
                """SELECT job_id, schedule_id, published, failed, error, created_at, done
                   FROM autopilot_jobs ORDER BY created_at DESC LIMIT 5"""
            ) as cur:
                job_rows = await cur.fetchall()
            recent_jobs = [dict(r) for r in job_rows]
        except Exception:
            recent_jobs = []

        # B3: Previous month published count (for MoM comparison)
        prev_month_published = 0
        try:
            async with db.execute(
                """SELECT COUNT(*) FROM posts
                   WHERE status='published' AND DATE(created_at) >= ? AND DATE(created_at) <= ?""",
                (prev_month_start, prev_month_end)
            ) as cur:
                prev_month_published = (await cur.fetchone())[0] or 0
            # Add autopilot
            async with db.execute(
                """SELECT COUNT(*) FROM domain_keywords
                   WHERE status='published' AND DATE(published_at) >= ? AND DATE(published_at) <= ?""",
                (prev_month_start, prev_month_end)
            ) as cur:
                prev_month_published += (await cur.fetchone())[0] or 0
        except Exception:
            pass

        # B8: Recent errors (last 10 failed posts/keywords)
        recent_errors = []
        try:
            async with db.execute(
                """SELECT 'post' as source, title as label, my_domain_id, created_at as ts
                   FROM posts WHERE status='failed'
                   ORDER BY created_at DESC LIMIT 5"""
            ) as cur:
                recent_errors.extend([dict(r) for r in await cur.fetchall()])
            async with db.execute(
                """SELECT 'autopilot' as source, keyword as label, my_domain_id, published_at as ts
                   FROM domain_keywords WHERE status='failed'
                   ORDER BY rowid DESC LIMIT 5"""
            ) as cur:
                recent_errors.extend([dict(r) for r in await cur.fetchall()])
        except Exception:
            pass

        # B2: Avg word count — approximate from content LENGTH (avoids loading full HTML)
        avg_word_count = 0
        try:
            async with db.execute(
                """SELECT AVG(LENGTH(content) - LENGTH(REPLACE(content, ' ', ''))) as avg_spaces
                   FROM (SELECT content FROM posts WHERE status='published'
                         ORDER BY created_at DESC LIMIT 20)"""
            ) as cur:
                row = await cur.fetchone()
                # word_count ≈ spaces + 1; HTML tags add ~30% overhead
                avg_spaces = row[0] or 0
                if avg_spaces > 0:
                    avg_word_count = round(avg_spaces * 0.7)  # compensate for HTML tag spaces
        except Exception:
            pass

    # Next daily cron: randomized windows (6-10, 11-14, 19-22 UTC) — show approximate range
    now_utc = datetime.now(timezone.utc)
    # Show next window start as approximate time
    _cron_windows = [(6, 10), (11, 14), (19, 22)]
    next_run = None
    for w_start, w_end in _cron_windows:
        candidate = now_utc.replace(hour=w_start, minute=0, second=0, microsecond=0)
        if candidate > now_utc:
            next_run = candidate
            break
    if not next_run:
        next_run = (now_utc + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
    next_cron_utc = next_run.strftime("%Y-%m-%dT%H:%M:%S") + " (approx)"

    result = {
        "total_domains": total_domains,
        "wp_ok_domains": wp_ok_domains,
        "total_clients": total_clients,
        "total_published": total_published,
        "total_failed": total_failed,
        "posts_today": posts_today,
        "posts_week": posts_week,
        "posts_month": posts_month,
        "active_schedules": active_schedules,
        "pending_keywords": pending_keywords,
        "published_keywords": published_keywords,
        "failed_keywords": failed_keywords,
        "maps_generated": maps_generated,
        "health_distribution": health_dist,
        "expiring_soon": expiring_soon,
        "trend_30d": trend,
        "top_domains": top_domains,
        "recent_jobs": recent_jobs,
        "next_cron_utc": next_cron_utc,
        "prev_month_published": prev_month_published,
        "recent_errors": recent_errors[:10],
        "avg_word_count": avg_word_count,
    }
    _cache["data"] = result
    _cache["ts"] = time.time()
    return result

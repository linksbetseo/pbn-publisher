"""
Dashboard stats API — aggregated KPIs for the main dashboard view.
"""
import aiosqlite
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter
from config import DB_PATH

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/stats")
async def dashboard_stats():
    """Returns all KPIs needed by the dashboard in one query."""
    today = datetime.utcnow().date().isoformat()
    week_ago = (datetime.utcnow() - timedelta(days=7)).date().isoformat()
    month_ago = (datetime.utcnow() - timedelta(days=30)).date().isoformat()

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

        # Batch 2: post stats in a single query
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
            total_published = row["total_published"] or 0
            total_failed = row["total_failed"] or 0
            posts_today = row["posts_today"] or 0
            posts_week = row["posts_week"] or 0
            posts_month = row["posts_month"] or 0

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

        # Daily trend — last 30 days
        try:
            async with db.execute(
                """SELECT DATE(created_at) as day, COUNT(*) as count
                   FROM posts WHERE status='published' AND DATE(created_at) >= ?
                   GROUP BY DATE(created_at) ORDER BY day""",
                (month_ago,)
            ) as cur:
                trend_rows = await cur.fetchall()
            trend = [{"day": r["day"], "count": r["count"]} for r in trend_rows]
        except Exception:
            trend = []

        # Top 5 most active domains
        try:
            async with db.execute(
                """SELECT md.domain, COUNT(p.id) as count
                   FROM posts p
                   JOIN my_domains md ON md.id = p.my_domain_id
                   WHERE p.status='published'
                   GROUP BY md.domain ORDER BY count DESC LIMIT 5"""
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

    # Next daily cron: 08:00 UTC
    now_utc = datetime.now(timezone.utc)
    next_run = now_utc.replace(hour=8, minute=0, second=0, microsecond=0)
    if next_run <= now_utc:
        next_run = next_run + timedelta(days=1)
    next_cron_utc = next_run.strftime("%Y-%m-%dT%H:%M:%S")

    return {
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
    }

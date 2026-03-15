"""
Deindex Detection API — checks if published posts are still alive (HTTP 200).

Endpoints:
- POST /api/deindex/scan        — scan all published posts for dead URLs
- GET  /api/deindex/results     — latest check results grouped by domain
- GET  /api/deindex/results/all — flat list of all check results
"""

import asyncio
import logging
from datetime import datetime, timezone

import aiosqlite
import httpx
from fastapi import APIRouter, Query

from config import DB_PATH

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/deindex", tags=["deindex"])

# Concurrency limit for HTTP checks
_CHECK_SEM = asyncio.Semaphore(10)


async def ensure_table():
    """Create the deindex_checks table if it doesn't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS deindex_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER,
                url TEXT NOT NULL,
                domain TEXT,
                status_code INTEGER,
                is_alive INTEGER DEFAULT 1,
                checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_deindex_checked ON deindex_checks(checked_at)"
        )
        await db.commit()


async def _check_url(url: str) -> tuple[int, bool]:
    """HEAD-request a URL. Returns (status_code, is_alive)."""
    async with _CHECK_SEM:
        try:
            async with httpx.AsyncClient(
                timeout=15, verify=False, follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; PBNPublisher/1.0)"}
            ) as client:
                resp = await client.head(url)
                # Some servers block HEAD, fallback to GET
                if resp.status_code == 405:
                    resp = await client.get(url)
                is_alive = resp.status_code < 400
                return resp.status_code, is_alive
        except httpx.ConnectError:
            return 0, False
        except httpx.TimeoutException:
            return 0, False
        except Exception as e:
            logger.warning(f"Deindex check error for {url}: {e}")
            return 0, False


async def run_deindex_scan() -> dict:
    """Scan all published posts and domain_keywords for dead URLs.

    Returns summary dict with counts.
    """
    await ensure_table()

    # Collect all published URLs from both tables
    urls_to_check: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Posts table
        async with db.execute(
            """SELECT p.id as post_id, p.wp_post_url as url, md.domain
               FROM posts p
               JOIN my_domains md ON md.id = p.my_domain_id
               WHERE p.status = 'published' AND p.wp_post_url IS NOT NULL AND p.wp_post_url != ''"""
        ) as cur:
            for row in await cur.fetchall():
                urls_to_check.append({"post_id": row["post_id"], "url": row["url"], "domain": row["domain"]})

        # domain_keywords table (autopilot)
        try:
            async with db.execute(
                """SELECT dk.id as post_id, dk.wp_post_url as url, md.domain
                   FROM domain_keywords dk
                   JOIN domain_schedules ds ON ds.id = dk.schedule_id
                   JOIN my_domains md ON md.id = ds.my_domain_id
                   WHERE dk.status = 'published' AND dk.wp_post_url IS NOT NULL AND dk.wp_post_url != ''"""
            ) as cur:
                for row in await cur.fetchall():
                    urls_to_check.append({"post_id": row["post_id"], "url": row["url"], "domain": row["domain"]})
        except Exception:
            pass  # table may not exist

    if not urls_to_check:
        return {"total_checked": 0, "alive": 0, "dead": 0, "dead_urls": []}

    # Check all URLs in batches to limit memory usage
    async def _do_check(item: dict) -> dict:
        status_code, is_alive = await _check_url(item["url"])
        return {**item, "status_code": status_code, "is_alive": is_alive}

    results = []
    _batch_size = 50
    for i in range(0, len(urls_to_check), _batch_size):
        batch = urls_to_check[i:i + _batch_size]
        batch_results = await asyncio.gather(*[_do_check(item) for item in batch])
        results.extend(batch_results)

    # Store results in DB
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT INTO deindex_checks (post_id, url, domain, status_code, is_alive, checked_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(r["post_id"], r["url"], r["domain"], r["status_code"], int(r["is_alive"]), now) for r in results],
        )
        await db.commit()

    alive = sum(1 for r in results if r["is_alive"])
    dead = sum(1 for r in results if not r["is_alive"])
    dead_urls = [{"url": r["url"], "domain": r["domain"], "status_code": r["status_code"]}
                 for r in results if not r["is_alive"]]

    # Send Telegram alert if dead URLs found (respects user preference)
    if dead > 0:
        try:
            from api.notifications import should_notify
            if not await should_notify("notify_errors"):
                raise Exception("disabled")
            from services.telegram_service import send_telegram
            domains_affected = len(set(r["domain"] for r in results if not r["is_alive"]))
            msg = (
                f"<b>ALERT: Deindexed URLs detected</b>\n\n"
                f"Dead URLs: <b>{dead}</b> on <b>{domains_affected}</b> domain(s)\n"
                f"Total checked: {len(results)}\n\n"
            )
            for item in dead_urls[:10]:
                msg += f"  {item['url']} (HTTP {item['status_code']})\n"
            if dead > 10:
                msg += f"\n... and {dead - 10} more"
            await send_telegram(msg)
        except Exception as e:
            logger.warning(f"Telegram alert failed: {e}")

    return {"total_checked": len(results), "alive": alive, "dead": dead, "dead_urls": dead_urls}


@router.post("/scan")
async def scan_deindex():
    """Scan all published posts for dead/deindexed URLs."""
    result = await run_deindex_scan()
    return result


@router.get("/results")
async def get_results():
    """Get latest deindex check results grouped by domain."""
    await ensure_table()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Get the latest check timestamp
        async with db.execute("SELECT MAX(checked_at) as last_check FROM deindex_checks") as cur:
            row = await cur.fetchone()
            last_check = row["last_check"] if row else None

        if not last_check:
            return {"last_check": None, "domains": [], "total_alive": 0, "total_dead": 0}

        # Get results from the latest scan batch
        async with db.execute(
            """SELECT domain,
                      SUM(CASE WHEN is_alive = 1 THEN 1 ELSE 0 END) as alive_count,
                      SUM(CASE WHEN is_alive = 0 THEN 1 ELSE 0 END) as dead_count,
                      COUNT(*) as total
               FROM deindex_checks
               WHERE checked_at = ?
               GROUP BY domain
               ORDER BY dead_count DESC, domain""",
            (last_check,)
        ) as cur:
            domain_rows = [dict(r) for r in await cur.fetchall()]

        # Get dead URLs from the latest scan
        async with db.execute(
            """SELECT url, domain, status_code
               FROM deindex_checks
               WHERE checked_at = ? AND is_alive = 0
               ORDER BY domain, url""",
            (last_check,)
        ) as cur:
            dead_urls = [dict(r) for r in await cur.fetchall()]

    total_alive = sum(d["alive_count"] for d in domain_rows)
    total_dead = sum(d["dead_count"] for d in domain_rows)

    return {
        "last_check": last_check,
        "domains": domain_rows,
        "dead_urls": dead_urls,
        "total_alive": total_alive,
        "total_dead": total_dead,
    }


@router.get("/results/all")
async def get_all_results(limit: int = Query(100, ge=1, le=10000)):
    """Get all deindex check records (flat list, newest first)."""
    await ensure_table()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM deindex_checks ORDER BY checked_at DESC LIMIT ?",
            (limit,)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return rows

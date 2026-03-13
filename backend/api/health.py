"""
Domain Health monitoring endpoint.
For each domain fetches in parallel:
- DataForSEO: organic traffic + keywords count + domain rank
- WHOIS: expiry date (WhoisXML preferred, python-whois fallback)
- WP ping: checks if WP REST API responds (with WP auth for htpasswd sites)

Weekly cron saves snapshots to domain_health_snapshots table.
Snapshot progress tracked in domain_health_snapshot_progress table.
"""
import asyncio
import base64
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import httpx
from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import JSONResponse

from config import DB_PATH, DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD

router = APIRouter(prefix="/api/health", tags=["health"])
logger = logging.getLogger(__name__)

WHOISXML_KEY = os.getenv("WHOISXML_API_KEY", "")

# Per-domain hard timeout for live check
_DOMAIN_TIMEOUT = 25  # seconds

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS domain_health_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    my_domain_id INTEGER NOT NULL,
    domain TEXT NOT NULL,
    traffic INTEGER DEFAULT 0,
    keywords INTEGER DEFAULT 0,
    wp_ok INTEGER DEFAULT 0,
    expiry_date TEXT,
    days_to_expiry INTEGER,
    health_score TEXT DEFAULT 'weak',
    dr INTEGER DEFAULT NULL,
    snapped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_dhs_domain ON domain_health_snapshots(my_domain_id, snapped_at);

CREATE TABLE IF NOT EXISTS domain_health_snapshot_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    total INTEGER DEFAULT 0,
    done INTEGER DEFAULT 0,
    finished INTEGER DEFAULT 0,
    finished_at TEXT
);
"""


_tables_ensured = False


async def ensure_tables():
    global _tables_ensured
    if _tables_ensured:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_TABLES)
        # Migration: add dr column to snapshots if missing
        try:
            await db.execute("ALTER TABLE domain_health_snapshots ADD COLUMN dr INTEGER DEFAULT NULL")
        except Exception:
            pass
        # Clean up old progress records (keep only last 10)
        try:
            await db.execute(
                """DELETE FROM domain_health_snapshot_progress
                   WHERE id NOT IN (
                       SELECT id FROM domain_health_snapshot_progress
                       ORDER BY id DESC LIMIT 10
                   )"""
            )
        except Exception:
            pass
        await db.commit()
    _tables_ensured = True


# ── DataForSEO helpers ────────────────────────────────────────────────────────

async def _dfs_domain_metrics(domain: str, location_code: int = 2616, language_code: str = "pl") -> dict:
    """Fetch organic traffic + keyword count + domain rank from DataForSEO."""
    if not DATAFORSEO_LOGIN or not DATAFORSEO_PASSWORD:
        logger.warning("[Health] DFS credentials not set — skipping metrics")
        return {}
    creds = base64.b64encode(f"{DATAFORSEO_LOGIN}:{DATAFORSEO_PASSWORD}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}
    clean = re.sub(r"^https?://", "", domain).rstrip("/")
    payload = [{"target": clean, "location_code": location_code, "language_code": language_code}]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.dataforseo.com/v3/dataforseo_labs/google/domain_rank_overview/live",
                json=payload,
                headers=headers,
            )
            if resp.status_code != 200:
                logger.warning(f"[Health] DFS HTTP {resp.status_code} for {clean}")
                return {}
            data = resp.json()
        for task in data.get("tasks", []):
            status_code = task.get("status_code", 0)
            if status_code != 20000:
                logger.info(f"[Health] DFS task status {status_code} for {clean}: {task.get('status_message', '')}")
            for result in task.get("result", []):
                metrics = result.get("metrics", {}).get("organic", {})
                traffic = int(metrics.get("etv", 0) or 0)
                keywords = int(metrics.get("count", 0) or 0)
                # DataForSEO domain_rank_overview returns 'rank' at the result level
                dr = result.get("rank")
                if dr is not None:
                    dr = int(dr)
                logger.info(f"[Health] DFS {clean}: traffic={traffic}, keywords={keywords}, dr={dr}")
                return {
                    "traffic": traffic,
                    "keywords": keywords,
                    "dr": dr,
                }
        logger.info(f"[Health] DFS no results for {clean} (domain may have no organic data)")
    except Exception as e:
        logger.warning(f"[Health] DFS metrics failed for {domain}: {e}")
    return {}


# ── WHOIS helpers ─────────────────────────────────────────────────────────────

def _days_until(expiry_str: str) -> Optional[int]:
    if not expiry_str:
        return None
    for fmt in ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d-%b-%Y", "%Y%m%d"]:
        try:
            dt = datetime.strptime(expiry_str[:len(fmt)], fmt)
            return (dt.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days
        except Exception:
            continue
    return None


async def _whois_expiry(domain: str) -> dict:
    clean = re.sub(r"^https?://", "", domain).rstrip("/").split("/")[0]

    if WHOISXML_KEY:
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                resp = await client.get(
                    "https://www.whoisxmlapi.com/whoisserver/WhoisService",
                    params={"apiKey": WHOISXML_KEY, "domainName": clean, "outputFormat": "JSON"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    expiry = (
                        data.get("WhoisRecord", {}).get("registryData", {}).get("expiresDate")
                        or data.get("WhoisRecord", {}).get("expiresDate", "")
                    )
                    days = _days_until(expiry)
                    return {"expiry_date": expiry[:10] if expiry else None, "days_to_expiry": days}
        except Exception as e:
            logger.warning(f"[Health] WhoisXML failed for {clean}: {e}")

    try:
        import whois
        loop = asyncio.get_event_loop()
        w = await asyncio.wait_for(
            loop.run_in_executor(None, whois.whois, clean),
            timeout=10
        )
        expiry = w.expiration_date
        if isinstance(expiry, list):
            expiry = expiry[0]
        if expiry:
            expiry_str = expiry.strftime("%Y-%m-%d") if hasattr(expiry, "strftime") else str(expiry)[:10]
            try:
                dt = expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)
                days = (dt - datetime.now(timezone.utc)).days
            except Exception:
                days = None
            return {"expiry_date": expiry_str, "days_to_expiry": days}
    except Exception as e:
        logger.warning(f"[Health] whois fallback failed for {clean}: {e}")

    return {"expiry_date": None, "days_to_expiry": None}


# ── WP ping ───────────────────────────────────────────────────────────────────

async def _wp_ping(
    domain: str,
    http_user: str = "",
    http_pass: str = "",
    wp_login: str = "",
    wp_pass: str = "",
) -> bool:
    """Check if WP REST API responds. Tries WP auth first (for htpasswd sites), then unauthenticated."""
    base = domain if domain.startswith("http") else f"https://{domain}"
    base = base.rstrip("/")
    site_auth = httpx.BasicAuth(http_user, http_pass) if http_user and http_pass else None

    # Build WP auth header if credentials available
    wp_headers = {}
    if wp_login and wp_pass:
        try:
            from services.crypto_service import get_plain_password
            plain = get_plain_password(wp_pass)
            wp_creds = base64.b64encode(f"{wp_login}:{plain}".encode()).decode()
            wp_headers = {"Authorization": f"Basic {wp_creds}"}
        except Exception as e:
            logger.debug(f"[Health] WP auth header build failed for {domain}: {e}")

    for url in [f"{base}/wp-json/wp/v2/posts?per_page=1",
                f"{base.replace('https://', 'http://')}/wp-json/wp/v2/posts?per_page=1"]:
        # Try with WP credentials first (handles htpasswd-protected sites)
        if wp_headers:
            try:
                async with httpx.AsyncClient(verify=False, timeout=7, auth=site_auth) as client:
                    resp = await client.get(url, headers=wp_headers)
                    if resp.status_code in (200, 401, 403):
                        return True
            except Exception:
                pass
        # Fall back to unauthenticated check
        try:
            async with httpx.AsyncClient(verify=False, timeout=7, auth=site_auth) as client:
                resp = await client.get(url)
                if resp.status_code in (200, 401, 403):
                    return True
        except Exception:
            continue
    return False


# ── Health score ──────────────────────────────────────────────────────────────

def _health_score(
    traffic: int,
    keywords: int,
    days_to_expiry: Optional[int] = None,
    dr: Optional[int] = None,
    wp_ok: bool = False,
) -> str:
    # Critical: expired or expiring very soon
    if days_to_expiry is not None and days_to_expiry < 7:
        return "critical"
    score = "weak"
    # PBN-friendly thresholds (lower than authority sites)
    if traffic >= 100 or keywords >= 50 or (dr is not None and dr >= 15 and wp_ok):
        score = "good"
    elif traffic >= 10 or keywords >= 10 or (dr is not None and dr >= 5):
        score = "medium"
    # DR boost: DR >= 10 bumps up score by one tier
    if dr is not None and dr >= 10:
        if score == "medium":
            score = "good"
        elif score == "weak":
            score = "medium"
    return score


# ── Per-domain health (with hard timeout) ─────────────────────────────────────

async def _domain_health_safe(row: dict) -> dict:
    """Wraps _domain_health with a hard timeout to prevent hangs."""
    try:
        return await asyncio.wait_for(_domain_health_live(row), timeout=_DOMAIN_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(f"[Health] Timeout for domain {row.get('domain')}")
        return _empty_result(row)
    except Exception as e:
        logger.warning(f"[Health] Error for domain {row.get('domain')}: {e}")
        return _empty_result(row)


def _empty_result(row: dict) -> dict:
    return {
        "id": row["id"],
        "domain": row["domain"],
        "server": row.get("server", ""),
        "active": row.get("active", 1),
        "wp_ok": False,
        "traffic": 0,
        "keywords": 0,
        "expiry_date": None,
        "days_to_expiry": None,
        "expiry_status": "unknown",
        "health_score": "weak",
        "dr": None,
        "error": "timeout",
    }


async def _domain_health_live(row: dict) -> dict:
    domain = row["domain"]
    http_user = row.get("http_user", "") or ""
    http_pass = row.get("http_pass", "") or ""
    wp_login = row.get("wp_login", "") or ""
    wp_pass = row.get("wp_pass", "") or ""

    metrics, whois_data, wp_ok = await asyncio.gather(
        _dfs_domain_metrics(domain),
        _whois_expiry(domain),
        _wp_ping(domain, http_user, http_pass, wp_login, wp_pass),
        return_exceptions=True,
    )
    if isinstance(metrics, Exception): metrics = {}
    if isinstance(whois_data, Exception): whois_data = {}
    if isinstance(wp_ok, Exception): wp_ok = False

    traffic = metrics.get("traffic", 0) or 0
    keywords = metrics.get("keywords", 0) or 0
    dr = metrics.get("dr")
    days = whois_data.get("days_to_expiry")

    return {
        "id": row["id"],
        "domain": domain,
        "server": row.get("server", ""),
        "active": row.get("active", 1),
        "wp_ok": bool(wp_ok),
        "traffic": traffic,
        "keywords": keywords,
        "expiry_date": whois_data.get("expiry_date"),
        "days_to_expiry": days,
        "expiry_status": (
            "critical" if days is not None and days < 14
            else "warning" if days is not None and days < 60
            else "ok" if days is not None
            else "unknown"
        ),
        "health_score": _health_score(traffic, keywords, days, dr, wp_ok=bool(wp_ok)),
        "dr": dr,
    }


# ── Cron: weekly snapshot ─────────────────────────────────────────────────────

async def run_weekly_snapshot():
    """Fetch metrics for all domains and save snapshot. Called by cron or manual trigger."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT md.id, md.domain, md.server, md.active,
                      COALESCE(md.http_user,'') as http_user,
                      COALESCE(md.http_pass,'') as http_pass,
                      COALESCE(md.wp_login,'') as wp_login,
                      COALESCE(md.wp_pass,'') as wp_pass
               FROM my_domains md WHERE md.active=1"""
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    total = len(rows)
    logger.info(f"[HealthCron] Starting snapshot for {total} domains")
    snapped_at = datetime.now(timezone.utc).isoformat()

    # Create progress record
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO domain_health_snapshot_progress (started_at, total, done, finished) VALUES (?,?,0,0)",
            (snapped_at, total)
        )
        progress_id = cur.lastrowid
        await db.commit()

    # Process in batches of 5 (smaller to avoid rate-limiting)
    results = []
    BATCH = 5
    try:
        for i in range(0, total, BATCH):
            batch = rows[i:i + BATCH]
            batch_results = await asyncio.gather(*[_domain_health_safe(r) for r in batch])
            results.extend(batch_results)
            done = min(i + BATCH, total)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE domain_health_snapshot_progress SET done=? WHERE id=?",
                    (done, progress_id)
                )
                await db.commit()
            logger.info(f"[HealthCron] Batch {i//BATCH+1}: {done}/{total} done")
    except Exception as e:
        logger.error(f"[HealthCron] Batch processing failed at {len(results)}/{total}: {e}")

    # Log summary of collected data
    with_traffic = sum(1 for r in results if r.get("traffic", 0) > 0)
    with_kw = sum(1 for r in results if r.get("keywords", 0) > 0)
    logger.info(f"[HealthCron] Collected {len(results)} results: {with_traffic} with traffic, {with_kw} with keywords")

    # Save whatever results we have (even partial)
    saved = 0
    if results:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.executemany(
                    """INSERT INTO domain_health_snapshots
                       (my_domain_id, domain, traffic, keywords, wp_ok, expiry_date, days_to_expiry, health_score, dr, snapped_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    [
                        (r["id"], r["domain"], r.get("traffic", 0), r.get("keywords", 0),
                         1 if r.get("wp_ok") else 0,
                         r.get("expiry_date"), r.get("days_to_expiry"), r.get("health_score", "weak"),
                         r.get("dr"), snapped_at)
                        for r in results
                    ]
                )
                await db.executemany(
                    "UPDATE my_domains SET wp_ok=? WHERE id=?",
                    [(1 if r.get("wp_ok") else 0, r["id"]) for r in results]
                )
                await db.commit()
                saved = len(results)
                logger.info(f"[HealthCron] Saved {saved} snapshots to DB")
        except Exception as e:
            logger.error(f"[HealthCron] FAILED to save snapshots to DB: {e}")

    # Always mark progress as finished (even on error)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE domain_health_snapshot_progress SET done=?, finished=1, finished_at=? WHERE id=?",
            (len(results), datetime.now(timezone.utc).isoformat(), progress_id)
        )
        await db.commit()

    logger.info(f"[HealthCron] Snapshot done: {saved}/{total} saved")
    return saved


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
async def domain_health(limit: int = 50, offset: int = 0):
    """Returns domains with last snapshot data (fast). Live check via /{id}."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, domain, server, active,
                      COALESCE(wp_ok, 0) as domain_wp_ok
               FROM my_domains ORDER BY domain LIMIT ? OFFSET ?""",
            (limit, offset)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        async with db.execute("SELECT COUNT(*) FROM my_domains") as cur:
            total = (await cur.fetchone())[0]

        # Get last snapshot for each domain
        async with db.execute(
            """SELECT my_domain_id, traffic, keywords, wp_ok, expiry_date,
                      days_to_expiry, health_score, dr, snapped_at
               FROM domain_health_snapshots
               WHERE id IN (
                   SELECT MAX(id) FROM domain_health_snapshots GROUP BY my_domain_id
               )"""
        ) as cur:
            snapshots = {r["my_domain_id"]: dict(r) for r in await cur.fetchall()}

        # Get latest progress
        async with db.execute(
            "SELECT total, done, finished FROM domain_health_snapshot_progress ORDER BY id DESC LIMIT 1"
        ) as cur:
            prog_row = await cur.fetchone()
        progress = dict(prog_row) if prog_row else None

    results = []
    for row in rows:
        snap = snapshots.get(row["id"])
        has_snap = snap is not None
        traffic = snap.get("traffic", 0) or 0 if has_snap else 0
        keywords = snap.get("keywords", 0) or 0 if has_snap else 0
        days = snap.get("days_to_expiry") if has_snap else None
        wp_ok = bool(snap.get("wp_ok", False)) if has_snap else bool(row.get("domain_wp_ok"))
        results.append({
            "id": row["id"],
            "domain": row["domain"],
            "server": row.get("server", ""),
            "active": row.get("active", 1),
            "wp_ok": wp_ok,
            "traffic": traffic,
            "keywords": keywords,
            "expiry_date": snap.get("expiry_date") if has_snap else None,
            "days_to_expiry": days,
            "expiry_status": (
                "critical" if days is not None and days < 14
                else "warning" if days is not None and days < 60
                else "ok" if days is not None
                else "unknown"
            ),
            "health_score": snap.get("health_score", "weak") if has_snap else "weak",
            "dr": snap.get("dr") if has_snap else None,
            "from_snapshot": has_snap,
            "snapped_at": snap.get("snapped_at") if has_snap else None,
        })

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "domains": results,
        "snapshot_progress": progress,
    }


@router.get("/snapshot-progress")
async def snapshot_progress():
    """Returns current snapshot progress (for polling)."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM domain_health_snapshot_progress ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return {"running": False, "total": 0, "done": 0, "pct": 0}
    r = dict(row)
    pct = round(r["done"] / r["total"] * 100) if r["total"] else 0

    # Safety net: auto-finish stuck snapshots
    # If done==total but finished==0 and started >5 min ago, mark as finished
    is_running = r["finished"] == 0
    if is_running and r["done"] >= r["total"] and r["total"] > 0:
        try:
            started = datetime.fromisoformat(r["started_at"].replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            if elapsed > 300:  # >5 min since start and done==total → stuck
                logger.warning(f"[Health] Auto-finishing stuck snapshot progress id={r['id']} (done={r['done']}/{r['total']}, elapsed={elapsed:.0f}s)")
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE domain_health_snapshot_progress SET finished=1, finished_at=? WHERE id=?",
                        (datetime.now(timezone.utc).isoformat(), r["id"])
                    )
                    await db.commit()
                is_running = False
        except Exception:
            pass

    return {
        "running": is_running,
        "total": r["total"],
        "done": r["done"],
        "pct": pct,
        "started_at": r["started_at"],
        "finished_at": r.get("finished_at"),
    }


@router.get("/summary")
async def health_summary():
    """Returns aggregate stats from latest snapshots."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT health_score, COUNT(*) as cnt
               FROM domain_health_snapshots
               WHERE id IN (SELECT MAX(id) FROM domain_health_snapshots GROUP BY my_domain_id)
               GROUP BY health_score"""
        ) as cur:
            rows = await cur.fetchall()
        async with db.execute(
            """SELECT COUNT(*) FROM domain_health_snapshots
               WHERE id IN (SELECT MAX(id) FROM domain_health_snapshots GROUP BY my_domain_id)
               AND wp_ok=1"""
        ) as cur:
            wp_ok_count = (await cur.fetchone())[0]
        async with db.execute(
            """SELECT COUNT(*) FROM domain_health_snapshots
               WHERE id IN (SELECT MAX(id) FROM domain_health_snapshots GROUP BY my_domain_id)
               AND days_to_expiry IS NOT NULL AND days_to_expiry < 60"""
        ) as cur:
            expiring_count = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM my_domains WHERE active=1") as cur:
            total_active = (await cur.fetchone())[0]

    score_map = {r["health_score"]: r["cnt"] for r in rows}
    return {
        "total_active": total_active,
        "good": score_map.get("good", 0),
        "medium": score_map.get("medium", 0),
        "weak": score_map.get("weak", 0),
        "critical": score_map.get("critical", 0),
        "wp_ok": wp_ok_count,
        "expiring_soon": expiring_count,
    }


@router.get("/snapshots")
async def list_snapshots(domain: Optional[str] = None, limit: int = 200):
    """Returns weekly snapshot history. Optionally filtered by domain."""
    await ensure_tables()
    if domain:
        clean = re.sub(r"^https?://", "", domain).rstrip("/")
        query = """SELECT * FROM domain_health_snapshots
                   WHERE domain LIKE ? ORDER BY snapped_at DESC LIMIT ?"""
        params = (f"%{clean}%", limit)
    else:
        query = """SELECT * FROM domain_health_snapshots ORDER BY snapped_at DESC LIMIT ?"""
        params = (limit,)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return rows


@router.post("/snapshot")
async def trigger_snapshot(background_tasks: BackgroundTasks):
    """Manually trigger a health snapshot for all active domains."""
    # Check if snapshot already running
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT finished FROM domain_health_snapshot_progress ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    if row and row["finished"] == 0:
        return {"message": "Snapshot już trwa w tle", "already_running": True}

    background_tasks.add_task(run_weekly_snapshot)
    return {"message": "Snapshot uruchomiony w tle", "already_running": False}


async def run_quick_snapshot():
    """Fast snapshot: DataForSEO + WP only (skip WHOIS). Runs in ~2-3 min for 100 domains."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT md.id, md.domain, md.server, md.active,
                      COALESCE(md.http_user,'') as http_user,
                      COALESCE(md.http_pass,'') as http_pass,
                      COALESCE(md.wp_login,'') as wp_login,
                      COALESCE(md.wp_pass,'') as wp_pass
               FROM my_domains md WHERE md.active=1"""
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    total = len(rows)
    logger.info(f"[HealthQuick] Starting quick snapshot for {total} domains")
    snapped_at = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO domain_health_snapshot_progress (started_at, total, done, finished) VALUES (?,?,0,0)",
            (snapped_at, total)
        )
        progress_id = cur.lastrowid
        await db.commit()

    async def _quick_check(row: dict) -> dict:
        domain = row["domain"]
        http_user = row.get("http_user", "") or ""
        http_pass = row.get("http_pass", "") or ""
        wp_login = row.get("wp_login", "") or ""
        wp_pass = row.get("wp_pass", "") or ""
        try:
            metrics, wp_ok = await asyncio.gather(
                _dfs_domain_metrics(domain),
                _wp_ping(domain, http_user, http_pass, wp_login, wp_pass),
                return_exceptions=True,
            )
            if isinstance(metrics, Exception): metrics = {}
            if isinstance(wp_ok, Exception): wp_ok = False
            traffic = metrics.get("traffic", 0) or 0
            keywords = metrics.get("keywords", 0) or 0
            dr = metrics.get("dr")
            return {"id": row["id"], "domain": domain, "traffic": traffic,
                    "keywords": keywords, "wp_ok": bool(wp_ok), "dr": dr,
                    "health_score": _health_score(traffic, keywords, dr=dr, wp_ok=bool(wp_ok))}
        except Exception as e:
            logger.warning(f"[HealthQuick] _quick_check failed for {row.get('domain')}: {e}")
            return {"id": row["id"], "domain": domain, "traffic": 0,
                    "keywords": 0, "wp_ok": False, "dr": None, "health_score": "weak"}

    results = []
    BATCH = 8
    try:
        for i in range(0, total, BATCH):
            batch = rows[i:i + BATCH]
            batch_results = await asyncio.gather(*[_quick_check(r) for r in batch])
            results.extend(batch_results)
            done = min(i + BATCH, total)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE domain_health_snapshot_progress SET done=? WHERE id=?",
                    (done, progress_id)
                )
                await db.commit()
            logger.info(f"[HealthQuick] Batch {i//BATCH+1}: {done}/{total} done")
    except Exception as e:
        logger.error(f"[HealthQuick] Batch processing failed at {len(results)}/{total}: {e}")

    # Log summary
    with_traffic = sum(1 for r in results if r.get("traffic", 0) > 0)
    with_kw = sum(1 for r in results if r.get("keywords", 0) > 0)
    logger.info(f"[HealthQuick] Collected {len(results)} results: {with_traffic} with traffic, {with_kw} with keywords")

    # Save whatever results we have
    saved = 0
    if results:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Merge with existing expiry data from last snapshot
                existing = {}
                try:
                    async with db.execute(
                        """SELECT my_domain_id, expiry_date, days_to_expiry, dr
                           FROM domain_health_snapshots
                           WHERE id IN (SELECT MAX(id) FROM domain_health_snapshots GROUP BY my_domain_id)"""
                    ) as cur:
                        existing = {r["my_domain_id"]: dict(r) for r in await cur.fetchall()}
                except Exception:
                    pass

                await db.executemany(
                    """INSERT INTO domain_health_snapshots
                       (my_domain_id, domain, traffic, keywords, wp_ok, expiry_date, days_to_expiry, health_score, dr, snapped_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    [
                        (r["id"], r["domain"], r.get("traffic", 0), r.get("keywords", 0),
                         1 if r.get("wp_ok") else 0,
                         existing.get(r["id"], {}).get("expiry_date"),
                         existing.get(r["id"], {}).get("days_to_expiry"),
                         _health_score(r.get("traffic", 0), r.get("keywords", 0),
                                       existing.get(r["id"], {}).get("days_to_expiry"),
                                       r.get("dr") or existing.get(r["id"], {}).get("dr"),
                                       wp_ok=bool(r.get("wp_ok"))),
                         r.get("dr") or existing.get(r["id"], {}).get("dr"),
                         snapped_at)
                        for r in results
                    ]
                )
                await db.executemany(
                    "UPDATE my_domains SET wp_ok=? WHERE id=?",
                    [(1 if r.get("wp_ok") else 0, r["id"]) for r in results]
                )
                await db.commit()
                saved = len(results)
                logger.info(f"[HealthQuick] Saved {saved} snapshots to DB")
        except Exception as e:
            logger.error(f"[HealthQuick] FAILED to save snapshots to DB: {e}")

    # Always mark as finished
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE domain_health_snapshot_progress SET done=?, finished=1, finished_at=? WHERE id=?",
            (len(results), datetime.now(timezone.utc).isoformat(), progress_id)
        )
        await db.commit()

    logger.info(f"[HealthQuick] Quick snapshot done: {saved}/{total} saved")
    return saved


@router.post("/snapshot-quick")
async def trigger_quick_snapshot(background_tasks: BackgroundTasks):
    """Fast snapshot: DataForSEO + WP only (no WHOIS). ~2-3 min for 100 domains."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT finished FROM domain_health_snapshot_progress ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    if row and row["finished"] == 0:
        return {"message": "Snapshot już trwa w tle", "already_running": True}

    background_tasks.add_task(run_quick_snapshot)
    return {"message": "Szybki snapshot uruchomiony (DataForSEO + WP)", "already_running": False}


@router.post("/snapshot-reset")
async def reset_snapshot_progress():
    """Force-finish any stuck snapshot progress so a new one can start."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE domain_health_snapshot_progress SET finished=1, finished_at=? WHERE finished=0",
            (datetime.now(timezone.utc).isoformat(),)
        )
        await db.commit()
    return {"ok": True, "message": "Stuck snapshot progress reset"}


@router.get("/export-csv")
async def export_health_csv():
    """Export latest domain health snapshot as CSV."""
    import csv, io
    from fastapi.responses import StreamingResponse
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT md.domain, md.server,
                      COALESCE(s.traffic, 0) as traffic,
                      COALESCE(s.keywords, 0) as keywords,
                      COALESCE(s.wp_ok, md.wp_ok, 0) as wp_ok,
                      s.expiry_date,
                      s.days_to_expiry,
                      COALESCE(s.health_score, 'weak') as health_score,
                      s.snapped_at
               FROM my_domains md
               LEFT JOIN domain_health_snapshots s ON s.id = (
                   SELECT MAX(id) FROM domain_health_snapshots WHERE my_domain_id = md.id
               )
               ORDER BY md.domain"""
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "domain", "server", "traffic", "keywords", "wp_ok",
        "expiry_date", "days_to_expiry", "health_score", "snapped_at"
    ])
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)
    filename = f"domain_health_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{domain_id}")
async def single_domain_health(domain_id: int):
    """Live check for a single domain (bypasses cache)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, domain, server, active,
                      COALESCE(http_user,'') as http_user,
                      COALESCE(http_pass,'') as http_pass,
                      COALESCE(wp_login,'') as wp_login,
                      COALESCE(wp_pass,'') as wp_pass
               FROM my_domains WHERE id = ?""",
            (domain_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        from fastapi import HTTPException
        raise HTTPException(404, "Domain not found")
    return await _domain_health_safe(dict(row))


@router.post("/{domain_id}/test-wp")
async def test_wp_connection(domain_id: int):
    """Test WordPress REST API connection for a single domain. Returns detailed error info."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, domain, wp_login, wp_pass,
                      COALESCE(http_user,'') as http_user,
                      COALESCE(http_pass,'') as http_pass
               FROM my_domains WHERE id = ?""",
            (domain_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        from fastapi import HTTPException
        raise HTTPException(404, "Domain not found")
    d = dict(row)
    domain = d["domain"].rstrip("/")
    if not domain.startswith("http"):
        domain = "https://" + domain
    from services.crypto_service import get_plain_password
    plain_pass = get_plain_password(d['wp_pass'])
    wp_creds = base64.b64encode(f"{d['wp_login']}:{plain_pass}".encode()).decode()
    headers = {"Authorization": f"Basic {wp_creds}"}
    if d.get("http_user"):
        # httpx handles http auth via auth param
        pass
    try:
        auth = None
        if d.get("http_user"):
            auth = httpx.BasicAuth(d["http_user"], d["http_pass"])
        async with httpx.AsyncClient(timeout=15, verify=False, auth=auth) as client:
            # Test 1: REST API reachable
            resp = await client.get(f"{domain}/wp-json/wp/v2/posts?per_page=1&_fields=id,title", headers=headers)
            if resp.status_code == 200:
                posts = resp.json()
                return {
                    "ok": True,
                    "domain": d["domain"],
                    "status_code": 200,
                    "posts_found": len(posts),
                    "message": f"WP REST API dziala poprawnie. Znaleziono {len(posts)} post(ow).",
                }
            elif resp.status_code == 401:
                return {"ok": False, "domain": d["domain"], "status_code": 401, "message": "Bledne dane logowania WP (401 Unauthorized)"}
            elif resp.status_code == 403:
                return {"ok": False, "domain": d["domain"], "status_code": 403, "message": "Brak dostepu do REST API (403 Forbidden). Sprawdz htpasswd lub wtyczke bezpieczenstwa."}
            else:
                return {"ok": False, "domain": d["domain"], "status_code": resp.status_code, "message": f"WP REST API zwrocilo {resp.status_code}: {resp.text[:200]}"}
    except httpx.ConnectTimeout:
        return {"ok": False, "domain": d["domain"], "status_code": 0, "message": "Timeout polaczenia (domena nie odpowiada)"}
    except httpx.ConnectError as e:
        return {"ok": False, "domain": d["domain"], "status_code": 0, "message": f"Blad polaczenia: {str(e)[:200]}"}
    except Exception as e:
        return {"ok": False, "domain": d["domain"], "status_code": 0, "message": f"Blad: {str(e)[:200]}"}


@router.get("/diagnostics")
async def health_diagnostics():
    """Diagnostic endpoint: check DFS credentials, test one domain, show last snapshot stats."""
    await ensure_tables()
    diag = {
        "dataforseo_configured": bool(DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD),
        "whoisxml_configured": bool(WHOISXML_KEY),
    }

    # Count domains
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM my_domains WHERE active=1") as cur:
            diag["active_domains"] = (await cur.fetchone())[0]
        # Last snapshot stats
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN traffic > 0 THEN 1 ELSE 0 END) as with_traffic,
                      SUM(CASE WHEN keywords > 0 THEN 1 ELSE 0 END) as with_keywords,
                      SUM(CASE WHEN wp_ok = 1 THEN 1 ELSE 0 END) as wp_ok_count,
                      MAX(snapped_at) as last_snap
               FROM domain_health_snapshots
               WHERE id IN (SELECT MAX(id) FROM domain_health_snapshots GROUP BY my_domain_id)"""
        ) as cur:
            snap = await cur.fetchone()
            diag["last_snapshot"] = dict(snap) if snap else None
        # Snapshot progress
        async with db.execute(
            "SELECT * FROM domain_health_snapshot_progress ORDER BY id DESC LIMIT 1"
        ) as cur:
            prog = await cur.fetchone()
            diag["last_progress"] = dict(prog) if prog else None

    # Test DFS with first active domain
    if diag["dataforseo_configured"]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT domain FROM my_domains WHERE active=1 LIMIT 1") as cur:
                row = await cur.fetchone()
        if row:
            test_domain = row[0]
            diag["dfs_test_domain"] = test_domain
            try:
                result = await _dfs_domain_metrics(test_domain)
                diag["dfs_test_result"] = result or {"note": "No data returned (domain may have no organic visibility)"}
            except Exception as e:
                diag["dfs_test_error"] = str(e)
    else:
        diag["dfs_test_note"] = "Skipped — DATAFORSEO_LOGIN or DATAFORSEO_PASSWORD not set"

    return diag

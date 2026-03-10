"""
Domain Health monitoring endpoint.
For each domain fetches in parallel:
- DataForSEO: organic traffic + keywords count (no DR — DFS doesn't have Ahrefs-style DR)
- WHOIS: expiry date
- WP ping: checks if WP REST API responds

Weekly cron saves snapshots to domain_health_snapshots table.
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

from config import DB_PATH, DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD

router = APIRouter(prefix="/api/health", tags=["health"])
logger = logging.getLogger(__name__)

WHOISXML_KEY = os.getenv("WHOISXML_API_KEY", "")

# Simple in-memory cache: domain_id -> (timestamp, result)
_health_cache: dict = {}
_CACHE_TTL = 300  # 5 minutes

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
    snapped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_dhs_domain ON domain_health_snapshots(my_domain_id, snapped_at);
"""


async def ensure_tables():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_TABLES)


# ── DataForSEO helpers ────────────────────────────────────────────────────────

async def _dfs_domain_metrics(domain: str) -> dict:
    """Fetch organic traffic + keyword count from DataForSEO."""
    if not DATAFORSEO_LOGIN or not DATAFORSEO_PASSWORD:
        return {}
    creds = base64.b64encode(f"{DATAFORSEO_LOGIN}:{DATAFORSEO_PASSWORD}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}
    clean = re.sub(r"^https?://", "", domain).rstrip("/")
    payload = [{"target": clean, "location_code": 2616, "language_code": "pl"}]
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.dataforseo.com/v3/dataforseo_labs/google/domain_rank_overview/live",
                json=payload,
                headers=headers,
            )
            if resp.status_code != 200:
                return {}
            data = resp.json()
        for task in data.get("tasks", []):
            for result in task.get("result", []):
                metrics = result.get("metrics", {}).get("organic", {})
                return {
                    "traffic": int(metrics.get("etv", 0) or 0),
                    "keywords": int(metrics.get("count", 0) or 0),
                }
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
            async with httpx.AsyncClient(timeout=15) as client:
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
        w = await loop.run_in_executor(None, whois.whois, clean)
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

async def _wp_ping(domain: str, http_user: str = "", http_pass: str = "") -> bool:
    base = domain if domain.startswith("http") else f"https://{domain}"
    base = base.rstrip("/")
    site_auth = (http_user, http_pass) if http_user and http_pass else None
    for url in [f"{base}/wp-json/wp/v2/posts?per_page=1",
                f"{base.replace('https://', 'http://')}/wp-json/wp/v2/posts?per_page=1"]:
        try:
            async with httpx.AsyncClient(verify=False, timeout=8, auth=site_auth) as client:
                resp = await client.get(url)
                if resp.status_code in (200, 401, 403):
                    return True
        except Exception:
            continue
    return False


# ── Per-domain health ─────────────────────────────────────────────────────────

def _health_score(traffic: int, keywords: int) -> str:
    if traffic >= 500 or keywords >= 200:
        return "good"
    if traffic >= 50 or keywords >= 30:
        return "medium"
    return "weak"


async def _domain_health(row: dict) -> dict:
    domain_id = row["id"]
    now = datetime.utcnow().timestamp()

    # Return cached result if fresh
    if domain_id in _health_cache:
        cached_ts, cached_result = _health_cache[domain_id]
        if now - cached_ts < _CACHE_TTL:
            return cached_result

    domain = row["domain"]
    metrics, whois, wp_ok = await asyncio.gather(
        _dfs_domain_metrics(domain),
        _whois_expiry(domain),
        _wp_ping(domain),
        return_exceptions=True,
    )
    if isinstance(metrics, Exception): metrics = {}
    if isinstance(whois, Exception): whois = {}
    if isinstance(wp_ok, Exception): wp_ok = False

    traffic = metrics.get("traffic", 0) or 0
    keywords = metrics.get("keywords", 0) or 0
    days = whois.get("days_to_expiry")

    result = {
        "id": domain_id,
        "domain": domain,
        "server": row.get("server", ""),
        "active": row.get("active", 1),
        "wp_ok": bool(wp_ok),
        "traffic": traffic,
        "keywords": keywords,
        "expiry_date": whois.get("expiry_date"),
        "days_to_expiry": days,
        "expiry_status": "critical" if days is not None and days < 14 else "warning" if days is not None and days < 60 else "ok" if days is not None else "unknown",
        "health_score": _health_score(traffic, keywords),
    }
    _health_cache[domain_id] = (now, result)
    return result


# ── Cron: weekly snapshot ─────────────────────────────────────────────────────

async def run_weekly_snapshot():
    """Fetch metrics for all domains and save snapshot. Called by cron."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, domain, server, active FROM my_domains WHERE active=1") as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    logger.info(f"[HealthCron] Starting weekly snapshot for {len(rows)} domains")
    snapped_at = datetime.utcnow().isoformat()

    # Process in batches of 5
    results = []
    for i in range(0, len(rows), 5):
        batch = rows[i:i + 5]
        batch_results = await asyncio.gather(*[_domain_health(r) for r in batch])
        results.extend(batch_results)

    async with aiosqlite.connect(DB_PATH) as db:
        for r in results:
            await db.execute(
                """INSERT INTO domain_health_snapshots
                   (my_domain_id, domain, traffic, keywords, wp_ok, expiry_date, days_to_expiry, health_score, snapped_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (r["id"], r["domain"], r["traffic"], r["keywords"],
                 1 if r["wp_ok"] else 0,
                 r["expiry_date"], r["days_to_expiry"], r["health_score"], snapped_at)
            )
        await db.commit()

    logger.info(f"[HealthCron] Snapshot saved for {len(results)} domains")
    return len(results)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.on_event("startup")
async def startup():
    await ensure_tables()


@router.get("")
async def domain_health(limit: int = 50, offset: int = 0):
    """Returns domains with last snapshot data (fast). Live check via /{id}."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, domain, server, active FROM my_domains ORDER BY domain LIMIT ? OFFSET ?",
            (limit, offset)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        async with db.execute("SELECT COUNT(*) FROM my_domains") as cur:
            total = (await cur.fetchone())[0]

        # Get last snapshot for each domain
        async with db.execute(
            """SELECT my_domain_id, traffic, keywords, wp_ok, expiry_date, days_to_expiry, health_score
               FROM domain_health_snapshots
               WHERE id IN (
                   SELECT MAX(id) FROM domain_health_snapshots GROUP BY my_domain_id
               )"""
        ) as cur:
            snapshots = {r["my_domain_id"]: dict(r) for r in await cur.fetchall()}

    results = []
    for row in rows:
        snap = snapshots.get(row["id"], {})
        traffic = snap.get("traffic", 0) or 0
        keywords = snap.get("keywords", 0) or 0
        days = snap.get("days_to_expiry")
        results.append({
            "id": row["id"],
            "domain": row["domain"],
            "server": row.get("server", ""),
            "active": row.get("active", 1),
            "wp_ok": bool(snap.get("wp_ok", False)),
            "traffic": traffic,
            "keywords": keywords,
            "expiry_date": snap.get("expiry_date"),
            "days_to_expiry": days,
            "expiry_status": "critical" if days is not None and days < 14 else "warning" if days is not None and days < 60 else "ok" if days is not None else "unknown",
            "health_score": snap.get("health_score", "weak"),
            "from_snapshot": True,
        })

    return {"total": total, "offset": offset, "limit": limit, "domains": results}


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
    background_tasks.add_task(run_weekly_snapshot)
    return {"message": "Snapshot started in background"}


@router.get("/{domain_id}")
async def single_domain_health(domain_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, domain, server, active FROM my_domains WHERE id = ?", (domain_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        from fastapi import HTTPException
        raise HTTPException(404, "Domain not found")
    return await _domain_health(dict(row))

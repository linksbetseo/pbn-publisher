"""
Domain Health monitoring endpoint.
For each domain fetches in parallel:
- DataForSEO: DR, organic traffic, keywords count
- WHOIS: expiry date (via whoisxmlapi or python-whois fallback)
- WP ping: checks if WP REST API responds
"""
import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import httpx
from fastapi import APIRouter

from config import DB_PATH, DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD

router = APIRouter(prefix="/api/health", tags=["health"])
logger = logging.getLogger(__name__)

WHOISXML_KEY = os.getenv("WHOISXML_API_KEY", "")


# ── DataForSEO helpers ────────────────────────────────────────────────────────

async def _dfs_domain_metrics(domain: str) -> dict:
    """Fetch DR + organic traffic + keyword count from DataForSEO."""
    if not DATAFORSEO_LOGIN or not DATAFORSEO_PASSWORD:
        return {}
    import base64
    creds = base64.b64encode(f"{DATAFORSEO_LOGIN}:{DATAFORSEO_PASSWORD}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}
    # Strip protocol
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
                    "dr": result.get("domain_rank", 0),
                    "traffic": metrics.get("etv", 0) or 0,
                    "keywords": metrics.get("count", 0) or 0,
                }
    except Exception as e:
        logger.warning(f"[Health] DFS metrics failed for {domain}: {e}")
    return {}


# ── WHOIS helpers ─────────────────────────────────────────────────────────────

def _days_until(expiry_str: str) -> Optional[int]:
    """Parse expiry date string and return days remaining."""
    if not expiry_str:
        return None
    formats = [
        "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
        "%d-%b-%Y", "%Y%m%d", "%Y-%m-%dT%H:%M:%S.%fZ",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(expiry_str[:19], fmt[:len(expiry_str[:19])])
            delta = dt.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
            return delta.days
        except Exception:
            continue
    return None


async def _whois_expiry(domain: str) -> dict:
    """Get domain expiry via WhoisXML API or python-whois fallback."""
    clean = re.sub(r"^https?://", "", domain).rstrip("/").split("/")[0]

    # Try WhoisXML API if key available
    if WHOISXML_KEY:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://www.whoisxmlapi.com/whoisserver/WhoisService",
                    params={
                        "apiKey": WHOISXML_KEY,
                        "domainName": clean,
                        "outputFormat": "JSON",
                    }
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

    # Fallback: python-whois (sync, run in thread)
    try:
        import whois
        loop = asyncio.get_event_loop()
        w = await loop.run_in_executor(None, whois.whois, clean)
        expiry = w.expiration_date
        if isinstance(expiry, list):
            expiry = expiry[0]
        if expiry:
            expiry_str = expiry.strftime("%Y-%m-%d") if hasattr(expiry, "strftime") else str(expiry)[:10]
            delta = expiry.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc) if hasattr(expiry, "tzinfo") else None
            days = delta.days if delta else None
            return {"expiry_date": expiry_str, "days_to_expiry": days}
    except Exception as e:
        logger.warning(f"[Health] whois fallback failed for {clean}: {e}")

    return {"expiry_date": None, "days_to_expiry": None}


# ── WP ping ───────────────────────────────────────────────────────────────────

async def _wp_ping(domain: str) -> bool:
    """Check if WordPress REST API is reachable."""
    base = domain if domain.startswith("http") else f"https://{domain}"
    base = base.rstrip("/")
    try:
        async with httpx.AsyncClient(verify=False, timeout=8) as client:
            resp = await client.get(f"{base}/wp-json/wp/v2/posts?per_page=1")
            return resp.status_code in (200, 401, 403)
    except Exception:
        try:
            base_http = base.replace("https://", "http://")
            async with httpx.AsyncClient(verify=False, timeout=8) as client:
                resp = await client.get(f"{base_http}/wp-json/wp/v2/posts?per_page=1")
                return resp.status_code in (200, 401, 403)
        except Exception:
            return False


# ── Per-domain health fetch ───────────────────────────────────────────────────

async def _domain_health(row: dict) -> dict:
    domain = row["domain"]

    # Run all checks in parallel
    metrics_task = asyncio.create_task(_dfs_domain_metrics(domain))
    whois_task = asyncio.create_task(_whois_expiry(domain))
    wp_task = asyncio.create_task(_wp_ping(domain))

    metrics, whois, wp_ok = await asyncio.gather(
        metrics_task, whois_task, wp_task, return_exceptions=True
    )

    if isinstance(metrics, Exception):
        metrics = {}
    if isinstance(whois, Exception):
        whois = {}
    if isinstance(wp_ok, Exception):
        wp_ok = False

    days = whois.get("days_to_expiry")
    if days is None:
        expiry_status = "unknown"
    elif days < 14:
        expiry_status = "critical"
    elif days < 60:
        expiry_status = "warning"
    else:
        expiry_status = "ok"

    dr = metrics.get("dr", 0) or 0
    traffic = metrics.get("traffic", 0) or 0
    keywords = metrics.get("keywords", 0) or 0

    if dr >= 20 and traffic >= 100:
        health_score = "good"
    elif dr >= 10 or traffic >= 20:
        health_score = "medium"
    else:
        health_score = "weak"

    return {
        "id": row["id"],
        "domain": domain,
        "server": row.get("server", ""),
        "active": row.get("active", 1),
        "wp_ok": bool(wp_ok),
        "dr": dr,
        "traffic": int(traffic),
        "keywords": int(keywords),
        "expiry_date": whois.get("expiry_date"),
        "days_to_expiry": days,
        "expiry_status": expiry_status,
        "health_score": health_score,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
async def domain_health(limit: int = 50, offset: int = 0):
    """
    Returns health metrics for all domains.
    Fetches in parallel batches of 5 to avoid rate limits.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, domain, server, active FROM my_domains ORDER BY domain LIMIT ? OFFSET ?",
            (limit, offset)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        async with db.execute("SELECT COUNT(*) FROM my_domains") as cur:
            total = (await cur.fetchone())[0]

    # Fetch in parallel batches of 5
    results = []
    batch_size = 5
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        batch_results = await asyncio.gather(*[_domain_health(r) for r in batch])
        results.extend(batch_results)

    return {"total": total, "offset": offset, "limit": limit, "domains": results}


@router.get("/{domain_id}")
async def single_domain_health(domain_id: int):
    """Returns health metrics for a single domain."""
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

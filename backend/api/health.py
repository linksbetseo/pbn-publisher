"""
Domain Health monitoring endpoint.
For each domain fetches in parallel:
- DataForSEO: organic traffic + keywords count
- WHOIS: expiry date (WhoisXML preferred, python-whois fallback)
- WP ping: checks if WP REST API responds
- Ahrefs DR: via public endpoint (no API key needed)

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


async def ensure_tables():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_TABLES)
        # Migration: add dr column to snapshots if missing
        try:
            await db.execute("ALTER TABLE domain_health_snapshots ADD COLUMN dr INTEGER DEFAULT NULL")
        except Exception:
            pass
        await db.commit()


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
        async with httpx.AsyncClient(timeout=18) as client:
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

async def _wp_ping(domain: str, http_user: str = "", http_pass: str = "") -> bool:
    base = domain if domain.startswith("http") else f"https://{domain}"
    base = base.rstrip("/")
    site_auth = (http_user, http_pass) if http_user and http_pass else None
    for url in [f"{base}/wp-json/wp/v2/posts?per_page=1",
                f"{base.replace('https://', 'http://')}/wp-json/wp/v2/posts?per_page=1"]:
        try:
            async with httpx.AsyncClient(verify=False, timeout=7, auth=site_auth) as client:
                resp = await client.get(url)
                if resp.status_code in (200, 401, 403):
                    return True
        except Exception:
            continue
    return False


# ── Health score ──────────────────────────────────────────────────────────────

def _health_score(traffic: int, keywords: int, days_to_expiry: Optional[int] = None) -> str:
    # Critical: expired or expiring very soon
    if days_to_expiry is not None and days_to_expiry < 7:
        return "critical"
    if traffic >= 500 or keywords >= 200:
        return "good"
    if traffic >= 50 or keywords >= 30:
        return "medium"
    return "weak"


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
        "error": "timeout",
    }


async def _domain_health_live(row: dict) -> dict:
    domain = row["domain"]
    http_user = row.get("http_user", "") or ""
    http_pass = row.get("http_pass", "") or ""

    metrics, whois_data, wp_ok = await asyncio.gather(
        _dfs_domain_metrics(domain),
        _whois_expiry(domain),
        _wp_ping(domain, http_user, http_pass),
        return_exceptions=True,
    )
    if isinstance(metrics, Exception): metrics = {}
    if isinstance(whois_data, Exception): whois_data = {}
    if isinstance(wp_ok, Exception): wp_ok = False

    traffic = metrics.get("traffic", 0) or 0
    keywords = metrics.get("keywords", 0) or 0
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
        "health_score": _health_score(traffic, keywords, days),
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
                      COALESCE(md.http_pass,'') as http_pass
               FROM my_domains md WHERE md.active=1"""
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    total = len(rows)
    logger.info(f"[HealthCron] Starting snapshot for {total} domains")
    snapped_at = datetime.utcnow().isoformat()

    # Create progress record
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO domain_health_snapshot_progress (started_at, total, done, finished) VALUES (?,?,0,0)",
            (snapped_at, total)
        )
        progress_id = cur.lastrowid
        await db.commit()

    # Process in batches of 10 (parallel per batch)
    results = []
    BATCH = 10
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

    # Save all results
    async with aiosqlite.connect(DB_PATH) as db:
        for r in results:
            await db.execute(
                """INSERT INTO domain_health_snapshots
                   (my_domain_id, domain, traffic, keywords, wp_ok, expiry_date, days_to_expiry, health_score, snapped_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (r["id"], r["domain"], r["traffic"], r["keywords"],
                 1 if r.get("wp_ok") else 0,
                 r.get("expiry_date"), r.get("days_to_expiry"), r.get("health_score", "weak"), snapped_at)
            )
        await db.execute(
            "UPDATE domain_health_snapshot_progress SET done=?, finished=1, finished_at=? WHERE id=?",
            (total, datetime.utcnow().isoformat(), progress_id)
        )
        await db.commit()

    # Also update my_domains.wp_ok for quick access
    async with aiosqlite.connect(DB_PATH) as db:
        for r in results:
            await db.execute(
                "UPDATE my_domains SET wp_ok=? WHERE id=?",
                (1 if r.get("wp_ok") else 0, r["id"])
            )
        await db.commit()

    logger.info(f"[HealthCron] Snapshot done: {len(results)} domains")
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
                      days_to_expiry, health_score, snapped_at
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
    return {
        "running": r["finished"] == 0,
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
    filename = f"domain_health_{datetime.utcnow().strftime('%Y%m%d')}.csv"
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
                      COALESCE(http_pass,'') as http_pass
               FROM my_domains WHERE id = ?""",
            (domain_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        from fastapi import HTTPException
        raise HTTPException(404, "Domain not found")
    return await _domain_health_safe(dict(row))

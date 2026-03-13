import csv
import logging
import os
import secrets
from datetime import datetime, timezone
import aiosqlite
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from config import DB_PATH, CSV_PATH
from services.crypto_service import encrypt_password, is_encrypted, get_plain_password
from api import projects, clients, domains, publish, history, topical_map, content_writer, autopilot, health, dashboard, analytics, deindex, notifications

logger = logging.getLogger(__name__)

APP_USER = os.getenv("APP_USER", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
CRON_SECRET = os.getenv("CRON_SECRET", "")

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS client_domains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
    domain TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS my_domains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    wp_login TEXT NOT NULL,
    wp_pass TEXT NOT NULL,
    server TEXT NOT NULL,
    active INTEGER DEFAULT 1,
    wp_ok INTEGER DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER REFERENCES clients(id),
    client_domain TEXT NOT NULL,
    my_domain_id INTEGER REFERENCES my_domains(id),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    image_prompt TEXT,
    image_url TEXT,
    wp_post_url TEXT,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


async def import_csv_domains(db: aiosqlite.Connection):
    """Import domains from CSV if my_domains table is empty."""
    async with db.execute("SELECT COUNT(*) FROM my_domains") as cursor:
        row = await cursor.fetchone()
        if row[0] > 0:
            return

    csv_path = CSV_PATH
    if not os.path.exists(csv_path):
        logger.info(f"CSV not found at {csv_path}, skipping import.")
        return

    rows_inserted = 0
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=",")
            for row in reader:
                server = (row.get("Serwer") or "").strip()
                domain = (row.get("Domena") or "").strip()
                wp_login = (row.get("Login WP") or "").strip()
                wp_pass = (row.get("Haslo Aplikacji") or "").strip()
                if not domain:
                    continue
                encrypted_pass = encrypt_password(wp_pass)
                await db.execute(
                    "INSERT INTO my_domains (domain, wp_login, wp_pass, server, active) VALUES (?, ?, ?, ?, 1)",
                    (domain, wp_login, encrypted_pass, server),
                )
                rows_inserted += 1
        await db.commit()
        logger.info(f"Imported {rows_inserted} domains from CSV.")
    except Exception as e:
        logger.error(f"CSV import error: {e}")


async def _weekly_cron():
    """Run weekly domain health snapshot every Monday at 03:00 UTC."""
    import asyncio as _asyncio
    from datetime import timedelta
    while True:
        now = datetime.now(timezone.utc)
        # Next Monday 03:00 UTC
        days_ahead = (7 - now.weekday()) % 7 or 7
        next_run = (now + timedelta(days=days_ahead)).replace(hour=3, minute=0, second=0, microsecond=0)
        wait_sec = max(0, (next_run - now).total_seconds())
        await _asyncio.sleep(wait_sec)
        try:
            from api.health import run_weekly_snapshot
            count = await run_weekly_snapshot()
            logger.info(f"[WeeklyCron] Health snapshot done: {count} domains")
        except Exception as e:
            logger.error(f"[WeeklyCron] Error: {e}", exc_info=True)


async def _daily_autopilot_cron():
    """Run autopilot for all active schedules daily at a random time between 06:00-11:45 UTC.

    Anti-footprint: each day picks a random base hour (6-10) and adds 0-45 min jitter
    so that the cron never fires at the exact same time.
    """
    import asyncio as _asyncio
    import random as _random
    from datetime import timedelta
    from api.autopilot import run_daily_all
    while True:
        now = datetime.now(timezone.utc)
        # Pick random window (morning/midday/evening) + random jitter — anti-footprint
        _windows = [(6, 10), (11, 14), (19, 22)]
        _win = _random.choice(_windows)
        rand_hour = _random.randint(*_win)
        rand_minute = _random.randint(0, 59)
        next_run = now.replace(hour=rand_hour, minute=rand_minute, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
            _win = _random.choice(_windows)
            next_run = next_run.replace(hour=_random.randint(*_win), minute=_random.randint(0, 59))
        wait_sec = (next_run - now).total_seconds()
        logger.info(f"[DailyCron] Next autopilot run scheduled at {next_run.strftime('%Y-%m-%d %H:%M')} UTC (in {wait_sec/3600:.1f}h)")
        await _asyncio.sleep(wait_sec)
        try:
            await run_daily_all()
            logger.info(f"[DailyCron] Autopilot triggered at {datetime.now(timezone.utc).isoformat()}")
        except Exception as e:
            logger.error(f"[DailyCron] Error: {e}", exc_info=True)


async def _date_modified_refresh_cron():
    """Refresh dateModified on published WP posts every ~6 months (180 days)."""
    import asyncio as _asyncio
    from datetime import timedelta
    import aiosqlite as _aiosqlite
    import httpx as _httpx
    import base64 as _b64

    # Run every Sunday at 04:00 UTC
    while True:
        now = datetime.now(timezone.utc)
        days_ahead = (6 - now.weekday()) % 7 or 7  # next Sunday
        next_run = (now + timedelta(days=days_ahead)).replace(hour=4, minute=0, second=0, microsecond=0)
        wait_sec = max(0, (next_run - now).total_seconds())
        await _asyncio.sleep(wait_sec)
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
            async with _aiosqlite.connect(DB_PATH) as db:
                db.row_factory = _aiosqlite.Row
                async with db.execute(
                    """SELECT dk.id, dk.wp_post_url, md.domain, md.wp_login, md.wp_pass
                       FROM domain_keywords dk
                       JOIN my_domains md ON md.id = dk.my_domain_id
                       WHERE dk.status='published' AND dk.wp_post_url!=''
                         AND (dk.published_at IS NULL OR dk.published_at < ?)
                       LIMIT 50""",
                    (cutoff,)
                ) as cur:
                    posts = [dict(r) for r in await cur.fetchall()]
                # Also pick up manual publisher posts from `posts` table (same connection)
                async with db.execute(
                    """SELECT p.id, p.wp_post_url, md.domain, md.wp_login, md.wp_pass
                       FROM posts p
                       JOIN my_domains md ON md.id = p.my_domain_id
                       WHERE p.status='published' AND p.wp_post_url IS NOT NULL AND p.wp_post_url!=''
                         AND (p.created_at IS NULL OR p.created_at < ?)
                       LIMIT 50""",
                    (cutoff,)
                ) as cur:
                    posts_manual = [dict(r) for r in await cur.fetchall()]
            all_posts = posts + posts_manual

            async def _refresh_wp_post(hc: _httpx.AsyncClient, post: dict, table: str) -> bool:
                try:
                    url = post["wp_post_url"]
                    domain = post["domain"].rstrip("/")
                    if not domain.startswith("http"):
                        domain = "https://" + domain
                    plain_pass = get_plain_password(post['wp_pass'])
                    creds = _b64.b64encode(f"{post['wp_login']}:{plain_pass}".encode()).decode()
                    headers = {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}
                    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                    slug = url.rstrip("/").split("/")[-1]
                    search_resp = await hc.get(
                        f"{domain}/wp-json/wp/v2/posts",
                        params={"slug": slug, "_fields": "id"},
                        headers=headers
                    )
                    if search_resp.status_code == 200:
                        found = search_resp.json()
                        if found:
                            wp_id = found[0]["id"]
                            patch_resp = await hc.post(
                                f"{domain}/wp-json/wp/v2/posts/{wp_id}",
                                json={"date": now_str, "date_gmt": now_str, "modified": now_str},
                                headers=headers
                            )
                            if patch_resp.status_code in (200, 201):
                                update_col = "published_at" if table == "domain_keywords" else "created_at"
                                async with _aiosqlite.connect(DB_PATH) as db:
                                    await db.execute(
                                        f"UPDATE {table} SET {update_col}=? WHERE id=?",
                                        (datetime.now(timezone.utc).isoformat(), post["id"])
                                    )
                                    await db.commit()
                                return True
                except Exception as e:
                    logger.warning(f"[DateModifiedCron] Failed to refresh {post.get('wp_post_url', '?')}: {e}")
                return False

            refreshed = 0
            async with _httpx.AsyncClient(timeout=15, verify=False) as hc:
                for post in posts:
                    if await _refresh_wp_post(hc, post, "domain_keywords"):
                        refreshed += 1
                for post in posts_manual:
                    if await _refresh_wp_post(hc, post, "posts"):
                        refreshed += 1
            logger.info(f"[DateModifiedCron] Refreshed {refreshed}/{len(all_posts)} posts")
        except Exception as e:
            logger.error(f"[DateModifiedCron] Error: {e}", exc_info=True)


async def _weekly_deindex_cron():
    """Run deindex scan every Wednesday at 05:00 UTC."""
    import asyncio as _asyncio
    from datetime import timedelta
    while True:
        now = datetime.now(timezone.utc)
        # Next Wednesday (weekday 2) at 05:00 UTC
        days_ahead = (2 - now.weekday()) % 7 or 7
        next_run = (now + timedelta(days=days_ahead)).replace(hour=5, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=7)
        wait_sec = max(0, (next_run - now).total_seconds())
        logger.info(f"[DeindexCron] Next scan at {next_run.strftime('%Y-%m-%d %H:%M')} UTC (in {wait_sec/3600:.1f}h)")
        await _asyncio.sleep(wait_sec)
        try:
            from api.deindex import run_deindex_scan
            result = await run_deindex_scan()
            logger.info(f"[DeindexCron] Scan done: {result['total_checked']} checked, {result['dead']} dead")
        except Exception as e:
            logger.error(f"[DeindexCron] Error: {e}", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with aiosqlite.connect(DB_PATH) as db:
        # Enable WAL mode for better concurrent read/write performance
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA cache_size=-64000")  # 64MB cache
        await db.execute("PRAGMA busy_timeout=5000")  # 5s retry on locked DB
        await db.executescript(CREATE_TABLES_SQL)
        # Migration: add wp_ok column if missing
        try:
            await db.execute("ALTER TABLE my_domains ADD COLUMN wp_ok INTEGER DEFAULT NULL")
            await db.commit()
        except Exception:
            pass  # column already exists
        await import_csv_domains(db)
        # Migration: batch_tag column in posts
        try:
            await db.execute("ALTER TABLE posts ADD COLUMN batch_tag TEXT DEFAULT ''")
            await db.commit()
        except Exception:
            pass  # column already exists
        # Migration: my_domains extra columns
        for col, typedef in [
            ("http_user", "TEXT DEFAULT ''"),
            ("http_pass", "TEXT DEFAULT ''"),
            ("project_id", "INTEGER DEFAULT NULL"),
            ("batch_tag", "TEXT DEFAULT ''"),
        ]:
            try:
                await db.execute(f"ALTER TABLE my_domains ADD COLUMN {col} {typedef}")
                await db.commit()
            except Exception:
                pass
        # Migration: keyword column in posts (for interlinking)
        try:
            await db.execute("ALTER TABLE posts ADD COLUMN keyword TEXT DEFAULT ''")
            await db.commit()
        except Exception:
            pass  # column already exists
        # DB indexes for autopilot performance
        try:
            await db.executescript("""
                CREATE INDEX IF NOT EXISTS idx_dk_schedule_status
                    ON domain_keywords(schedule_id, status);
                CREATE INDEX IF NOT EXISTS idx_dk_status
                    ON domain_keywords(status);
                CREATE INDEX IF NOT EXISTS idx_posts_status
                    ON posts(status);
                CREATE INDEX IF NOT EXISTS idx_posts_created
                    ON posts(created_at);
                CREATE INDEX IF NOT EXISTS idx_posts_my_domain
                    ON posts(my_domain_id);
                CREATE INDEX IF NOT EXISTS idx_posts_domain_status
                    ON posts(my_domain_id, status);
            """)
            await db.commit()
        except Exception:
            pass
        # Migration: encrypt plaintext wp_pass values with Fernet
        try:
            async with db.execute(
                "SELECT id, wp_pass FROM my_domains WHERE wp_pass != '' AND wp_pass IS NOT NULL"
            ) as cursor:
                all_rows = await cursor.fetchall()
            encrypted_count = 0
            for row_id, wp_pass_val in all_rows:
                if not is_encrypted(wp_pass_val):
                    enc = encrypt_password(wp_pass_val)
                    await db.execute(
                        "UPDATE my_domains SET wp_pass = ? WHERE id = ?",
                        (enc, row_id),
                    )
                    encrypted_count += 1
            if encrypted_count > 0:
                await db.commit()
                logger.info(f"[Migration] Encrypted {encrypted_count} plaintext WordPress passwords.")
        except Exception as e:
            logger.error(f"[Migration] Password encryption error: {e}")
        # Create deindex_checks table
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
        # Create settings table (for Telegram config etc.)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        await db.commit()
        # Clean up expired SERP cache entries on startup
        try:
            import time as _time
            await db.execute("DELETE FROM serp_cache WHERE expires_at < ?", (_time.time(),))
            await db.commit()
        except Exception:
            pass  # table may not exist yet on first run
        # Clean up expired topical map cache entries
        try:
            await db.execute("DELETE FROM topical_map_cache WHERE expires_at < ?", (_time.time(),))
            await db.commit()
        except Exception:
            pass  # table may not exist yet on first run
    # Start background crons
    import asyncio as _asyncio
    cron_task = _asyncio.create_task(_weekly_cron())
    daily_task = _asyncio.create_task(_daily_autopilot_cron())
    date_mod_task = _asyncio.create_task(_date_modified_refresh_cron())
    deindex_task = _asyncio.create_task(_weekly_deindex_cron())
    yield
    cron_task.cancel()
    daily_task.cancel()
    date_mod_task.cancel()
    deindex_task.cancel()


app = FastAPI(title="PBN Publisher API", version="1.0.0", lifespan=lifespan)

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Simple rate limiter — per IP, max 60 requests per minute for API endpoints
import time as _time
_rate_limit_store: dict[str, list[float]] = {}
_RATE_LIMIT_WINDOW = 60  # seconds
_RATE_LIMIT_MAX = 60  # requests per window
_rate_limit_last_cleanup = _time.time()
# Stricter limits for expensive endpoints (article generation, publishing)
_RATE_LIMIT_STRICT = {"/api/publish": 10, "/api/content-writer/generate": 10, "/api/autopilot/run": 5}


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    global _rate_limit_last_cleanup
    path = request.url.path
    if not path.startswith("/api"):
        return await call_next(request)
    client_ip = request.client.host if request.client else "unknown"
    now = _time.time()
    # Periodic cleanup of stale IPs (every 5 minutes) + hard cap at 10k IPs
    if now - _rate_limit_last_cleanup > 300:
        _rate_limit_last_cleanup = now
        stale_ips = [ip for ip, hits in _rate_limit_store.items() if not hits or hits[-1] < now - _RATE_LIMIT_WINDOW]
        for ip in stale_ips:
            _rate_limit_store.pop(ip, None)
        # Hard cap: if still over 10k entries, evict oldest
        if len(_rate_limit_store) > 10_000:
            sorted_ips = sorted(_rate_limit_store.items(), key=lambda x: x[1][-1] if x[1] else 0)
            for ip, _ in sorted_ips[:len(_rate_limit_store) - 10_000]:
                _rate_limit_store.pop(ip, None)
    hits = _rate_limit_store.get(client_ip, [])
    # Remove old entries
    hits = [t for t in hits if t > now - _RATE_LIMIT_WINDOW]
    # Per-endpoint strict limit for expensive operations
    max_req = _RATE_LIMIT_MAX
    for prefix, limit in _RATE_LIMIT_STRICT.items():
        if path.startswith(prefix):
            max_req = limit
            break
    if len(hits) >= max_req:
        return Response(
            content=f'{{"detail":"Too many requests. Max {max_req}/min."}}',
            status_code=429,
            media_type="application/json",
        )
    hits.append(now)
    _rate_limit_store[client_ip] = hits
    return await call_next(request)


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    # Skip auth if no password set (local dev)
    if not APP_PASSWORD:
        return await call_next(request)
    # Skip auth for health check and static assets
    path = request.url.path
    # Pass-through: health, static assets, login endpoint, and all frontend SPA routes
    if (path in ("/health", "/", "/api/auth/login")
            or path.startswith("/assets")
            or not path.startswith("/api")  # all non-API paths = SPA routes
            or path.endswith((".svg", ".ico", ".png", ".webmanifest", ".js", ".css"))):
        return await call_next(request)
    # Allow cron endpoints — triggered by Railway Cron or internal scheduler
    if path in ("/api/autopilot/run-daily", "/api/autopilot/run-bulk"):
        cron_token = request.headers.get("X-Cron-Secret", "")
        # Accept if: no CRON_SECRET set, or token matches, or request from localhost
        client_host = request.client.host if request.client else ""
        is_local = client_host in ("127.0.0.1", "::1", "localhost")
        token_ok = CRON_SECRET and cron_token and secrets.compare_digest(cron_token, CRON_SECRET)
        if is_local or token_ok:
            return await call_next(request)
    auth = request.headers.get("Authorization", "")
    # Also accept auth via query param for SSE endpoints (EventSource can't send headers)
    if not auth:
        _auth_qp = request.query_params.get("_auth", "")
        if _auth_qp:
            auth = f"Basic {_auth_qp}"
    if auth.startswith("Basic "):
        import base64
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            user, pwd = decoded.split(":", 1)
            if secrets.compare_digest(user, APP_USER) and secrets.compare_digest(pwd, APP_PASSWORD):
                return await call_next(request)
        except Exception:
            pass
    # Return 401 WITHOUT WWW-Authenticate to avoid browser popup
    # Frontend handles redirect to login page
    return Response(
        content='{"detail":"Unauthorized"}',
        status_code=401,
        media_type="application/json",
    )

app.include_router(projects.router)
app.include_router(clients.router)
app.include_router(domains.router)
app.include_router(publish.router)
app.include_router(history.router)
app.include_router(topical_map.router)
app.include_router(content_writer.router)
app.include_router(autopilot.router)
app.include_router(health.router)
app.include_router(dashboard.router)
app.include_router(analytics.router)
app.include_router(deindex.router)
app.include_router(notifications.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


# Serve frontend static files (after API routes)
FRONTEND_DIST = os.path.join(os.path.dirname(__file__), "frontend_dist")
if os.path.exists(FRONTEND_DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIST, "assets")), name="assets")

    @app.get("/")
    async def serve_index():
        return FileResponse(os.path.join(FRONTEND_DIST, "index.html"))

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        file_path = os.path.join(FRONTEND_DIST, full_path)
        # Prevent path traversal — resolved path must stay within FRONTEND_DIST
        if not os.path.realpath(file_path).startswith(os.path.realpath(FRONTEND_DIST)):
            return FileResponse(os.path.join(FRONTEND_DIST, "index.html"))
        if os.path.exists(file_path) and os.path.isfile(file_path):
            return FileResponse(file_path)
        return FileResponse(os.path.join(FRONTEND_DIST, "index.html"))
else:
    @app.get("/")
    async def root():
        return {"message": "PBN Publisher API running"}

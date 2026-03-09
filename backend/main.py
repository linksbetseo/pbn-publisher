import csv
import os
import secrets
import aiosqlite
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from config import DB_PATH, CSV_PATH
from api import projects, clients, domains, publish, history

APP_USER = os.getenv("APP_USER", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

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
        print(f"CSV not found at {csv_path}, skipping import.")
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
                await db.execute(
                    "INSERT INTO my_domains (domain, wp_login, wp_pass, server, active) VALUES (?, ?, ?, ?, 1)",
                    (domain, wp_login, wp_pass, server),
                )
                rows_inserted += 1
        await db.commit()
        print(f"Imported {rows_inserted} domains from CSV.")
    except Exception as e:
        print(f"CSV import error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_TABLES_SQL)
        # Migration: add wp_ok column if missing
        try:
            await db.execute("ALTER TABLE my_domains ADD COLUMN wp_ok INTEGER DEFAULT NULL")
            await db.commit()
        except Exception:
            pass  # column already exists
        await import_csv_domains(db)
    yield


app = FastAPI(title="PBN Publisher API", version="1.0.0", lifespan=lifespan)

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    # Skip auth if no password set (local dev)
    if not APP_PASSWORD:
        return await call_next(request)
    # Skip auth for health check
    if request.url.path == "/health":
        return await call_next(request)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        import base64
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            user, pwd = decoded.split(":", 1)
            if secrets.compare_digest(user, APP_USER) and secrets.compare_digest(pwd, APP_PASSWORD):
                return await call_next(request)
        except Exception:
            pass
    return Response(
        content="Unauthorized",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="PBN Publisher"'},
    )

app.include_router(projects.router)
app.include_router(clients.router)
app.include_router(domains.router)
app.include_router(publish.router)
app.include_router(history.router)


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
        if os.path.exists(file_path) and os.path.isfile(file_path):
            return FileResponse(file_path)
        return FileResponse(os.path.join(FRONTEND_DIST, "index.html"))
else:
    @app.get("/")
    async def root():
        return {"message": "PBN Publisher API running"}

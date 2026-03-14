"""
Notifications & Settings API — Telegram, GPT model, encryption status.

Endpoints:
- GET  /api/notifications/settings         — all settings (Telegram, GPT model, encryption)
- POST /api/notifications/telegram-config  — save bot token + chat ID
- GET  /api/notifications/telegram-test    — send a test message
- POST /api/notifications/gpt-model       — save GPT model preference
"""

import os
import aiosqlite
from fastapi import APIRouter
from pydantic import BaseModel

from config import DB_PATH
from services.telegram_service import send_telegram, is_configured

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class TelegramConfigRequest(BaseModel):
    bot_token: str = ""
    chat_id: str = ""


class GptModelRequest(BaseModel):
    model: str = "gpt-4o-mini"


async def _ensure_settings_table():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        await db.commit()


@router.get("/settings")
async def get_settings():
    """Return all settings: Telegram, GPT model, encryption status."""
    configured = await is_configured()
    bot_token = ""
    chat_id = ""
    gpt_model = "gpt-4o-mini"
    # Notification preference keys
    _notify_keys = [
        "notify_autopilot_done", "notify_bulk_publish_done",
        "notify_news_approve", "notify_news_generate",
        "notify_health_snapshot", "notify_errors",
    ]
    notify_prefs: dict[str, bool] = {k: True for k in _notify_keys}  # default all ON

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            _all_keys = ['telegram_bot_token', 'telegram_chat_id', 'gpt_model'] + _notify_keys
            _placeholders = ','.join('?' * len(_all_keys))
            async with db.execute(
                f"SELECT key, value FROM settings WHERE key IN ({_placeholders})",
                _all_keys,
            ) as cur:
                for key, value in await cur.fetchall():
                    if key == "telegram_bot_token" and value:
                        bot_token = value[:8] + "..." + value[-4:] if len(value) > 12 else "***"
                    elif key == "telegram_chat_id":
                        chat_id = value
                    elif key == "gpt_model" and value:
                        gpt_model = value
                    elif key in _notify_keys:
                        notify_prefs[key] = value == "1"
    except Exception:
        pass

    # Encryption status
    encryption_key_set = bool(os.getenv("PBN_ENCRYPTION_KEY", ""))

    # B10: API keys status (configured or not, never expose actual keys)
    api_keys_status = {
        "openai": bool(os.getenv("OPENAI_API_KEY", "")),
        "dataforseo": bool(os.getenv("DATAFORSEO_LOGIN", "")) and bool(os.getenv("DATAFORSEO_PASSWORD", "")),
        "rocket_indexer": bool(os.getenv("ROCKET_INDEXER_TOKEN", "")),
        "freepik": bool(os.getenv("FREEPIK_API_KEY", "")),
    }

    # Default image source
    default_image_source = "freepik_flux"
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT value FROM settings WHERE key = 'default_image_source'"
            ) as cur:
                row = await cur.fetchone()
                if row and row[0]:
                    default_image_source = row[0]
    except Exception:
        pass

    return {
        "telegram_configured": configured,
        "telegram_bot_token_masked": bot_token,
        "telegram_chat_id": chat_id,
        "gpt_model": gpt_model,
        "encryption_key_set": encryption_key_set,
        "api_keys_status": api_keys_status,
        "default_image_source": default_image_source,
        "notify_prefs": notify_prefs,
    }


@router.post("/telegram-config")
async def save_telegram_config(body: TelegramConfigRequest):
    """Save Telegram bot token and chat ID to DB settings."""
    await _ensure_settings_table()
    async with aiosqlite.connect(DB_PATH) as db:
        if body.bot_token:
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('telegram_bot_token', ?)",
                (body.bot_token,)
            )
        if body.chat_id:
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('telegram_chat_id', ?)",
                (body.chat_id,)
            )
        await db.commit()
    configured = await is_configured()
    return {"ok": True, "telegram_configured": configured}


@router.get("/telegram-test")
async def telegram_test():
    """Send a test message via Telegram."""
    configured = await is_configured()
    if not configured:
        return {"ok": False, "error": "Telegram not configured. Set bot_token and chat_id first."}
    success = await send_telegram(
        "<b>PBN Publisher</b>\n\nTest notification. Telegram integration is working correctly."
    )
    return {"ok": success, "message": "Test message sent" if success else "Failed to send"}


@router.post("/gpt-model")
async def save_gpt_model(body: GptModelRequest):
    """Save the GPT model preference to DB settings."""
    allowed = [
        "gpt-4o-mini", "gpt-4o", "gpt-4-turbo", "gpt-4.1-mini",
        "gpt-4.1", "gpt-4.1-nano", "o3-mini",
    ]
    if body.model not in allowed:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Model not allowed. Choose from: {allowed}")
    await _ensure_settings_table()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('gpt_model', ?)",
            (body.model,)
        )
        await db.commit()
    return {"ok": True, "gpt_model": body.model}


@router.post("/default-image-source")
async def save_default_image_source(body: dict):
    """Save the default image source preference to DB settings."""
    source = body.get("image_source", "freepik_flux")
    allowed = ["none", "freepik_stock", "freepik_zimage", "freepik_flux", "dalle", "gemini"]
    if source not in allowed:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Source not allowed. Choose from: {allowed}")
    await _ensure_settings_table()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('default_image_source', ?)",
            (source,)
        )
        await db.commit()
    return {"ok": True, "default_image_source": source}


# All valid notification preference keys
NOTIFY_KEYS = {
    "notify_autopilot_done", "notify_bulk_publish_done",
    "notify_news_approve", "notify_news_generate",
    "notify_health_snapshot", "notify_errors",
}


@router.post("/notify-prefs")
async def save_notify_prefs(body: dict):
    """Save notification preferences (checkboxes). Body: {key: bool, ...}."""
    await _ensure_settings_table()
    async with aiosqlite.connect(DB_PATH) as db:
        for key, val in body.items():
            if key in NOTIFY_KEYS:
                await db.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (key, "1" if val else "0"),
                )
        await db.commit()
    return {"ok": True}


async def should_notify(pref_key: str) -> bool:
    """Check if a notification preference is enabled. Default: True (all on)."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT value FROM settings WHERE key = ?", (pref_key,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return row[0] == "1"
    except Exception:
        pass
    return True  # default on

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
        "telegram_indexer": bool(os.getenv("TELEGRAM_INDEXER_TOKEN", "")),
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


class CustomLlmRequest(BaseModel):
    enabled: bool = False
    base_url: str = ""
    model: str = ""
    api_key: str = ""


@router.get("/custom-llm")
async def get_custom_llm():
    """Return current custom LLM config (api_key masked)."""
    await _ensure_settings_table()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT key, value FROM settings WHERE key IN ('custom_llm_enabled','custom_llm_base_url','custom_llm_model','custom_llm_api_key')"
        ) as cur:
            rows = dict(await cur.fetchall())
    api_key_raw = rows.get("custom_llm_api_key", "")
    masked = (api_key_raw[:6] + "..." + api_key_raw[-4:]) if len(api_key_raw) > 10 else ("***" if api_key_raw else "")
    return {
        "enabled": rows.get("custom_llm_enabled", "0") == "1",
        "base_url": rows.get("custom_llm_base_url", ""),
        "model": rows.get("custom_llm_model", ""),
        "api_key_masked": masked,
        "api_key_set": bool(api_key_raw),
    }


@router.post("/custom-llm")
async def save_custom_llm(body: CustomLlmRequest):
    """Save custom LLM config. Clears cache after save."""
    await _ensure_settings_table()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('custom_llm_enabled',?)", ("1" if body.enabled else "0",))
        await db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('custom_llm_base_url',?)", (body.base_url.strip(),))
        await db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('custom_llm_model',?)", (body.model.strip(),))
        # Only overwrite api_key if user sent a non-masked value
        if body.api_key and "..." not in body.api_key:
            await db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('custom_llm_api_key',?)", (body.api_key,))
        await db.commit()
    # Invalidate in-memory cache
    try:
        from services.openai_service import _custom_llm_cache
        _custom_llm_cache["data"] = None
        _custom_llm_cache["ts"] = 0
    except Exception:
        pass
    return {"ok": True}


@router.post("/custom-llm/test")
async def test_custom_llm():
    """Send a test completion to the configured custom LLM endpoint."""
    from services.openai_service import get_custom_llm_config
    cfg = await get_custom_llm_config()
    if not cfg["base_url"] or not cfg["model"]:
        from fastapi import HTTPException
        raise HTTPException(400, "Brak base_url lub model — najpierw skonfiguruj i zapisz")
    try:
        from openai import AsyncOpenAI
        base = cfg["base_url"].rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        test_client = AsyncOpenAI(
            api_key=cfg["api_key"] or "not-needed",
            base_url=base,
            timeout=120.0,
            max_retries=0,
        )
        resp = await test_client.chat.completions.create(
            model=cfg["model"],
            messages=[{"role": "user", "content": "Reply with: OK"}],
            max_tokens=10,
            temperature=0,
        )
        answer = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        return {"ok": True, "response": answer, "model": cfg["model"], "base_url": cfg["base_url"]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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

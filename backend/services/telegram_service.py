"""
Telegram notification service.

Sends messages via Telegram Bot API. Configuration is loaded from:
1. DB `settings` table (key: telegram_bot_token, telegram_chat_id)
2. Environment variables TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID as fallback
"""

import logging
import aiosqlite
import httpx

from config import DB_PATH

logger = logging.getLogger(__name__)

# Env-var fallback (imported lazily to avoid circular imports)
_ENV_BOT_TOKEN: str | None = None
_ENV_CHAT_ID: str | None = None


def _load_env_vars():
    global _ENV_BOT_TOKEN, _ENV_CHAT_ID
    if _ENV_BOT_TOKEN is None:
        from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        _ENV_BOT_TOKEN = TELEGRAM_BOT_TOKEN
        _ENV_CHAT_ID = TELEGRAM_CHAT_ID


async def _get_telegram_config() -> tuple[str, str]:
    """Return (bot_token, chat_id) from DB settings, falling back to env vars."""
    _load_env_vars()
    bot_token = _ENV_BOT_TOKEN or ""
    chat_id = _ENV_CHAT_ID or ""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT key, value FROM settings WHERE key IN ('telegram_bot_token', 'telegram_chat_id')"
            ) as cur:
                rows = await cur.fetchall()
            for key, value in rows:
                if key == "telegram_bot_token" and value:
                    bot_token = value
                elif key == "telegram_chat_id" and value:
                    chat_id = value
    except Exception:
        pass  # table may not exist yet
    return bot_token, chat_id


async def send_telegram(message: str, parse_mode: str = "HTML") -> bool:
    """Send a Telegram message. Returns True on success, False otherwise."""
    bot_token, chat_id = await _get_telegram_config()
    if not bot_token or not chat_id:
        logger.debug("Telegram not configured, skipping notification")
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": parse_mode,
            })
            if resp.status_code == 200:
                return True
            logger.warning(f"Telegram API returned {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")
        return False


async def is_configured() -> bool:
    """Check if Telegram bot token and chat ID are set."""
    bot_token, chat_id = await _get_telegram_config()
    return bool(bot_token and chat_id)

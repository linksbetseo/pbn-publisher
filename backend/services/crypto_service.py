"""
AES-256 encryption for WordPress passwords using Fernet (symmetric encryption).

Fernet uses AES-128-CBC under the hood with HMAC-SHA256 for authentication.
The key is derived from a URL-safe base64-encoded 32-byte key.

Usage:
    from services.crypto_service import encrypt_password, decrypt_password, is_encrypted
"""
import os
import logging

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Load key from environment; auto-generate and warn if missing
_ENCRYPTION_KEY = os.getenv("PBN_ENCRYPTION_KEY", "")

if not _ENCRYPTION_KEY:
    _ENCRYPTION_KEY = Fernet.generate_key().decode()
    logger.warning(
        "[Crypto] PBN_ENCRYPTION_KEY not set! Auto-generated a temporary key. "
        "Add this to your .env file to persist encryption across restarts:\n"
        f"  PBN_ENCRYPTION_KEY={_ENCRYPTION_KEY}"
    )
    print(
        f"\n{'='*70}\n"
        f"WARNING: PBN_ENCRYPTION_KEY not found in environment.\n"
        f"A temporary key was generated. Passwords encrypted with this key\n"
        f"will be UNREADABLE after restart unless you save it.\n\n"
        f"Add to your .env file:\n"
        f"  PBN_ENCRYPTION_KEY={_ENCRYPTION_KEY}\n"
        f"{'='*70}\n"
    )

_fernet = Fernet(_ENCRYPTION_KEY.encode() if isinstance(_ENCRYPTION_KEY, str) else _ENCRYPTION_KEY)


def encrypt_password(plain: str) -> str:
    """Encrypt a plaintext password. Returns a Fernet token string (starts with 'gAAAAA')."""
    if not plain:
        return plain
    return _fernet.encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_password(encrypted: str) -> str:
    """Decrypt a Fernet-encrypted password. Returns plaintext."""
    if not encrypted:
        return encrypted
    try:
        return _fernet.decrypt(encrypted.encode("utf-8")).decode("utf-8")
    except (InvalidToken, Exception) as e:
        logger.error(f"[Crypto] Failed to decrypt password: {e}")
        raise ValueError("Failed to decrypt password. Check PBN_ENCRYPTION_KEY.") from e


def is_encrypted(value: str) -> bool:
    """Check if a value looks like a Fernet token (starts with 'gAAAAA')."""
    if not value:
        return False
    return value.startswith("gAAAAA")


def get_plain_password(wp_pass: str) -> str:
    """Return decrypted password if encrypted, otherwise return as-is.
    This is the main helper for transparent decryption."""
    if not wp_pass:
        return wp_pass
    if is_encrypted(wp_pass):
        return decrypt_password(wp_pass)
    return wp_pass

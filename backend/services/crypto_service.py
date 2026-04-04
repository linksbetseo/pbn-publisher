"""
AES-256 encryption for WordPress passwords using Fernet (symmetric encryption).

Fernet uses AES-128-CBC under the hood with HMAC-SHA256 for authentication.
The key is derived from a URL-safe base64-encoded 32-byte key.

Usage:
    from services.crypto_service import encrypt_password, decrypt_password, is_encrypted
"""
import os
import sys
import logging

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Load key from environment — REQUIRED in production.
# If missing, generate one and persist it to a local .encryption_key file
# so that restarts don't lose access to encrypted passwords.
_ENCRYPTION_KEY = os.getenv("PBN_ENCRYPTION_KEY", "")
_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".encryption_key")
_KEY_FILE = os.path.normpath(_KEY_FILE)

if not _ENCRYPTION_KEY:
    # Try to load from persisted key file
    if os.path.exists(_KEY_FILE):
        try:
            with open(_KEY_FILE, "r") as f:
                _ENCRYPTION_KEY = f.read().strip()
            logger.info("[Crypto] Loaded encryption key from .encryption_key file.")
        except Exception as e:
            logger.error(f"[Crypto] Failed to read key file: {e}")
    if not _ENCRYPTION_KEY:
        # Generate and persist to file so it survives restarts
        _ENCRYPTION_KEY = Fernet.generate_key().decode()
        try:
            with open(_KEY_FILE, "w") as f:
                f.write(_ENCRYPTION_KEY)
            logger.warning(
                "[Crypto] PBN_ENCRYPTION_KEY not set! Generated key and saved to .encryption_key file. "
                "For production, set PBN_ENCRYPTION_KEY env var instead."
            )
        except Exception as e:
            logger.error(f"[Crypto] Could not persist key to file: {e}")
            logger.critical(
                "[Crypto] PBN_ENCRYPTION_KEY not set and key cannot be persisted! "
                "Encrypted passwords WILL BE LOST on restart. Set PBN_ENCRYPTION_KEY env var."
            )
        print(
            f"\n{'='*70}\n"
            f"WARNING: PBN_ENCRYPTION_KEY not found in environment.\n"
            f"A key was generated and saved to: {_KEY_FILE}\n"
            f"For production, add to your env vars:\n"
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
        try:
            return decrypt_password(wp_pass)
        except Exception as e:
            logger.error(
                f"[Crypto] Decryption failed — PBN_ENCRYPTION_KEY mismatch? "
                f"Returning raw value (may fail WP auth). Error: {e}"
            )
            return wp_pass  # Return as-is — WP auth will fail but won't crash
    return wp_pass

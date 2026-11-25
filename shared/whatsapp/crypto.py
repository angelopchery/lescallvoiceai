"""Encrypt the WhatsApp access token at rest.

A permanent system-user token is a WhatsApp account: whoever holds it can
message every customer from the tenant's number. The repo is bind-mounted
into every container and database dumps get copied around, so the token is
stored as Fernet ciphertext and the key lives only in the environment
(WA_TOKEN_KEY). No key → no connecting, with a message saying so; there is no
plaintext fallback.
"""

from __future__ import annotations

from shared.whatsapp import config


class TokenKeyMissing(RuntimeError):
    pass


def _fernet():
    from cryptography.fernet import Fernet

    key = config.token_key()
    if not key:
        raise TokenKeyMissing(
            "WhatsApp token encryption key (WA_TOKEN_KEY) is not configured on "
            "the platform, so an access token cannot be stored safely.")
    try:
        return Fernet(key.encode())
    except Exception as exc:
        raise TokenKeyMissing(f"WA_TOKEN_KEY is not a valid Fernet key: {exc}") from exc


def encrypt(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt(blob: str) -> str:
    return _fernet().decrypt(blob.encode()).decode()


def new_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()

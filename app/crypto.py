"""Field-level encryption for sensitive columns (SMTP password, Turnstile secret key,
2FA TOTP secret) so a raw database file/dump doesn't hand over plaintext credentials.

The encryption key is derived from SECRET_KEY by default, so this works with zero
extra configuration. For stronger separation between "forge a session" and "read my
SMTP password", set a dedicated FIELD_ENCRYPTION_KEY env var instead (any random
string — it gets run through the same KDF below).

Values written before this was introduced are plaintext in the database. Rather than
requiring a manual migration, EncryptedString transparently falls back to returning
the raw stored value if it doesn't look like a Fernet token, so old rows keep working
and get encrypted the next time they're saved.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.types import TypeDecorator, Text

_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet
    from flask import current_app
    key_material = os.environ.get('FIELD_ENCRYPTION_KEY') or current_app.config['SECRET_KEY']
    digest = hashlib.sha256(key_material.encode('utf-8')).digest()
    _fernet = Fernet(base64.urlsafe_b64encode(digest))
    return _fernet


class EncryptedString(TypeDecorator):
    """A Text column that's encrypted at rest and transparently decrypted on read."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or value == '':
            return value
        return _get_fernet().encrypt(value.encode('utf-8')).decode('utf-8')

    def process_result_value(self, value, dialect):
        if value is None or value == '':
            return value
        try:
            return _get_fernet().decrypt(value.encode('utf-8')).decode('utf-8')
        except (InvalidToken, ValueError):
            # Pre-encryption legacy plaintext value — hand it back as-is.
            return value

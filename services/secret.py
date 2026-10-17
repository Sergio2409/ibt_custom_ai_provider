"""Encryption of provider API keys with master keys kept outside the database.

The master keys come from the ``ibt_ai_secret_keys`` option of the Odoo
configuration file, or else from the ``IBT_AI_SECRET_KEYS`` environment
variable: Fernet keys separated by commas, the first one encrypts, all of them
decrypt (so a new key can be put first while the old one is still needed).
"""
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from odoo.tools import config
from odoo.tools.translate import LazyTranslate

from .client import ProviderError

_ = LazyTranslate(__name__, default_lang="en_US")

CONFIG_OPTION = "ibt_ai_secret_keys"
UNREADABLE = _(
    "A provider API key cannot be decrypted with the configured master keys "
    "(ibt_ai_secret_keys). Restore the master key or set the API key again."
)


class SecretError(ProviderError):
    pass


def _fernet():
    keys = config.get(CONFIG_OPTION) or os.environ.get(CONFIG_OPTION.upper()) or ""
    keys = [key.strip() for key in keys.split(",") if key.strip()]
    if not keys:
        raise SecretError(_(
            "No master key is configured for provider API keys: set ibt_ai_secret_keys "
            "in the Odoo configuration file (see the module README)."
        ))
    try:
        return MultiFernet([Fernet(key) for key in keys])
    except ValueError:
        raise SecretError(_("The ibt_ai_secret_keys option must contain valid Fernet keys.")) from None


def encrypt(text):
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token):
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise SecretError(UNREADABLE) from None


def rotate(token):
    """Re-encrypt ``token`` with the first master key."""
    try:
        return _fernet().rotate(token.encode()).decode()
    except InvalidToken:
        raise SecretError(UNREADABLE) from None

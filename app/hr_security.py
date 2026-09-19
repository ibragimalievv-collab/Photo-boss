"""Authentication for the HR workspace. Never trust a client-selected role."""
from __future__ import annotations
import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl


class AccessError(ValueError):
    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


def validate_init_data(raw: str, token: str, *, now: float | None = None,
                       max_age: int = 3600) -> int:
    """Validate Telegram Mini App initData HMAC, expiry and unique fields."""
    message = "Откройте приложение заново через Telegram."
    try:
        if not token or not raw or len(raw.encode("utf-8")) > 16384:
            raise ValueError
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=64)
        values = dict(pairs)
        if len(values) != len(pairs):
            raise ValueError
        supplied_hash = values.pop("hash")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", supplied_hash):
            raise ValueError
        check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied_hash.lower()):
            raise ValueError
        issued = int(values["auth_date"])
        current = time.time() if now is None else now
        if issued < current - max_age or issued > current + 30:
            raise ValueError
        user = json.loads(values["user"])
        uid = user["id"]
        if type(uid) is not int or not 0 < uid < 2**52 or user.get("is_bot"):
            raise ValueError
        return uid
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise AccessError(message, 401) from exc


def webhook_secret(bot_token: str) -> str:
    """Domain-separated server-only secret for inbound Telegram webhooks."""
    if not bot_token:
        raise ValueError("BOT_TOKEN is required")
    return hmac.new(bot_token.encode(), b"photo-boss:telegram-webhook:v2", hashlib.sha256).hexdigest()

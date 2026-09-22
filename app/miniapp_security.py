"""Authentication and authorization shared by the production Mini App."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

THEMES = frozenset({"premium", "light", "photo"})
STAFF_ROLES = frozenset({"OWNER", "ADMIN", "MANAGER", "PHOTOGRAPHER"})
# Telegram clients can reuse the signed WebView launch when the Mini App is
# reopened. Keep the API and its boundary tests on one explicit policy.
MINIAPP_SESSION_MAX_AGE = 24 * 60 * 60


def booking_assignment_columns(roles):
    """Trusted column names for assignments allowed by the current staff roles."""
    return tuple(column for role, column in (
        ("PHOTOGRAPHER", "photographer_id"), ("MANAGER", "manager_id")
    ) if role in roles)


class AccessError(ValueError):
    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


def validate_init_data(raw: str, token: str, *, now: float | None = None,
                       max_age: int = 3600) -> int:
    """Validate signed Telegram initData, never initDataUnsafe or client roles."""
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


def owner_launch_token(telegram_id: int, token: str, *, now: float | None = None,
                       ttl: int = 2 * 60 * 60) -> str:
    """Create a short-lived server-signed owner fallback token for Telegram WebViews."""
    if type(telegram_id) is not int or not 0 < telegram_id < 2**52 or not token:
        raise ValueError("Invalid launch token input")
    issued = int(time.time() if now is None else now)
    payload = f"{telegram_id}:{issued + ttl}".encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    secret = hmac.new(token.encode(), b"photo-boss:owner-launch:v1", hashlib.sha256).digest()
    signature = hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def validate_owner_launch_token(raw: str, token: str, *, now: float | None = None) -> int:
    """Validate the short-lived fallback token. Role is checked after DB lookup."""
    try:
        if not token or not raw or len(raw) > 512:
            raise ValueError
        encoded, supplied = raw.split(".", 1)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded) or not re.fullmatch(r"[a-f0-9]{64}", supplied):
            raise ValueError
        secret = hmac.new(token.encode(), b"photo-boss:owner-launch:v1", hashlib.sha256).digest()
        expected = hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            raise ValueError
        padding = "=" * (-len(encoded) % 4)
        telegram_id_text, expires_text = base64.urlsafe_b64decode(encoded + padding).decode().split(":", 1)
        telegram_id, expires = int(telegram_id_text), int(expires_text)
        current = int(time.time() if now is None else now)
        if not 0 < telegram_id < 2**52 or expires < current or expires > current + 2 * 60 * 60 + 60:
            raise ValueError
        return telegram_id
    except (ValueError, TypeError, UnicodeDecodeError, OverflowError) as exc:
        raise AccessError("Ссылка входа устарела. Отправьте /app в боте и откройте новую кнопку.", 401) from exc


def role_permissions(roles) -> dict:
    roles = set(roles)
    owner, admin = "OWNER" in roles, "ADMIN" in roles
    return {"financeScope": "all" if owner else "today" if admin else "self",
            "audit": owner, "manageSchedule": owner or admin,
            "assignPhotographers": owner or admin,
            "manageBookings": owner or admin or "MANAGER" in roles}


def financial_period(roles, period: str, today: date, *, start=None, end=None):
    """Return inclusive local dates. Enforce scope before reading financial rows."""
    permissions = role_permissions(roles)
    if permissions["financeScope"] == "today" and (
        period != "today" or start is not None or end is not None
    ):
        raise AccessError("Общая касса за прошлые периоды доступна только владельцу.")
    if period == "today":
        if start is not None or end is not None:
            raise AccessError("Лишние параметры периода.", 400)
        return today, today
    if period == "week":
        return today - timedelta(days=today.weekday()), today
    if period == "month":
        return today.replace(day=1), today
    if period == "custom" and permissions["financeScope"] == "all":
        try:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
            if not first <= last <= today or (last-first).days > 366:
                raise ValueError
            return first, last
        except (ValueError, TypeError) as exc:
            raise AccessError("Укажите корректный период не длиннее года.", 400) from exc
    raise AccessError("Недопустимый период.", 400)


def require_owner(roles):
    if "OWNER" not in roles:
        raise AccessError("Аудит доступен только владельцу.")


def require_schedule_editor(roles):
    if not {"OWNER", "ADMIN"} & set(roles):
        raise AccessError("Назначать смены может владелец или администратор.")


def utc_bounds(start: date, end: date, tz: ZoneInfo):
    first = datetime.combine(start, datetime.min.time(), tz)
    last = datetime.combine(end + timedelta(days=1), datetime.min.time(), tz)
    return (first.astimezone(timezone.utc).replace(tzinfo=None),
            last.astimezone(timezone.utc).replace(tzinfo=None))


def parse_shift(payload: dict, tz: ZoneInfo, today: date):
    try:
        if set(payload) != {"userId", "hotelId", "date", "start", "end"}:
            raise ValueError
        if not all(type(payload[k]) in (str, int) and re.fullmatch(r"[0-9]+", str(payload[k]))
                   for k in ("userId", "hotelId")):
            raise ValueError
        uid, hid = int(payload["userId"]), int(payload["hotelId"])
        if not 0 < uid < 2**31 or not 0 < hid < 2**31:
            raise ValueError
        day = date.fromisoformat(payload["date"])
        if not today <= day <= today + timedelta(days=366):
            raise ValueError
        if not all(re.fullmatch(r"\d{2}:\d{2}", payload[k]) for k in ("start", "end")):
            raise ValueError
        start = datetime.fromisoformat(f"{day}T{payload['start']}").replace(tzinfo=tz)
        end = datetime.fromisoformat(f"{day}T{payload['end']}").replace(tzinfo=tz)
        if not timedelta(0) < end-start <= timedelta(hours=16):
            raise ValueError
        return (uid, hid, start.astimezone(timezone.utc).replace(tzinfo=None),
                end.astimezone(timezone.utc).replace(tzinfo=None))
    except (ValueError, TypeError, KeyError) as exc:
        raise AccessError("Проверьте сотрудника, отель, дату и время смены (не более 16 часов).", 400) from exc


def webhook_secret(bot_token: str) -> str:
    """Domain-separated stable server secret, never published into the frontend."""
    if not bot_token:
        raise ValueError("BOT_TOKEN is required")
    return hmac.new(bot_token.encode(), b"photo-boss:telegram-webhook:v2", hashlib.sha256).hexdigest()

"""Telegram authentication and role boundaries, independent of the bot process."""
from __future__ import annotations

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


class AccessError(ValueError):
    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


def validate_init_data(raw: str, token: str, *, now: float | None = None,
                       max_age: int = 3600) -> int:
    """Validate signed initData, including auth_date, before trusting any user ID.

    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    Only hash is removed from the HMAC data-check-string. Never log credentials.
    """
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
        raise AccessError("Откройте приложение заново через Telegram.", 401) from exc


def role_permissions(roles) -> dict:
    roles = set(roles)
    owner, admin = "OWNER" in roles, "ADMIN" in roles
    return {"financeScope": "all" if owner else "today" if admin else "self",
            "audit": owner, "manageSchedule": owner or admin,
            "manageBookings": owner or admin or "MANAGER" in roles}


def financial_period(roles, period: str, today: date, *, start=None, end=None):
    """Inclusive local dates; check admin boundaries before financial queries."""
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
        return uid, hid, start.astimezone(timezone.utc).replace(tzinfo=None), end.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError, KeyError) as exc:
        raise AccessError("Проверьте сотрудника, отель, дату и время смены (не более 16 часов).", 400) from exc


def webhook_secret(bot_token: str) -> str:
    """Stable server-only, domain-separated secret for Telegram webhook ingress."""
    if not bot_token:
        raise ValueError("BOT_TOKEN is required")
    return hmac.new(bot_token.encode(), b"photo-boss:telegram-webhook:v2", hashlib.sha256).hexdigest()

"""Production Mini App backed by the existing database, never demo data.

No second payroll ledger is created. Financial mutations and photo delivery use
existing bot workflows. Plans are not presented as confirmed attendance.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from aiohttp import web
from sqlalchemy import text

from . import miniapp_sessions
from .miniapp_security import (
    MINIAPP_SESSION_MAX_AGE,
    STAFF_ROLES,
    THEMES,
    AccessError,
    financial_period,
    parse_shift,
    require_owner,
    require_schedule_editor,
    role_permissions,
    utc_bounds,
    validate_init_data,
    validate_owner_launch_token,
)
from .operations_guard import (
    OperationsResetError,
    current_generation,
    operation_lease,
    require_generation,
)
from .services.academy_growth import personal_tip
from .services.booking_access import booking_scope_sql
from .yandex_disk import YandexDiskError

logger = logging.getLogger(__name__)
PREFIX = "/api/miniapp"
ROLE_ORDER = ("OWNER", "ADMIN", "PHOTOGRAPHER", "MANAGER")
OPERATIONS_GENERATION_HEADER = "X-PhotoBoss-Operations-Generation"


def operational_mutation(request):
    """Only reset-owned workflows participate in the reset write barrier.

    Planned shifts, staff, settings, Academy, chat and manual cash movements are
    retained. Outbox envelopes validate their durable generation in Workday;
    unlike an HTTP header it cannot be refreshed onto an old queued operation.
    Guest capabilities need the barrier but never a staff generation header.
    """
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    path = request.path
    if path.startswith(("/feedback/", "/g/")):
        return True
    if not path.startswith(PREFIX + "/"):
        return False
    path = path[len(PREFIX):]
    return bool(
        path.startswith(("/attendance/", "/bookings/", "/sales/", "/photos/",
                         "/receipts/", "/sale-drafts/", "/payroll/", "/operations/", "/work-breaks", "/hotel-tariffs", "/bank-payments"))
        or re.match(r"^/(?:delivery|galleries)/[0-9]+(?:/|$)", path)
        or path == "/events/scan"
        or re.fullmatch(r"/events/[0-9]+/acknowledge", path)
    )


def operation_header_required(request):
    return (request.path.startswith(PREFIX + "/")
            and not request.path.startswith(PREFIX + "/operations/"))


def operation_lease_required(request):
    # Public album views and downloads increment reset-owned counters and may
    # finish after a long storage read. Drain those writers before archiving.
    return (operational_mutation(request)
            or (request.method == "GET" and request.path.startswith("/g/")))


ACTIONS = {
    "row.insert": "Созданы данные", "row.update": "Изменены данные", "row.delete": "Удалены данные",
    "work_call_history": "Изменён статус звонка",
    "sale_created": "Зарегистрировал продажу",
    "booking_created": "Создал запись на съёмку",
    "booking_assigned": "Назначил фотографа",
    "booking_confirmed": "Подтвердил съёмку",
    "booking_rejected": "Отклонил съёмку",
    "booking_rescheduled": "Перенёс съёмку",
    "shoot_completed": "Завершил съёмку",
    "shooting_completed": "Завершил съёмку",
    "shift_started": "Начал смену",
    "shift_finished": "Завершил смену",
    "photos_ready_for_sale": "Подготовил фотографии к продаже",
    "receipt_approved": "Подтвердил чек",
    "receipt_rejected": "Отклонил чек",
    "premium_added": "Начислил премию",
    "employee_created": "Добавил сотрудника",
    "employee_fired": "Отключил доступ сотруднику",
    "employee_restored": "Восстановил доступ сотруднику",
    "employee_role_removed": "Снял роль сотруднику",
    "academy_lesson_completed": "Завершил урок Академии",
    "academy_ai_plan": "AI подготовил персональное учебное задание",
    "academy_location_created": "Добавил учебную локацию",
    "miniapp_theme_changed": "Изменил оформление приложения",
    "miniapp_opened": "Открыл приложение",
    "miniapp_owner_access_changed": "Изменил права владельца у администратора",
    "miniapp_screen_capture_changed": "Изменил разрешение на скриншоты",
    "miniapp_shift_created": "Назначил смену",
    "miniapp_shift_cancelled": "Отменил запланированную смену",
    "miniapp_password_changed": "Настроил личный вход",
}


def cents(value) -> int:
    return int((Decimal(str(value or 0)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def as_utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def first_role(roles):
    return next((r for r in ROLE_ORDER if r in roles), "PHOTOGRAPHER")


class MiniApp:
    def __init__(self, engine, bot, lessons, *, blocks=None, tz_name="Europe/Moscow", static_dir=None):
        self.engine, self.bot = engine, bot
        self.tz = ZoneInfo(tz_name)
        self.lessons = [{"slug": l.slug, "title": l.title, "body": l.body,
                         "duration": "Короткий урок", "day": getattr(l, "day", index),
                         "block": getattr(l, "block", 1)}
                        for index, l in enumerate(lessons, start=1)]
        from .services.academy_materials import LESSON_MATERIALS
        for lesson in self.lessons:
            material = LESSON_MATERIALS.get(lesson["slug"])
            if material:
                lesson["materials"] = material
                lesson["duration"] = f"{material['estimatedMinutes']} мин + практика"
        self.blocks = [{"number": b.number, "title": b.title,
                        "practiceCategories": list(b.practice_categories),
                        "practiceTitle": b.practice_title} for b in (blocks or [])]
        if self.lessons and not self.blocks:
            self.blocks = [{"number": 1, "title": "Учебный блок",
                            "practiceCategories": [], "practiceTitle": ""}]
        self.static_dir = Path(static_dir or Path(__file__).parent / "webapp")

    def academy_state(self, completed, accepted_categories):
        unlocked = 1
        for block in self.blocks[:-1]:
            lesson_slugs = {l["slug"] for l in self.lessons if l["block"] == block["number"]}
            practice_done = not block["practiceCategories"] or bool(
                set(block["practiceCategories"]) & accepted_categories
            )
            if not lesson_slugs.issubset(completed) or not practice_done:
                break
            unlocked = block["number"] + 1
        return min(unlocked, len(self.blocks) or 1)

    def today(self):
        return datetime.now(self.tz).date()

    async def rows(self, conn, sql, **params):
        return list((await conn.execute(text(sql), params)).mappings())

    async def actor(self, request):
        # Prefer Telegram's signed initData. Some Telegram Android WebViews have
        # been observed opening the app without exposing initData at all; for
        # owners only, allow a short-lived server-signed token issued by the bot.
        init_data = request.headers.get("X-Telegram-Init-Data", "")
        launch_token = request.headers.get("X-PhotoBoss-Owner-Launch", "")
        owner_fallback = False
        if init_data:
            telegram_id = validate_init_data(
                init_data,
                self.bot.token,
                max_age=MINIAPP_SESSION_MAX_AGE,
            )
        elif launch_token:
            telegram_id = validate_owner_launch_token(launch_token, self.bot.token)
            owner_fallback = True
        else:
            telegram_id = await miniapp_sessions.authenticate(self.engine, request)
        request["miniapp_telegram_id"] = telegram_id
        async with self.engine.connect() as conn:
            people = await self.rows(conn, "SELECT id,tg_id,name,active FROM users WHERE tg_id=:tg", tg=telegram_id)
            if not people or not people[0]["active"]:
                raise AccessError("Аккаунт сотрудника не активен. Обратитесь к владельцу.")
            actor = dict(people[0])
            roles = await self.rows(conn, "SELECT role FROM user_roles WHERE user_id=:uid", uid=actor["id"])
            actor["roles"] = [r for r in ROLE_ORDER if any(x["role"] == r for x in roles)]
            if not STAFF_ROLES & set(actor["roles"]):
                raise AccessError("Владелец ещё не назначил вам рабочую роль.")
            if owner_fallback and "OWNER" not in actor["roles"]:
                raise AccessError("Резервный вход разрешён только владельцу.", 403)
            actor["permissions"] = role_permissions(actor["roles"])
            actor["ownerFallback"] = owner_fallback
            return actor

    @web.middleware
    async def middleware(self, request, handler):
        guarded = operation_lease_required(request)
        if not (request.path.startswith(PREFIX) or request.path.startswith("/app/") or guarded):
            return await handler(request)
        from .audit_context import actor_id
        actor_token = actor_id.set(None)
        try:
            if request.path.startswith(PREFIX):
                if any(k in request.query for k in ("role", "roles", "userId", "user_id", "employee_id", "scope")):
                    raise AccessError("Пользователь и роль определяются сервером.", 400)
                request["miniapp_actor"] = await self.actor(request)
                actor_id.set(request["miniapp_actor"]["id"])
            if guarded:
                async with operation_lease(self.engine) as conn:
                    if operation_header_required(request):
                        await require_generation(conn, request.headers.get(OPERATIONS_GENERATION_HEADER, ""))
                    response = await handler(request)
            else:
                response = await handler(request)
            if request.path.startswith(PREFIX) and response.status < 400:
                try:
                    await miniapp_sessions.refresh(self.engine, request, response)
                except Exception:
                    # The handler may have committed a message or another operation.
                    # Extending the session must not turn that acknowledgement into an error.
                    logger.warning("Mini App session refresh failed: %s", request.path, exc_info=True)
        except AccessError as exc:
            body = {"error": str(exc)}
            if isinstance(exc, OperationsResetError):
                body.update(code="operations_reset", operationsGeneration=exc.generation)
            if exc.status == 403 and request.get("miniapp_telegram_id"):
                # Signed account identity only; never disclose a different user or roles.
                body["telegramId"] = request["miniapp_telegram_id"]
            response = web.json_response(body, status=exc.status)
        except web.HTTPException as exc:
            response = web.json_response({"error": "Недопустимый запрос."}, status=exc.status)
        except YandexDiskError as exc:
            # The provider may be down while the application and database are healthy.
            # Preserve the original outbox row and do not expose provider URLs or tokens.
            # Keep only the numeric provider status; never log signed URLs or tokens.
            import re
            code = re.search(r'HTTP (\d{3})', str(exc))
            logger.warning("Mini App storage temporarily unavailable: %s providerStatus=%s",
                           request.path, code.group(1) if code else 'unconfirmed')
            if 'HTTP 404' in str(exc):
                response = web.json_response({'error': 'Файл не найден на подключённом Яндекс.Диске. Он мог остаться на прежнем Диске. Загрузите исходную фотографию повторно в эту же съёмку.'}, status=404)
            else:
                response = web.json_response(
                    {"error": "Яндекс.Диск временно недоступен. Повторите действие. Не удаляйте исходные файлы до подтверждения сохранения."},
                status=503,
            )
        except Exception:
            logger.exception("Mini App handler failed: %s", request.path)
            response = web.json_response({"error": "Сервис временно недоступен. Повторите попытку."}, status=503)
        finally:
            actor_id.reset(actor_token)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/app/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' https://telegram.org; "
                "style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
                "object-src 'none'; base-uri 'self'; form-action 'self'; "
                "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
            )
        return response

    async def body(self, request, *, max_bytes=8192):
        if request.content_length and request.content_length > max_bytes:
            raise AccessError("Слишком большой запрос.", 413)
        try:
            raw = bytearray()
            while not request.content.at_eof():
                raw.extend(await request.content.read(max_bytes + 1 - len(raw)))
                if len(raw) > max_bytes:
                    raise AccessError("Слишком большой запрос.", 413)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise TypeError("JSON body must be an object")
            return value
        except (ConnectionResetError, TimeoutError) as exc:
            raise AccessError('Соединение прервано. Повторите отправку при восстановлении сети.', 503) from exc
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            if isinstance(exc, AccessError):
                raise
            raise AccessError("Ожидается корректный JSON-объект.", 400) from exc

    def day(self, value):
        try:
            return date.fromisoformat(value)
        except (ValueError, TypeError) as exc:
            raise AccessError("Некорректная дата.", 400) from exc

    async def ai_diagnostics(self, request):
        require_owner(request['miniapp_actor']['roles'])
        from .config import config
        from .services.ai_diagnostics import public_diagnostics
        return web.json_response({'provider': 'openai', 'configured': bool(config.openai_api_key),
            'models': {'academy': config.academy_analysis_model, 'receipts': config.receipt_analysis_model},
            'checks': public_diagnostics(), 'scope': 'since_server_start',
            'note': 'Наличие ключа не доказывает успешный ответ API. Проверка использует только тестовый текст.'})

    async def ai_probe(self, request):
        require_owner(request['miniapp_actor']['roles'])
        if await self.body(request) != {}:
            raise AccessError('Проверка использует только встроенное тестовое задание.', 400)
        import time
        now = time.monotonic()
        if now - getattr(self, '_ai_probe_at', -1000) < 60:
            raise AccessError('Проверка уже выполнялась. Повтор доступен через минуту.', 429)
        self._ai_probe_at = now
        from .services.development_ai import ROLEPLAY_SCHEMA, structured
        result = await structured('Это проверка доступности учебного ИИ. Ответь reply="Готов", score=null, errors=[], recommendations=[].',
            [{'type': 'input_text', 'text': 'Проверка соединения, без данных сотрудников и гостей.'}],
            ROLEPLAY_SCHEMA, 'connection_probe', limit=300)
        return web.json_response({'ok': result['status'] == 'completed', 'status': result['status'],
            'errorCode': result.get('errorCode'), 'message': result.get('message', 'OpenAI ответил корректно.')})

    async def me(self, request):
        actor = request["miniapp_actor"]
        async with self.engine.connect() as conn:
            theme = await self.rows(conn, "SELECT value FROM settings WHERE key=:key", key=f"miniapp:theme:{actor['id']}")
            capture = await self.rows(conn, "SELECT value FROM settings WHERE key=:key", key=f"miniapp:screen_capture:{actor['id']}")
            password = await self.rows(conn, "SELECT value FROM settings WHERE key=:key", key=f"miniapp:password:{actor['id']}")
            generation = await current_generation(conn)
            sales_epoch = await self.rows(conn, 'SELECT value FROM settings WHERE key=:key', key='sales:generation')
            sales_generation = sales_epoch[0]['value'] if sales_epoch else ''
        capture_allowed = (
            True
            if "OWNER" in actor["roles"]
            else bool(capture and capture[0]["value"] == "1")
        )
        return web.json_response({"user": {"id": actor["id"], "telegramId": actor["tg_id"],
            "name": actor["name"], "roles": actor["roles"],
            "theme": theme[0]["value"] if theme and theme[0]["value"] in THEMES else ("premium" if "OWNER" in actor["roles"] else "light")},
            "permissions": actor["permissions"], "screenCaptureAllowed": capture_allowed,
            "passwordConfigured": bool(password),
            "passwordLogin": json.loads(password[0]['value'])['login'] if password else '',
            "operationsGeneration": generation,
            "salesGeneration": sales_generation,
            "today": self.today().isoformat(),
            "timezone": str(self.tz), "mode": "live"})

    async def session_open(self, request):
        if await self.body(request):
            raise AccessError("Лишние параметры входа.", 400)
        actor = request["miniapp_actor"]
        credential = (
            request.headers.get("X-Telegram-Init-Data", "")
            or request.headers.get("X-PhotoBoss-Owner-Launch", "")
            or miniapp_sessions.cookie(request)
        )
        digest = hashlib.sha256(credential.encode()).hexdigest()
        key = f"miniapp:opened:{actor['id']}"
        last_login_key = f"miniapp:last_login:{actor['id']}"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with self.engine.begin() as conn:
            await self.rows(conn, "SELECT id FROM users WHERE id=:uid FOR UPDATE", uid=actor["id"])
            await conn.execute(text("""INSERT INTO settings (key,value) VALUES (:key,:value)
                ON CONFLICT (key) DO UPDATE SET value=excluded.value"""),
                {"key": last_login_key, "value": now.isoformat()})
            old = await self.rows(conn, "SELECT value FROM settings WHERE key=:key", key=key)
            if not old or old[0]["value"] != digest:
                await conn.execute(text("""INSERT INTO settings (key,value) VALUES (:key,:value)
                    ON CONFLICT (key) DO UPDATE SET value=excluded.value"""), {"key": key, "value": digest})
                await self.audit_write(conn, actor, "miniapp_opened", "user", actor["id"],
                    "Подтверждён вход через Telegram. Это не журнал каждого просмотра экрана.")
        return web.json_response({"ok": True})

    async def audit_write(self, conn, actor, action, entity, eid, details):
        await conn.execute(text("""INSERT INTO audit_logs
            (user_id,action,entity,entity_id,details,created_at)
            VALUES (:uid,:action,:entity,:eid,:details,:created)"""),
            {"uid": actor["id"], "action": action, "entity": entity, "eid": eid,
             "details": details, "created": datetime.now(timezone.utc).replace(tzinfo=None)})

    async def preferences(self, request):
        body = await self.body(request)
        if set(body) != {"theme"} or not isinstance(body["theme"], str) or body["theme"] not in THEMES:
            raise AccessError("Выберите одну из трёх тем.", 400)
        actor = request["miniapp_actor"]
        async with self.engine.begin() as conn:
            key = f"miniapp:theme:{actor['id']}"
            await self.rows(conn, "SELECT id FROM users WHERE id=:uid FOR UPDATE", uid=actor["id"])
            old = await self.rows(conn, "SELECT value FROM settings WHERE key=:key", key=key)
            previous = old[0]["value"] if old else ("premium" if "OWNER" in actor["roles"] else "light")
            await conn.execute(text("""INSERT INTO settings (key,value) VALUES (:key,:value)
                ON CONFLICT (key) DO UPDATE SET value=excluded.value"""), {"key": key, "value": body["theme"]})
            await self.audit_write(conn, actor, "miniapp_theme_changed", "user", actor["id"], f"Оформление: {previous} → {body['theme']}")
        return web.json_response({"theme": body["theme"]})

    async def bookings_data(self, conn, actor, day, *, search='', trash=False):
        import re

        from .booking_management import version
        clause = f"AND ({booking_scope_sql(actor['id'], actor['roles'])})"
        params = {'uid': actor['id'], 'day': day}
        where = '1=1' if search else 'b.shoot_date=:day'
        marker = "EXISTS (SELECT 1 FROM settings st WHERE st.key='booking:trash:' || CAST(b.id AS VARCHAR))"
        where += ' AND ' + (marker if trash else 'NOT ' + marker)
        if search:
            search = re.sub(r'\b(\d{2})\.(\d{2})\.(\d{4})\b', r'\3-\2-\1', search)
            terms = [re.sub(r'\D', '', search)] if re.fullmatch(r'[+\d\s()\-]+', search) and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', search) else search.casefold().split()
            fields = ['c.name', 'h.name', 'b.room', 'p.name', 'm.name', 'c.phone',
                      'CAST(b.id AS VARCHAR)', 'CAST(b.shoot_date AS VARCHAR)', 'CAST(b.shoot_time AS VARCHAR)',
                      "replace(replace(replace(replace(replace(c.phone,' ',''),'+',''),'-',''),'(',''),')','')"]
            for i, term in enumerate(terms[:8]):
                key = f'q{i}'
                params[key] = '%' + term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
                where += ' AND (' + ' OR '.join(f"LOWER(COALESCE({field},'')) LIKE :{key} ESCAPE '\\'" for field in fields) + ')'
        rows = await self.rows(conn, f"""SELECT b.id,b.room,b.guest_count,b.shoot_date,b.shoot_time,b.status,
            b.photographer_id,b.manager_id,b.hotel_id,c.name AS client,c.phone,h.name AS hotel,
            sh.id AS shooting_id,sh.status AS shooting_status,
            p.name AS photographer,m.name AS manager,pk.name AS package,
            (SELECT COUNT(*) FROM photos ph JOIN shootings sh ON sh.id=ph.shooting_id
             WHERE sh.booking_id=b.id) AS frames
            FROM bookings b LEFT JOIN shootings sh ON sh.booking_id=b.id
            JOIN clients c ON c.id=b.client_id
            JOIN hotels h ON h.id=b.hotel_id LEFT JOIN users p ON p.id=b.photographer_id
            LEFT JOIN users m ON m.id=b.manager_id
            LEFT JOIN packages pk ON pk.id=b.package_id
            WHERE {where} {clause} ORDER BY b.shoot_date DESC,b.shoot_time,b.id LIMIT 201""", **params)
        items = [{"id": r["id"], "client": r["client"], "phone": r['phone'], "guestCount": r['guest_count'], "date": str(r["shoot_date"]),
                 "time": str(r["shoot_time"])[:5], "room": r["room"], "status": r["status"],
                 "photographerId": r["photographer_id"], "photographer": r["photographer"],
                 "shootingId": r["shooting_id"], "shootingStatus": r["shooting_status"],
                 "managerId": r["manager_id"], "manager": r['manager'], "hotelId": r["hotel_id"], "hotel": r["hotel"],
                 "trashed": trash,
                 "canEdit": bool({'OWNER', 'ADMIN'} & set(actor['roles']) or ('MANAGER' in actor['roles'] and r['manager_id'] == actor['id'])),
                 "type": r["package"] or "Съёмка", "frames": r["frames"]} for r in rows]
        for item in items:
            item['revision'] = version(item)
        return items

    async def bookings(self, request):
        day = self.day(request.query.get("day", str(self.today())))
        actor = request["miniapp_actor"]
        search = request.query.get('q', '').strip()
        if len(search) > 160 or len(search.split()) > 8:
            raise AccessError('Поисковый запрос: до 160 символов и 8 слов.', 400)
        trash = request.query.get('trash') == '1'
        async with self.engine.connect() as conn:
            items = await self.bookings_data(conn, actor, day, search=search, trash=trash)
            # Only supervisors receive the complete hotel list, including hotels
            # with no bookings on the selected date. Staff still see only their
            # assigned bookings through bookings_data.
            hotels = ([dict(row) for row in await self.rows(conn,
                "SELECT id,name FROM hotels WHERE active=TRUE " +
                ("" if "OWNER" in actor["roles"] else
                 "AND id IN (SELECT hotel_id FROM hotel_employees WHERE user_id=:uid) ") +
                "ORDER BY name,id", uid=actor["id"])]
                if {"OWNER", "ADMIN"} & set(actor["roles"]) else [])
        return web.json_response({"items": items[:200], "hotels": hotels, 'truncated': len(items) > 200, 'allDates': bool(search)})

    async def booking_photographers(self, request):
        actor = request["miniapp_actor"]
        if not {"OWNER", "ADMIN"} & set(actor["roles"]):
            raise AccessError("Назначение доступно администратору и владельцу.")
        async with self.engine.connect() as conn:
            people = await self.rows(conn, """SELECT u.id,u.name FROM users u
                WHERE u.active=TRUE AND EXISTS
                (SELECT 1 FROM user_roles r WHERE r.user_id=u.id AND r.role='PHOTOGRAPHER')
                ORDER BY u.name,u.id""")
        return web.json_response({"items": [dict(p) for p in people]})

    async def assign_booking_photographer(self, request):
        actor = request["miniapp_actor"]
        if not {"OWNER", "ADMIN"} & set(actor["roles"]):
            raise AccessError("Назначение доступно администратору и владельцу.")
        body = await self.body(request)
        uid = body.get("photographerId")
        previous = body.get("previousPhotographerId")
        if (set(body) != {"photographerId", "previousPhotographerId"}
                or type(uid) is not int or uid <= 0
                or previous is not None and (type(previous) is not int or previous <= 0)):
            raise AccessError("Выберите фотографа и обновите карточку съёмки.", 400)
        bid = int(request.match_info["id"])
        async with self.engine.begin() as conn:
            rows = await self.rows(conn, "SELECT * FROM bookings WHERE id=:bid FOR UPDATE", bid=bid)
            if not rows:
                raise AccessError("Съёмка не найдена.", 404)
            booking = rows[0]
            scope = booking_scope_sql(actor['id'], actor['roles'])
            allowed = await self.rows(conn,
                f"SELECT b.id FROM bookings b WHERE b.id=:bid AND ({scope})",
                bid=bid, uid=actor['id'])
            if not allowed:
                raise AccessError("Нет доступа к съёмке этого отеля.")
            if booking["photographer_id"] == uid:
                return web.json_response({"ok": True, "photographerId": uid})
            if booking["photographer_id"] != previous:
                raise AccessError("Фотограф уже изменён другим сотрудником. Обновите карточку.", 409)
            if booking["status"] not in {"NEW", "CONFIRMED", "PENDING_CONFIRMATION", "RESCHEDULED", "ASSIGNED"}:
                raise AccessError("Менять фотографа можно до начала съёмки.", 409)
            people = await self.rows(conn, """SELECT u.id FROM users u WHERE u.id=:uid AND u.active=TRUE
                AND EXISTS (SELECT 1 FROM user_roles r WHERE r.user_id=u.id AND r.role='PHOTOGRAPHER')
                FOR UPDATE""", uid=uid)
            if not people:
                raise AccessError("Нужен активный сотрудник с ролью фотографа.", 400)
            shoots = await self.rows(conn, "SELECT * FROM shootings WHERE booking_id=:bid FOR UPDATE", bid=bid)
            if shoots and (shoots[0]["started_at"] or shoots[0]["completed_at"]
                    or shoots[0]["status"] not in {"NEW", "PENDING_CONFIRMATION", "CONFIRMED", "ASSIGNED"}):
                raise AccessError("Съёмка уже начата. Назначение менять нельзя.", 409)
            await conn.execute(text("UPDATE bookings SET photographer_id=:uid,status='ASSIGNED' WHERE id=:bid"), {"uid": uid, "bid": bid})
            if shoots:
                await conn.execute(text("UPDATE shootings SET status='ASSIGNED',accepted_at=NULL WHERE booking_id=:bid"), {"bid": bid})
            else:
                await conn.execute(text("INSERT INTO shootings (booking_id,status) VALUES (:bid,'ASSIGNED')"), {"bid": bid})
            await self.audit_write(conn, actor, "miniapp_photographer_assigned", "booking", bid,
                                   f"Фотограф: {previous} → {uid}")
            from .services.assignment_notifications import queue_assignment
            await queue_assignment(conn, bid, uid)
        return web.json_response({"ok": True, "photographerId": uid})

    async def assignment_notifications(self, request):
        actor = request['miniapp_actor']
        items = []
        if 'PHOTOGRAPHER' in actor['roles']:
            from .services.assignment_notifications import assignment_items
            async with self.engine.connect() as conn:
                items = await assignment_items(conn, actor['id'], self.today())
        return web.json_response({'items': items}, headers={'Cache-Control': 'no-store'})

    async def shooting_action(self, request):
        """Run exactly the same state transitions used by the Telegram photographer bot."""
        actor = request["miniapp_actor"]
        if "PHOTOGRAPHER" not in actor["roles"]:
            raise AccessError("Действие доступно только фотографу.", 403)
        if await self.body(request):
            raise AccessError("Лишние параметры действия.", 400)
        action = request.match_info["action"]
        transitions = {
            "pickup": ("ASSIGNED", "PICKED_UP", "accepted_at"),
            "start": ("PICKED_UP", "SHOOTING", "started_at"),
            "finish": ("SHOOTING", "SHOT", "completed_at"),
            "ready": ("SHOT", "READY_FOR_SALE", "ready_for_sale_at"),
        }
        if action not in transitions:
            raise AccessError("Неизвестное действие съёмки.", 400)
        bid = int(request.match_info["id"])
        expected, target, timestamp = transitions[action]
        async with self.engine.begin() as conn:
            bookings = await self.rows(conn,
                "SELECT photographer_id,status FROM bookings WHERE id=:bid FOR UPDATE", bid=bid)
            shoots = await self.rows(conn,
                "SELECT id,status FROM shootings WHERE booking_id=:bid FOR UPDATE", bid=bid)
            if not shoots:
                raise AccessError("Съёмка не найдена.", 404)
            shoot = shoots[0]
            if not bookings or bookings[0]["photographer_id"] != actor["id"]:
                raise AccessError("Это не ваша съёмка.", 403)
            if bookings[0]["status"] in {"PENDING_CONFIRMATION", "REJECTED", "CANCELLED"}:
                raise AccessError("Съёмка ещё не подтверждена или отменена.", 409)
            if shoot["status"] != expected:
                raise AccessError("Этот шаг уже выполнен или предыдущий ещё не завершён. Обновите карточку.", 409)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            await conn.execute(text(
                f"UPDATE shootings SET status=:status,{timestamp}=:now WHERE id=:sid"),
                {"status": target, "now": now, "sid": shoot["id"]})
            await conn.execute(text("UPDATE bookings SET status=:status WHERE id=:bid"),
                {"status": target, "bid": bid})
            await self.audit_write(conn, actor, f"shooting_{action}", "shooting",
                                   shoot["id"], f"Mini App: {expected} → {target}")
        if action == "ready":
            # Notify using the same template as the Telegram bot, after commit.
            try:
                from .services.bookings import send_manager_ready_message
                async with self.engine.connect() as conn:
                    managers = await self.rows(conn, """SELECT u.tg_id FROM bookings b
                        JOIN users u ON u.id=b.manager_id
                        WHERE b.id=:bid AND u.active=TRUE""", bid=bid)
                if managers:
                    await send_manager_ready_message(self.bot, managers[0]["tg_id"], bid, engine=self.engine)
            except Exception:
                logger.warning("Could not notify manager for booking %s", bid, exc_info=True)
        return web.json_response({"ok": True, "bookingId": bid,
                                  "shootingId": shoot["id"], "status": target})

    async def finance_data(self, conn, actor, start, end, period):
        if "ADMIN" in actor["roles"] and "OWNER" not in actor["roles"] and (start != self.today() or end != self.today()):
            raise AccessError("Общая касса за прошлые периоды доступна только владельцу.")
        own = actor["permissions"]["financeScope"] == "self"
        hotel_only = "ADMIN" in actor["roles"] and "OWNER" not in actor["roles"]
        hotel_scope = booking_scope_sql(actor["id"], actor["roles"])
        lower, upper = utc_bounds(start, end, self.tz)
        args = {"lo": lower, "hi": upper, "uid": actor["id"]}
        sales = await self.rows(conn, """SELECT s.*, u.name AS employee, c.name AS client
            FROM sales s JOIN users u ON u.id=s.credited_user_id
            LEFT JOIN bookings b ON b.id=s.booking_id LEFT JOIN clients c ON c.id=b.client_id
            WHERE s.created_at>=:lo AND s.created_at<:hi """ +
            ("AND s.credited_user_id=:uid " if own else f"AND ({hotel_scope}) " if hotel_only else "") + "ORDER BY s.created_at DESC", **args)
        # Unscoped manual premiums/fines must not expose another hotel's money.
        # Only an explicit sale -> payroll FK proves an administrator's scope;
        # do not infer it from employee membership or free-text notes.
        payroll_scope = "AND p.user_id=:uid" if own else ""
        if hotel_only:
            payroll_scope = ("AND EXISTS (SELECT 1 FROM sales ps JOIN bookings b ON b.id=ps.booking_id "
                             "WHERE ps.manager_payroll_entry_id=p.id AND p.user_id=b.manager_id "
                             f"AND ({hotel_scope}))")
        payroll = await self.rows(conn, "SELECT p.*,u.name FROM payroll_entries p JOIN users u ON u.id=p.user_id WHERE p.created_at>=:lo AND p.created_at<:hi " + payroll_scope, **args)
        employees = {}
        for sale in sales:
            uid = sale["credited_user_id"]
            e = employees.setdefault(uid, {"id": uid, "name": sale["employee"], "role": sale["commission_role"], "sales": 0, "commission": 0, "adjustments": 0, "earned": 0})
            e["sales"] += cents(sale["amount"])
            e["commission"] += cents(sale["commission"])
        for row in payroll:
            uid = row["user_id"]
            e = employees.setdefault(uid, {"id": uid, "name": row["name"], "role": "", "sales": 0, "commission": 0, "adjustments": 0, "earned": 0})
            e["adjustments"] += cents(row["amount"])
        for item in employees.values():
            item["earned"] = item["commission"] + item["adjustments"]
        cash = None
        if not own:
            receipts = await self.rows(conn, "SELECT r.verified_amount FROM receipts r " +
                ("JOIN bookings b ON b.id=r.booking_id " if hotel_only else "") +
                "WHERE r.status='APPROVED' AND r.reviewed_at>=:lo AND r.reviewed_at<:hi " +
                (f"AND ({hotel_scope})" if hotel_only else ""), **args)
            cash = sum(cents(r["verified_amount"]) for r in receipts)
        return {"period": period, "from": str(start), "to": str(end), "scope": "self" if own else "hotels" if hotel_only else "company",
                "unscopedAdjustmentsExcluded": hotel_only,
                "sales": sum(cents(s["amount"]) for s in sales), "cashReceived": cash,
                "payroll": sum(e["earned"] for e in employees.values()),
                "employees": sorted(employees.values(), key=lambda x: (-x["sales"], x["name"])),
                "salesRows": [{"id": s["id"], "bookingId": s["booking_id"], "client": s["client"],
                    "employee": s["employee"], "date": str(as_utc(s["created_at"]).astimezone(self.tz).date()),
                    "amount": cents(s["amount"]), "status": s["payment_status"]} for s in sales[:100]],
                "salesCount": len(sales), "salesListLimited": len(sales) > 100,
                "note": ("Только назначенные вам отели. Ручные премии и удержания без надёжной привязки к отелю здесь не показаны; владелец и сам сотрудник видят их в своих отчётах. " if hotel_only else "") + "Касса — подтверждённые чеки по дате подтверждения. Начислено — комиссии и записи премий/удержаний существующего бота. Это не выплаченная зарплата и не чистая прибыль. Продажи показаны до учёта оплаты."}

    async def finance(self, request):
        actor = request["miniapp_actor"]
        period = request.query.get("period", "today")
        start, end = financial_period(actor["roles"], period, self.today(), start=request.query.get("from"), end=request.query.get("to"))
        async with self.engine.connect() as conn:
            result = await self.finance_data(conn, actor, start, end, period)
        return web.json_response(result)

    async def shifts_data(self, conn, actor, start, end, *, team=False):
        lower, upper = utc_bounds(start, end, self.tz)
        own = actor["permissions"]["financeScope"] == "self"
        clause = ""
        if 'ADMIN' in actor['roles'] and 'OWNER' not in actor['roles']:
            clause = 'AND s.hotel_id IN (SELECT hotel_id FROM hotel_employees WHERE user_id=:uid)'
        elif own and not team:
            clause = "AND s.user_id=:uid"
        elif own and team:
            clause = """AND (s.user_id=:uid OR s.hotel_id IN
               (SELECT hotel_id FROM hotel_employees WHERE user_id=:uid)
               OR s.hotel_id IN (SELECT hotel_id FROM shifts WHERE user_id=:uid
               AND start_at<:hi AND end_at>:lo AND status<>'CANCELLED'))"""
        rows = await self.rows(conn, f"""SELECT s.*,u.name,h.name AS hotel,
            CASE WHEN EXISTS(SELECT 1 FROM user_roles r WHERE r.user_id=u.id AND r.role='PHOTOGRAPHER')
            THEN 'PHOTOGRAPHER' ELSE 'MANAGER' END AS role
            FROM shifts s JOIN users u ON u.id=s.user_id JOIN hotels h ON h.id=s.hotel_id
            WHERE s.start_at<:hi AND s.end_at>:lo {clause} ORDER BY s.start_at,u.name""",
            uid=actor["id"], lo=lower, hi=upper)
        items = []
        for r in rows:
            st, en = as_utc(r["start_at"]).astimezone(self.tz), as_utc(r["end_at"]).astimezone(self.tz)
            attendance = "PLANNED"
            checks = await self.rows(conn, """SELECT started_at FROM shift_check_ins
                WHERE user_id=:uid AND shift_date=:day AND started_at IS NOT NULL""",
                uid=r["user_id"], day=st.date())
            outs = await self.rows(conn, """SELECT ended_at FROM shift_check_outs
                WHERE user_id=:uid AND shift_date=:day AND ended_at IS NOT NULL""",
                uid=r["user_id"], day=st.date())
            if checks:
                attendance = "ENDED" if outs else ("STARTED" if st.date() == self.today() else "MISSING_CHECKOUT")
            breaks = await self.rows(conn, '''SELECT started_at,ended_at FROM work_breaks
                WHERE user_id=:uid AND hotel_id=:hid AND shift_date=:day ORDER BY started_at''',
                uid=r['user_id'], hid=r['hotel_id'], day=st.date())
            if attendance == 'STARTED' and any(not item['ended_at'] for item in breaks):
                attendance = 'ON_BREAK'
            items.append({"id": r["id"], "userId": r["user_id"], "name": r["name"], "role": r["role"],
                "hotelId": r["hotel_id"], "hotel": r["hotel"], "date": str(st.date()), "start": st.strftime("%H:%M"),
                "end": en.strftime("%H:%M"), "status": r["status"], "attendance": attendance,
                'breaks': [{'start': as_utc(item['started_at']).astimezone(self.tz).strftime('%H:%M'),
                            'end': as_utc(item['ended_at']).astimezone(self.tz).strftime('%H:%M') if item['ended_at'] else None}
                           for item in breaks]})
        return items

    async def dashboard(self, request):
        actor, today = request["miniapp_actor"], self.today()
        if request.query:
            raise AccessError("Главная показывает только текущий день.", 400)
        async with self.engine.connect() as conn:
            result = {"today": str(today), "team": [s for s in await self.shifts_data(conn, actor, today, today, team=True) if s["status"] != "CANCELLED"],
                "bookings": await self.bookings_data(conn, actor, today),
                "finance": await self.finance_data(conn, actor, today, today, "today"),
                "updatedAt": datetime.now(timezone.utc).isoformat()}
            if 'PHOTOGRAPHER' in actor['roles']:
                from .services.assignment_notifications import assignment_items
                result['assignments'] = await assignment_items(conn, actor['id'], today)
        return web.json_response(result)

    async def schedule(self, request):
        actor = request["miniapp_actor"]
        start = self.day(request.query.get("from", str(self.today())))
        end = self.day(request.query.get("to", str(start)))
        if not 0 <= (end-start).days <= 31:
            raise AccessError("Период графика должен быть не длиннее 31 дня.", 400)
        async with self.engine.connect() as conn:
            items = await self.shifts_data(conn, actor, start, end)
            employees, hotels, unavailable = [], [], []
            if actor["permissions"]["manageSchedule"]:
                # Keep the complete roster available to the editor. In particular,
                # hotel assignment and existing shifts must not hide a new employee.
                roster = {}
                for row in await self.rows(conn, """SELECT u.id,u.name,u.active,r.role FROM users u
                    LEFT JOIN user_roles r ON r.user_id=u.id ORDER BY u.name,u.id,r.role"""):
                    person = roster.setdefault(row['id'], {'id': row['id'], 'name': row['name'],
                        'active': bool(row['active']), 'roles': []})
                    if row['role'] and row['role'] not in person['roles']:
                        person['roles'].append(row['role'])
                for person in roster.values():
                    eligible = bool({'MANAGER', 'PHOTOGRAPHER'} & set(person['roles']))
                    if person['active'] and eligible:
                        employees.append({k: person[k] for k in ('id', 'name', 'roles')})
                    else:
                        unavailable.append({k: person[k] for k in ('id', 'name', 'roles')} |
                            {'reason': 'inactive' if not person['active'] else 'work_role_required'})
                hotels = [dict(r) for r in await self.rows(conn,
                    "SELECT id,name FROM hotels WHERE active=TRUE " +
                    ("" if 'OWNER' in actor['roles'] else
                     "AND id IN (SELECT hotel_id FROM hotel_employees WHERE user_id=:uid) ") +
                    "ORDER BY name", uid=actor['id'])]
        return web.json_response({"items": items, "employees": employees, "hotels": hotels,
                                  "unavailableEmployees": unavailable})

    async def create_shift(self, request):
        payload = await self.body(request)
        ids = await self.save_shifts(request, [payload])
        return web.json_response({"id": ids[0]}, status=201)

    async def create_shift_batch(self, request):
        payload = await self.body(request)
        if set(payload) != {'userId', 'slots'} or not isinstance(payload['slots'], list) or not 1 <= len(payload['slots']) <= 62:
            raise AccessError('Выберите сотрудника и от 1 до 62 смен.', 400)
        shifts = []
        for slot in payload['slots']:
            if not isinstance(slot, dict) or set(slot) != {'hotelId', 'date', 'start', 'end'}:
                raise AccessError('Проверьте отель, дату и время каждой смены.', 400)
            shifts.append({'userId': payload['userId'], **slot})
        ids = await self.save_shifts(request, shifts)
        return web.json_response({'ids': ids}, status=201)

    async def create_team_schedule(self, request):
        require_schedule_editor(request["miniapp_actor"]["roles"])
        payload = await self.body(request, max_bytes=131072)
        if set(payload) != {'shifts'} or not isinstance(payload['shifts'], list) or not 1 <= len(payload['shifts']) <= 620:
            raise AccessError('Добавьте от 1 до 620 смен в общий график.', 400)
        if not all(isinstance(shift, dict) for shift in payload['shifts']):
            raise AccessError('Проверьте данные каждой смены.', 400)
        ids = await self.save_shifts(request, payload['shifts'], team=True)
        return web.json_response({'ids': ids}, status=201)

    async def save_shifts(self, request, payloads, *, team=False):
        actor = request["miniapp_actor"]
        require_schedule_editor(actor["roles"])
        parsed = [parse_shift(payload, self.tz, self.today()) for payload in payloads]
        user_ids = sorted({row[0] for row in parsed})
        if (not team and len(user_ids) != 1) or (max(self.day(p['date']) for p in payloads) - min(self.day(p['date']) for p in payloads)).days > 30:
            raise AccessError('Период графика — не более 31 дня.', 400)
        for uid in user_ids:
            ordered = sorted((row for row in parsed if row[0] == uid), key=lambda row: row[2])
            if any(previous[3] > following[2] for previous, following in pairwise(ordered)):
                raise AccessError(f'У сотрудника №{uid} пересекаются выбранные смены. Измените интервалы.', 409)
        ids = []
        async with self.engine.begin() as conn:
            names = {}
            # Consistent lock order serializes concurrent plans without a lock-order deadlock.
            for uid in sorted(set(user_ids) | {actor['id']}):
                users = await self.rows(conn, "SELECT id,name,active FROM users WHERE id=:uid FOR UPDATE", uid=uid)
                if not users or not users[0]["active"]:
                    raise AccessError("Сотрудник недоступен.", 400)
                roles = await self.rows(conn, "SELECT role FROM user_roles WHERE user_id=:uid", uid=uid)
                if uid in user_ids and not any(r["role"] in {"PHOTOGRAPHER", "MANAGER"} for r in roles):
                    raise AccessError("Нужен фотограф или менеджер записи.", 400)
                names[uid] = users[0]['name']
            editor_roles = {r['role'] for r in await self.rows(conn,
                'SELECT role FROM user_roles WHERE user_id=:uid', uid=actor['id'])}
            require_schedule_editor(editor_roles)
            for payload, (uid, hid, start, end) in zip(payloads, parsed):
                if not await self.rows(conn, "SELECT id FROM hotels WHERE id=:hid AND active=TRUE", hid=hid):
                    raise AccessError("Отель недоступен.", 400)
                if 'OWNER' not in editor_roles and not await self.rows(conn,
                        'SELECT hotel_id FROM hotel_employees WHERE user_id=:uid AND hotel_id=:hid',
                        uid=actor['id'], hid=hid):
                    raise AccessError('Изменять график можно только в назначенных вам отелях.', 403)
                if await self.rows(conn, """SELECT id FROM shifts WHERE user_id=:uid AND status<>'CANCELLED'
                    AND start_at<:end AND end_at>:start""", uid=uid, start=start, end=end):
                    raise AccessError(f"У {names[uid]} уже есть смена, пересекающая {payload['date']} {payload['start']}–{payload['end']}.", 409)
                row = (await self.rows(conn, """INSERT INTO shifts (user_id,hotel_id,start_at,end_at,status)
                    VALUES (:uid,:hid,:start,:end,'PLANNED') RETURNING id""", uid=uid, hid=hid, start=start, end=end))[0]
                detail = f"{names[uid]} · {payload['date']} {payload['start']}–{payload['end']} · отель №{hid}; новая запланированная смена"
                await self.audit_write(conn, actor, "miniapp_shift_created", "shift", row["id"], detail)
                ids.append(row['id'])
        return ids

    async def cancel_shift(self, request):
        actor = request["miniapp_actor"]
        require_schedule_editor(actor["roles"])
        try:
            sid = int(request.match_info["id"])
            if not 0 < sid < 2**31:
                raise ValueError
        except ValueError as exc:
            raise AccessError("Некорректный номер смены.", 400) from exc
        async with self.engine.begin() as conn:
            rows = await self.rows(conn, "SELECT * FROM shifts WHERE id=:id", id=sid)
            if not rows:
                raise AccessError("Смена не найдена.", 404)
            # Match planning/check-in lock order and recheck grants inside the mutation.
            for uid in sorted({actor['id'], rows[0]['user_id']}):
                users = await self.rows(conn, 'SELECT id,active FROM users WHERE id=:uid FOR UPDATE', uid=uid)
                if uid == actor['id'] and (not users or not users[0]['active']):
                    raise AccessError('Рабочий доступ отключён.', 403)
            editor_roles = {r['role'] for r in await self.rows(conn,
                'SELECT role FROM user_roles WHERE user_id=:uid', uid=actor['id'])}
            require_schedule_editor(editor_roles)
            rows = await self.rows(conn, "SELECT * FROM shifts WHERE id=:id FOR UPDATE", id=sid)
            shift = rows[0]
            if 'OWNER' not in editor_roles and not await self.rows(conn,
                    'SELECT hotel_id FROM hotel_employees WHERE user_id=:uid AND hotel_id=:hid',
                    uid=actor['id'], hid=shift['hotel_id']):
                raise AccessError('Изменять график можно только в назначенных вам отелях.', 403)
            if shift["status"] == "CANCELLED":
                return web.json_response({"ok": True})
            day = as_utc(shift["start_at"]).astimezone(self.tz).date()
            if day < self.today() or shift["status"] != "PLANNED" or await self.rows(conn,
                "SELECT id FROM shift_check_ins WHERE user_id=:uid AND shift_date=:day AND started_at IS NOT NULL",
                uid=shift["user_id"], day=day):
                raise AccessError("Начавшуюся или прошедшую смену отменять нельзя.", 409)
            await conn.execute(text("UPDATE shifts SET status='CANCELLED' WHERE id=:id"), {"id": sid})
            await self.audit_write(conn, actor, "miniapp_shift_cancelled", "shift", sid,
                f"Сотрудник №{shift['user_id']} · {day} · статус: PLANNED → CANCELLED")
        return web.json_response({"ok": True})

    async def audit(self, request):
        actor = request["miniapp_actor"]
        require_owner(actor["roles"])
        try:
            before = int(request.query.get("before", 2**31-1))
            if not 0 < before < 2**63:
                raise ValueError
        except ValueError as exc:
            raise AccessError("Некорректная страница аудита.", 400) from exc
        async with self.engine.connect() as conn:
            rows = await self.rows(conn, """SELECT a.*,u.name AS actor FROM audit_logs a LEFT JOIN users u ON u.id=a.user_id
                WHERE a.id<:before ORDER BY a.id DESC LIMIT 31""", before=before)
        from .audit_labels import presentation
        items = [{"id": r["id"], "actor": r["actor"] or (f"Пользователь №{r['user_id']}" if r["user_id"] else "Система"), "at": as_utc(r["created_at"]).isoformat(),
                  "action": r["action"], "entity": r["entity"], "entityId": r["entity_id"],
                  **presentation(r['action'], r['entity'], r['details'], ACTIONS)} for r in rows[:30]]
        return web.json_response({"items": items, "next": rows[29]["id"] if len(rows)>30 else None})

    async def academy(self, request):
        actor = request["miniapp_actor"]
        uid = actor["id"]
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
        async with self.engine.connect() as conn:
            completed = await self.rows(conn, "SELECT topic_slug FROM academy_lesson_progress WHERE user_id=:uid", uid=uid)
            practices = await self.rows(conn, "SELECT id,category_slug,status FROM training_assignments WHERE user_id=:uid ORDER BY id DESC", uid=uid)
            reviews = await self.rows(conn, "SELECT id,quality_score,issues,recommendation FROM academy_reviews WHERE user_id=:uid ORDER BY id DESC LIMIT 30", uid=uid)
            ai_reviews = await self.rows(conn, """SELECT r.id,r.result FROM academy_practice_reviews r
                JOIN training_assignments a ON a.id=r.assignment_id WHERE a.user_id=:uid ORDER BY r.id DESC LIMIT 30""", uid=uid)
            certificate = await self.rows(conn, "SELECT certificate_no,verification_code,final_score,issued_at FROM academy_certificates WHERE user_id=:uid AND revoked_at IS NULL", uid=uid)
            locations = await self.rows(conn, "SELECT id,name,description,shot_plan FROM academy_locations WHERE active=TRUE ORDER BY name")
            tip_rows = await self.rows(conn, "SELECT ai_analysis FROM training_assignments WHERE user_id=:uid AND ai_analysis IS NOT NULL ORDER BY id DESC LIMIT 10", uid=uid)
            team = []
            if "OWNER" in actor["roles"]:
                team = await self.rows(conn, """SELECT u.id,u.name,
                    (SELECT COUNT(*) FROM academy_lesson_progress alp WHERE alp.user_id=u.id AND alp.topic_slug NOT LIKE 'guide-%') AS lessons,
                    (SELECT COUNT(*) FROM training_assignments ta WHERE ta.user_id=u.id AND ta.status='COMPLETED') AS practices,
                    (SELECT AVG(ta.ai_score) FROM training_assignments ta WHERE ta.user_id=u.id AND ta.status='COMPLETED') AS quality,
                    (SELECT COALESCE(SUM(s.amount),0) FROM sales s WHERE s.credited_user_id=u.id AND s.created_at>=:cutoff) AS sales,
                    (SELECT COUNT(*) FROM sales s WHERE s.credited_user_id=u.id AND s.created_at>=:cutoff) AS sales_count
                    FROM users u JOIN user_roles ur ON ur.user_id=u.id AND ur.role='PHOTOGRAPHER'
                    WHERE u.active=TRUE ORDER BY u.name""", cutoff=cutoff)
        known = {l["slug"] for l in self.lessons}
        done = [r["topic_slug"] for r in completed if r["topic_slug"] in known]
        done_set = set(done)
        accepted = {p["category_slug"] for p in practices if p["status"] == "COMPLETED"}
        unlocked = self.academy_state(done_set, accepted)
        lessons = [dict(lesson, locked=lesson["block"] > unlocked) for lesson in self.lessons]
        blocks = []
        for block in self.blocks:
            slugs = {l["slug"] for l in self.lessons if l["block"] == block["number"]}
            lesson_done = len(slugs & done_set)
            practice_done = not block["practiceCategories"] or bool(
                set(block["practiceCategories"]) & accepted
            )
            blocks.append(dict(block, locked=block["number"] > unlocked,
                               lessonsDone=lesson_done, lessonsTotal=len(slugs),
                               practiceDone=practice_done))
        current = next((b for b in blocks if not b["locked"] and
                        (b["lessonsDone"] < b["lessonsTotal"] or not b["practiceDone"])),
                       blocks[-1] if blocks else None)
        tip = personal_tip([SimpleNamespace(ai_analysis=r["ai_analysis"]) for r in tip_rows])
        from .academy_coach import review_data
        fresh_reviews = []
        for row in ai_reviews:
            result = review_data(row["result"])
            score = result.get("score")
            if type(score) is int and 0 <= score <= 100:
                fresh_reviews.append({"id": f"ai-{row['id']}", "score": score / 10, "source": "AI",
                    "strengths": "; ".join(result.get("strengths", [])), "issues": "; ".join(result.get("issues", [])),
                    "recommendation": result.get("next_action", "")})
        cert = certificate[0] if certificate else None
        course_done = bool(blocks) and all(
            b["lessonsDone"] == b["lessonsTotal"] and b["practiceDone"] for b in blocks
        )
        final_assignment = next((p for p in practices if p["category_slug"] == "final_exam"), None)
        return web.json_response({"lessons": lessons, "blocks": blocks,
            "currentBlock": current["number"] if current else None,
            "carryOver": True, "completed": done, "points": len(done)*10,
            "totalPoints": len(done)*10 + sum(p["status"] == "COMPLETED" for p in practices)*100,
            "acceptedCount": sum(p["status"] == "COMPLETED" for p in practices),
            "practices": [{"id": p["id"], "category": p["category_slug"], "status": p["status"]} for p in practices],
            "reviews": fresh_reviews + [{"id": r["id"], "score": r["quality_score"], "issues": r["issues"], "recommendation": r["recommendation"], "source": "ARCHIVE"} for r in reviews],
            "personalTip": tip,
            "finalExam": {"eligible": course_done, "category": "final_exam",
                "status": final_assignment["status"] if final_assignment else None,
                "assignmentId": final_assignment["id"] if final_assignment else None},
            "certificate": ({"number": cert["certificate_no"], "score": cert["final_score"],
                "issuedAt": as_utc(cert["issued_at"]).isoformat(),
                "url": f"{request.scheme}://{request.host}/certificate/{cert['verification_code']}"} if cert else None),
            "locations": [{"id": row["id"], "name": row["name"], "description": row["description"],
                "shotPlan": json.loads(row["shot_plan"] or "[]")} for row in locations],
            "team": [{"id": row["id"], "name": row["name"], "lessons": int(row["lessons"] or 0),
                "practices": int(row["practices"] or 0), "quality": round(float(row["quality"]), 1) if row["quality"] is not None else None,
                "sales": cents(row["sales"]), "salesCount": int(row["sales_count"] or 0)} for row in team]})

    async def certificate_page(self, request):
        code = request.match_info.get("code", "")
        if len(code) > 64 or not code:
            raise web.HTTPNotFound()
        async with self.engine.connect() as conn:
            rows = await self.rows(conn, """SELECT c.certificate_no,c.final_score,c.issued_at,c.revoked_at,u.name
                FROM academy_certificates c JOIN users u ON u.id=c.user_id
                WHERE c.verification_code=:code""", code=code)
        if not rows:
            raise web.HTTPNotFound()
        row = rows[0]
        valid = row["revoked_at"] is None
        name = html.escape(row["name"])
        number = html.escape(row["certificate_no"])
        issued = as_utc(row["issued_at"]).date().isoformat()
        status = "VALID / ДЕЙСТВИТЕЛЕН" if valid else "REVOKED / ОТОЗВАН"
        page = f"""<!doctype html><html lang='en'><meta charset='utf-8'>
        <meta name='viewport' content='width=device-width,initial-scale=1'>
        <title>Photo Boss International Certificate</title>
        <style>body{{margin:0;background:#090909;color:#eee;font:16px Arial;display:grid;min-height:100vh;place-items:center}}
        main{{max-width:820px;margin:24px;padding:48px;border:2px solid #c9a94b;background:#111;text-align:center}}
        h1{{color:#d8bb61;letter-spacing:.08em}}h2{{font-size:34px}}.status{{padding:12px;border:1px solid #d8bb61}}
        small{{color:#aaa}}@media print{{body{{background:white;color:black}}main{{background:white}}}}</style>
        <main><small>PHOTO BOSS ACADEMY</small><h1>INTERNATIONAL CERTIFICATE</h1>
        <p>Certificate of completion / Сертификат об окончании</p><h2>{name}</h2>
        <p>has completed the Photo Boss Academy program and passed the final practical assessment.<br>
        завершил(а) программу Photo Boss Academy и прошёл(а) итоговую практическую аттестацию.</p>
        <p>Final score / Итоговая оценка: <b>{row['final_score']}/100</b></p>
        <p>Certificate No: <b>{number}</b><br>Issued / Выдан: {issued}</p>
        <p class='status'>{status}</p><small>Private professional certificate issued by Photo Boss. Not a state-accredited diploma.<br>
        Частный профессиональный сертификат Photo Boss. Не является дипломом государственной аккредитации.</small></main></html>"""
        return web.Response(text=page, content_type="text/html")

    async def complete_lesson(self, request):
        actor, slug = request["miniapp_actor"], request.match_info["slug"]
        from .services.academy_curriculum import MANAGER_LESSONS
        lesson = next((l for l in [*self.lessons, *MANAGER_LESSONS] if l["slug"] == slug), None)
        if lesson is None:
            raise AccessError("Урок не найден.", 404)
        async with self.engine.begin() as conn:
            if len(self.lessons) == 28 or slug.startswith("booking-"):
                passed = await self.rows(conn, "SELECT id FROM academy_quiz_attempts WHERE user_id=:uid AND topic_slug=:slug AND passed=TRUE LIMIT 1", uid=actor["id"], slug=slug)
                if not passed:
                    raise AccessError("Сначала пройдите короткий тест по уроку.", 409)
            completed = {r["topic_slug"] for r in await self.rows(
                conn, "SELECT topic_slug FROM academy_lesson_progress WHERE user_id=:uid",
                uid=actor["id"])}
            practices = await self.rows(
                conn, "SELECT category_slug,status FROM training_assignments WHERE user_id=:uid",
                uid=actor["id"])
            accepted = {p["category_slug"] for p in practices if p["status"] == "COMPLETED"}
            if lesson["block"] > self.academy_state(completed, accepted):
                raise AccessError(
                    "Сначала завершите предыдущие четыре урока и сдайте практику.", 409
                )
            changed = await self.rows(conn, """INSERT INTO academy_lesson_progress (user_id,topic_slug,completed_at)
                VALUES (:uid,:slug,:now) ON CONFLICT (user_id,topic_slug) DO NOTHING RETURNING id""",
                uid=actor["id"], slug=slug, now=datetime.now(timezone.utc).replace(tzinfo=None))
            if changed:
                await self.audit_write(conn, actor, "academy_lesson_completed", "academy_lesson", None, lesson["title"])
        return web.json_response({"ok": True})

    async def create_academy_location(self, request):
        actor = request["miniapp_actor"]
        require_owner(actor["roles"])
        body = await self.body(request)
        if set(body) != {"name", "description", "shotPlan", "hotelId"}:
            raise AccessError("Заполните карточку локации полностью.", 400)
        name, description, shot_plan = body["name"], body["description"], body["shotPlan"]
        hotel_id = body["hotelId"]
        if not isinstance(name, str) or not 2 <= len(name.strip()) <= 150:
            raise AccessError("Название локации должно содержать 2–150 символов.", 400)
        if not isinstance(description, str) or len(description) > 2000:
            raise AccessError("Описание локации слишком длинное.", 400)
        if (not isinstance(shot_plan, list) or len(shot_plan) != 5 or
                any(not isinstance(item, str) or not 3 <= len(item.strip()) <= 300
                    for item in shot_plan)):
            raise AccessError("Для локации нужны ровно пять понятных схем кадров.", 400)
        if hotel_id is not None and (type(hotel_id) is not int or hotel_id <= 0):
            raise AccessError("Некорректный отель.", 400)
        async with self.engine.begin() as conn:
            if hotel_id is not None and not await self.rows(
                conn, "SELECT id FROM hotels WHERE id=:id AND active=TRUE", id=hotel_id
            ):
                raise AccessError("Отель не найден.", 404)
            rows = await self.rows(conn, """INSERT INTO academy_locations
                (hotel_id,name,description,shot_plan,active,created_at)
                VALUES (:hotel,:name,:description,:plan,TRUE,:now) RETURNING id""",
                hotel=hotel_id, name=name.strip(), description=description.strip(),
                plan=json.dumps([item.strip() for item in shot_plan], ensure_ascii=False),
                now=datetime.now(timezone.utc).replace(tzinfo=None))
            await self.audit_write(conn, actor, "academy_location_created",
                                   "academy_location", rows[0]["id"], name.strip())
        return web.json_response({"id": rows[0]["id"]}, status=201)

    async def handoff(self, request):
        actor = request["miniapp_actor"]
        body = await self.body(request)
        # Compatibility for older clients. No Telegram messages or bot URLs.
        options = {"new-booking": "/app/#workflow", "new-sale": "/app/#workflow",
                   "shift": "/app/#schedule", "practice": "/app/#academy"}
        choice = body.get("action")
        if set(body) != {"action"} or not isinstance(choice, str) or choice not in options:
            raise AccessError("Действие не найдено.", 400)
        if choice == "new-booking" and not actor["permissions"]["manageBookings"]:
            raise AccessError("Создавать записи может менеджер или администратор.")
        return web.json_response({"url": options[choice]})

    async def static_file(self, request):
        name = request.match_info.get("asset", "index.html")
        allowed = {"js/delivery.js", "css/delivery-client.css", "sw.js", "manifest.webmanifest", "js/install.js", "assets/icon-192.png", "assets/icon-512.png", "assets/icon-maskable-512.png", "assets/apple-touch-icon.png", "js/browser-login.js", "js/account.js", "js/schedule-editor.js", "js/localstore.js", "js/workflow.js", "index.html", "config.js", "css/styles.css", "js/app.js", "js/icons.js", "js/domain.js",
                   "js/realtime-training.js", "js/voice-training.js", "js/booking-control.js", "js/form-state.js", "js/galleries.js", "js/gallery-view.js", "js/gallery-client.js", "css/galleries.css", "js/development.js", "js/academy-coach.js", "js/academy-mentor.js", "js/booking-diagnostic.js", "js/outbox.js", "js/workday.js", "js/feedback.js", "js/team.js", "js/insights.js", "js/api.js", "js/telegram.js", "js/academy.js", "js/practice.js", "assets/icon.svg", "assets/studio.jpg",
                   "assets/academy/hero.jpg", "assets/academy/family.jpg", "assets/academy/child.jpg",
                   "assets/academy/couple.jpg", "assets/academy/coast.jpg", "assets/academy/evening.jpg", "assets/academy/lens.jpg", "assets/academy/exposure.svg", "assets/academy/window-light.svg", "assets/academy/perspective.svg", "assets/academy/group-depth.svg"}
        allowed.add('js/sale-files.js')
        allowed.update({'js/attendance-fines.js', 'js/hotel-points.js', 'js/storage-audit.js', 'js/notifications.js', 'js/lunch.js', 'js/hotel-pricing.js', 'js/bank-payments.js'})
        if name not in allowed:
            raise web.HTTPNotFound()
        path = self.static_dir / name
        if not path.is_file():
            raise web.HTTPNotFound()
        headers = {"Content-Type": "application/manifest+json"} if name == "manifest.webmanifest" else None
        return web.FileResponse(path, headers=headers)

    def register(self, app):
        app.middlewares.append(self.middleware)
        app.router.add_get("/app/", self.static_file)
        app.router.add_get("/app/{asset:.*}", self.static_file)
        app.router.add_get("/certificate/{code}", self.certificate_page)
        routes = [("GET", "/diagnostics/ai", self.ai_diagnostics), ("POST", "/diagnostics/ai/probe", self.ai_probe), ("GET", "/me", self.me), ("POST", "/session", self.session_open), ("PUT", "/preferences", self.preferences),
            ("GET", "/dashboard", self.dashboard), ("GET", "/bookings", self.bookings),
            ("GET", "/booking-photographers", self.booking_photographers),
            ("GET", "/assignment-notifications", self.assignment_notifications),
            ("POST", r"/bookings/{id:\d+}/photographer", self.assign_booking_photographer),
            ("POST", r"/bookings/{id:\d+}/shooting/{action:pickup|start|finish|ready}", self.shooting_action),
            ("GET", "/finance", self.finance), ("GET", "/schedule", self.schedule),
            ("POST", "/schedule", self.create_shift), ("POST", "/schedule/batch", self.create_shift_batch), ("POST", "/schedule/team", self.create_team_schedule), ("DELETE", "/schedule/{id}", self.cancel_shift),
            ("GET", "/audit", self.audit), ("GET", "/academy", self.academy),
            ("POST", "/academy/lessons/{slug}", self.complete_lesson),
            ("POST", "/academy/locations", self.create_academy_location),
            ("POST", "/handoff", self.handoff)]
        for method, path, handler in routes:
            app.router.add_route(method, PREFIX + path, handler)
        from .academy_coach import install_academy_coach
        install_academy_coach(app, self)
        from .academy_mentor import install_academy_mentor
        install_academy_mentor(app, self, app["academy_coach"])
        from .academy_diagnostic import install_academy_diagnostic
        install_academy_diagnostic(app, self)


def install_miniapp(app, *, engine, bot, lessons, blocks=None, tz_name="Europe/Moscow"):
    miniapp = MiniApp(engine, bot, lessons, blocks=blocks, tz_name=tz_name)
    miniapp.register(app)
    from .hotel_points import install_hotel_points
    install_hotel_points(app, miniapp)
    from .delivery import install_delivery
    install_delivery(app, miniapp)
    from .insights import install_insights
    install_insights(app, miniapp)
    from .team import install_team
    install_team(app, miniapp)
    from .workday import install_workday
    install_workday(app, miniapp)
    from .development import install_development
    install_development(app, miniapp)
    from .booking_management import install_booking_management
    install_booking_management(app, miniapp)
    from .storage_inventory import install_inventory
    install_inventory(app, miniapp)
    from .booking_control import install_booking_control
    install_booking_control(app, miniapp)
    return miniapp


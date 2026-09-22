"""Production Mini App backed by the existing database, never demo data.

No second payroll ledger is created. Financial mutations and photo delivery use
existing bot workflows. Plans are not presented as confirmed attendance.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from aiohttp import web
from sqlalchemy import text

from .miniapp_security import (
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
)
from .services.academy_growth import personal_tip

logger = logging.getLogger(__name__)
PREFIX = "/api/miniapp"
ROLE_ORDER = ("OWNER", "ADMIN", "PHOTOGRAPHER", "MANAGER")
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
    "employee_role_removed": "Снял роль сотрудника",
    "academy_lesson_completed": "Завершил урок Академии",
    "academy_location_created": "Добавил учебную локацию",
    "miniapp_theme_changed": "Изменил оформление приложения",
    "miniapp_opened": "Открыл приложение",
    "miniapp_screen_capture_changed": "Изменил разрешение на скриншоты",
    "miniapp_shift_created": "Назначил смену",
    "miniapp_shift_cancelled": "Отменил запланированную смену",
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
        telegram_id = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""), self.bot.token)
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
            actor["permissions"] = role_permissions(actor["roles"])
            return actor

    @web.middleware
    async def middleware(self, request, handler):
        if not (request.path.startswith(PREFIX) or request.path.startswith("/app/")):
            return await handler(request)
        from .audit_context import actor_id
        actor_token = actor_id.set(None)
        try:
            if request.path.startswith(PREFIX):
                if any(k in request.query for k in ("role", "roles", "userId", "user_id", "employee_id", "scope")):
                    raise AccessError("Пользователь и роль определяются сервером.", 400)
                request["miniapp_actor"] = await self.actor(request)
                actor_id.set(request["miniapp_actor"]["id"])
            response = await handler(request)
        except AccessError as exc:
            body = {"error": str(exc)}
            if exc.status == 403 and request.get("miniapp_telegram_id"):
                # Signed account identity only; never disclose a different user or roles.
                body["telegramId"] = request["miniapp_telegram_id"]
            response = web.json_response(body, status=exc.status)
        except web.HTTPException as exc:
            response = web.json_response({"error": "Недопустимый запрос."}, status=exc.status)
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

    async def body(self, request):
        if request.content_length and request.content_length > 8192:
            raise AccessError("Слишком большой запрос.", 413)
        try:
            raw = bytearray()
            while not request.content.at_eof():
                raw.extend(await request.content.read(8193 - len(raw)))
                if len(raw) > 8192:
                    raise AccessError("Слишком большой запрос.", 413)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise TypeError("JSON body must be an object")
            return value
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            if isinstance(exc, AccessError):
                raise
            raise AccessError("Ожидается корректный JSON-объект.", 400) from exc

    def day(self, value):
        try:
            return date.fromisoformat(value)
        except (ValueError, TypeError) as exc:
            raise AccessError("Некорректная дата.", 400) from exc

    async def me(self, request):
        actor = request["miniapp_actor"]
        async with self.engine.connect() as conn:
            theme = await self.rows(conn, "SELECT value FROM settings WHERE key=:key", key=f"miniapp:theme:{actor['id']}")
            capture = await self.rows(conn, "SELECT value FROM settings WHERE key=:key", key=f"miniapp:screen_capture:{actor['id']}")
        capture_allowed = (
            True
            if "OWNER" in actor["roles"]
            else bool(capture and capture[0]["value"] == "1")
        )
        return web.json_response({"user": {"id": actor["id"], "telegramId": actor["tg_id"],
            "name": actor["name"], "roles": actor["roles"],
            "theme": theme[0]["value"] if theme and theme[0]["value"] in THEMES else ("premium" if "OWNER" in actor["roles"] else "light")},
            "permissions": actor["permissions"], "screenCaptureAllowed": capture_allowed,
            "today": self.today().isoformat(),
            "timezone": str(self.tz), "mode": "live"})

    async def session_open(self, request):
        if await self.body(request):
            raise AccessError("Лишние параметры входа.", 400)
        actor = request["miniapp_actor"]
        digest = hashlib.sha256(request.headers["X-Telegram-Init-Data"].encode()).hexdigest()
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

    async def bookings_data(self, conn, actor, day):
        own = actor["permissions"]["financeScope"] == "self"
        clause = "AND (b.photographer_id=:uid OR b.manager_id=:uid)" if own else ""
        rows = await self.rows(conn, f"""SELECT b.id,b.room,b.shoot_date,b.shoot_time,b.status,
            b.photographer_id,b.manager_id,b.hotel_id,c.name AS client,h.name AS hotel,
            p.name AS photographer,pk.name AS package,
            (SELECT COUNT(*) FROM photos ph JOIN shootings sh ON sh.id=ph.shooting_id
             WHERE sh.booking_id=b.id) AS frames
            FROM bookings b JOIN clients c ON c.id=b.client_id
            JOIN hotels h ON h.id=b.hotel_id LEFT JOIN users p ON p.id=b.photographer_id
            LEFT JOIN packages pk ON pk.id=b.package_id
            WHERE b.shoot_date=:day {clause} ORDER BY b.shoot_time,b.id""", uid=actor["id"], day=day)
        return [{"id": r["id"], "client": r["client"], "date": str(r["shoot_date"]),
                 "time": str(r["shoot_time"])[:5], "room": r["room"], "status": r["status"],
                 "photographerId": r["photographer_id"], "photographer": r["photographer"],
                 "managerId": r["manager_id"], "hotelId": r["hotel_id"], "hotel": r["hotel"],
                 "type": r["package"] or "Съёмка", "frames": r["frames"]} for r in rows]

    async def bookings(self, request):
        day = self.day(request.query.get("day", str(self.today())))
        async with self.engine.connect() as conn:
            items = await self.bookings_data(conn, request["miniapp_actor"], day)
        return web.json_response({"items": items})

    async def finance_data(self, conn, actor, start, end, period):
        if "ADMIN" in actor["roles"] and "OWNER" not in actor["roles"] and (start != self.today() or end != self.today()):
            raise AccessError("Общая касса за прошлые периоды доступна только владельцу.")
        own = actor["permissions"]["financeScope"] == "self"
        lower, upper = utc_bounds(start, end, self.tz)
        args = {"lo": lower, "hi": upper, "uid": actor["id"]}
        sales = await self.rows(conn, """SELECT s.*, u.name AS employee, c.name AS client
            FROM sales s JOIN users u ON u.id=s.credited_user_id
            LEFT JOIN bookings b ON b.id=s.booking_id LEFT JOIN clients c ON c.id=b.client_id
            WHERE s.created_at>=:lo AND s.created_at<:hi """ +
            ("AND s.credited_user_id=:uid " if own else "") + "ORDER BY s.created_at DESC", **args)
        payroll = await self.rows(conn, "SELECT p.*,u.name FROM payroll_entries p JOIN users u ON u.id=p.user_id WHERE p.created_at>=:lo AND p.created_at<:hi " + ("AND p.user_id=:uid" if own else ""), **args)
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
            receipts = await self.rows(conn, """SELECT verified_amount FROM receipts
                WHERE status='APPROVED' AND reviewed_at>=:lo AND reviewed_at<:hi""", **args)
            cash = sum(cents(r["verified_amount"]) for r in receipts)
        return {"period": period, "from": str(start), "to": str(end), "scope": "self" if own else "company",
                "sales": sum(cents(s["amount"]) for s in sales), "cashReceived": cash,
                "payroll": sum(e["earned"] for e in employees.values()),
                "employees": sorted(employees.values(), key=lambda x: (-x["sales"], x["name"])),
                "salesRows": [{"id": s["id"], "bookingId": s["booking_id"], "client": s["client"],
                    "employee": s["employee"], "date": str(as_utc(s["created_at"]).astimezone(self.tz).date()),
                    "amount": cents(s["amount"]), "status": s["payment_status"]} for s in sales[:100]],
                "salesCount": len(sales), "salesListLimited": len(sales) > 100,
                "note": "Касса — подтверждённые чеки по дате подтверждения. Начислено — комиссии и записи премий/удержаний существующего бота. Это не выплаченная зарплата и не чистая прибыль. Продажи показаны до учёта оплаты."}

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
        if own and not team:
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
            items.append({"id": r["id"], "userId": r["user_id"], "name": r["name"], "role": r["role"],
                "hotelId": r["hotel_id"], "hotel": r["hotel"], "date": str(st.date()), "start": st.strftime("%H:%M"),
                "end": en.strftime("%H:%M"), "status": r["status"], "attendance": attendance})
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
        return web.json_response(result)

    async def schedule(self, request):
        actor = request["miniapp_actor"]
        start = self.day(request.query.get("from", str(self.today())))
        end = self.day(request.query.get("to", str(start)))
        if not 0 <= (end-start).days <= 31:
            raise AccessError("Период графика должен быть не длиннее 31 дня.", 400)
        async with self.engine.connect() as conn:
            items = await self.shifts_data(conn, actor, start, end)
            employees, hotels = [], []
            if actor["permissions"]["manageSchedule"]:
                employees = [dict(r) for r in await self.rows(conn, """SELECT DISTINCT u.id,u.name FROM users u
                    JOIN user_roles r ON r.user_id=u.id WHERE u.active=TRUE AND r.role IN ('MANAGER','PHOTOGRAPHER') ORDER BY u.name""")]
                hotels = [dict(r) for r in await self.rows(conn, "SELECT id,name FROM hotels WHERE active=TRUE ORDER BY name")]
        return web.json_response({"items": items, "employees": employees, "hotels": hotels})

    async def create_shift(self, request):
        actor = request["miniapp_actor"]
        require_schedule_editor(actor["roles"])
        payload = await self.body(request)
        uid, hid, start, end = parse_shift(payload, self.tz, self.today())
        async with self.engine.begin() as conn:
            users = await self.rows(conn, "SELECT id,name,active FROM users WHERE id=:uid FOR UPDATE", uid=uid)
            if not users or not users[0]["active"]:
                raise AccessError("Сотрудник недоступен.", 400)
            roles = await self.rows(conn, "SELECT role FROM user_roles WHERE user_id=:uid", uid=uid)
            if not any(r["role"] in {"PHOTOGRAPHER", "MANAGER"} for r in roles):
                raise AccessError("Нужен фотограф или менеджер записи.", 400)
            if not await self.rows(conn, "SELECT id FROM hotels WHERE id=:hid AND active=TRUE", hid=hid):
                raise AccessError("Отель недоступен.", 400)
            if await self.rows(conn, """SELECT id FROM shifts WHERE user_id=:uid AND status<>'CANCELLED'
                AND start_at<:end AND end_at>:start""", uid=uid, start=start, end=end):
                raise AccessError("У сотрудника уже есть пересекающаяся смена.", 409)
            row = (await self.rows(conn, """INSERT INTO shifts (user_id,hotel_id,start_at,end_at,status)
                VALUES (:uid,:hid,:start,:end,'PLANNED') RETURNING id""", uid=uid, hid=hid, start=start, end=end))[0]
            detail = f"{users[0]['name']} · {payload['date']} {payload['start']}–{payload['end']} · отель №{hid}; новая запланированная смена"
            await self.audit_write(conn, actor, "miniapp_shift_created", "shift", row["id"], detail)
        return web.json_response({"id": row["id"]}, status=201)

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
            rows = await self.rows(conn, "SELECT * FROM shifts WHERE id=:id FOR UPDATE", id=sid)
            if not rows:
                raise AccessError("Смена не найдена.", 404)
            shift = rows[0]
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
        items = [{"id": r["id"], "actor": r["actor"] or (f"Пользователь №{r['user_id']}" if r["user_id"] else "Система"), "at": as_utc(r["created_at"]).isoformat(),
                  "action": r["action"], "title": ACTIONS.get(r["action"], f"Событие «{r['action']}»"),
                  "entity": r["entity"], "entityId": r["entity_id"], "details": r["details"] or "Дополнительные сведения не были записаны."} for r in rows[:30]]
        return web.json_response({"items": items, "next": rows[29]["id"] if len(rows)>30 else None})

    async def academy(self, request):
        actor = request["miniapp_actor"]
        uid = actor["id"]
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
        async with self.engine.connect() as conn:
            completed = await self.rows(conn, "SELECT topic_slug FROM academy_lesson_progress WHERE user_id=:uid", uid=uid)
            practices = await self.rows(conn, "SELECT id,category_slug,status FROM training_assignments WHERE user_id=:uid ORDER BY id DESC LIMIT 30", uid=uid)
            reviews = await self.rows(conn, "SELECT id,quality_score,issues,recommendation FROM academy_reviews WHERE user_id=:uid ORDER BY id DESC LIMIT 30", uid=uid)
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
        cert = certificate[0] if certificate else None
        return web.json_response({"lessons": lessons, "blocks": blocks,
            "currentBlock": current["number"] if current else None,
            "carryOver": True, "completed": done, "points": len(done)*10,
            "practices": [{"id": p["id"], "category": p["category_slug"], "status": p["status"]} for p in practices],
            "reviews": [{"id": r["id"], "score": r["quality_score"], "issues": r["issues"], "recommendation": r["recommendation"]} for r in reviews],
            "personalTip": tip,
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
        <p>has completed the 28-day Photo Boss Academy program and passed the final practical assessment.<br>
        завершил(а) 28-дневную программу Photo Boss Academy и прошёл(а) итоговую практическую аттестацию.</p>
        <p>Final score / Итоговая оценка: <b>{row['final_score']}/100</b></p>
        <p>Certificate No: <b>{number}</b><br>Issued / Выдан: {issued}</p>
        <p class='status'>{status}</p><small>Private professional certificate issued by Photo Boss. Not a state-accredited diploma.<br>
        Частный профессиональный сертификат Photo Boss. Не является дипломом государственной аккредитации.</small></main></html>"""
        return web.Response(text=page, content_type="text/html")

    async def complete_lesson(self, request):
        actor, slug = request["miniapp_actor"], request.match_info["slug"]
        lesson = next((l for l in self.lessons if l["slug"] == slug), None)
        if lesson is None:
            raise AccessError("Урок не найден.", 404)
        async with self.engine.begin() as conn:
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
        allowed = {"sw.js", "js/localstore.js", "js/workflow.js", "index.html", "config.js", "css/styles.css", "js/app.js", "js/icons.js", "js/domain.js",
                   "js/development.js", "js/outbox.js", "js/workday.js", "js/feedback.js", "js/team.js", "js/insights.js", "js/api.js", "js/telegram.js", "js/academy.js", "js/practice.js", "assets/icon.svg", "assets/studio.jpg",
                   "assets/academy/hero.jpg", "assets/academy/family.jpg", "assets/academy/child.jpg",
                   "assets/academy/couple.jpg", "assets/academy/coast.jpg", "assets/academy/evening.jpg", "assets/academy/lens.jpg"}
        if name not in allowed:
            raise web.HTTPNotFound()
        path = self.static_dir / name
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    def register(self, app):
        app.middlewares.append(self.middleware)
        app.router.add_get("/app/", self.static_file)
        app.router.add_get("/app/{asset:.*}", self.static_file)
        app.router.add_get("/certificate/{code}", self.certificate_page)
        routes = [("GET", "/me", self.me), ("POST", "/session", self.session_open), ("PUT", "/preferences", self.preferences),
            ("GET", "/dashboard", self.dashboard), ("GET", "/bookings", self.bookings),
            ("GET", "/finance", self.finance), ("GET", "/schedule", self.schedule),
            ("POST", "/schedule", self.create_shift), ("DELETE", "/schedule/{id}", self.cancel_shift),
            ("GET", "/audit", self.audit), ("GET", "/academy", self.academy),
            ("POST", "/academy/lessons/{slug}", self.complete_lesson),
            ("POST", "/academy/locations", self.create_academy_location),
            ("POST", "/handoff", self.handoff)]
        for method, path, handler in routes:
            app.router.add_route(method, PREFIX + path, handler)


def install_miniapp(app, *, engine, bot, lessons, blocks=None, tz_name="Europe/Moscow"):
    miniapp = MiniApp(engine, bot, lessons, blocks=blocks, tz_name=tz_name)
    miniapp.register(app)
    from .insights import install_insights
    install_insights(app, miniapp)
    from .team import install_team
    install_team(app, miniapp)
    from .workday import install_workday
    install_workday(app, miniapp)
    from .development import install_development
    install_development(app, miniapp)
    return miniapp

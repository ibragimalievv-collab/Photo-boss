"""Staff administration in the Mini App. No payroll or contract signing.

Uses the existing authenticated MiniApp middleware and existing staff tables.
Documents are visibly DRAFT until the operator and signing rules are provided.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from aiogram.exceptions import TelegramAPIError
from aiohttp import web
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .miniapp_security import AccessError
from .work_rules import WORK_RULES_TEXT, WORK_RULES_VERSION

ASSIGNABLE = {"ADMIN", "MANAGER", "PHOTOGRAPHER"}
DOCUMENT_VERSION = "2026-09-20-draft-1"
GENERAL_RULES = """Проект общих правил использования Photo Boss

1. Назначение приложения
Photo Boss предназначен для организации съёмок, графика, рабочих задач и обучения. Доступ определяется назначенной владельцем ролью. Нельзя передавать свой аккаунт другому человеку.

2. Рабочая переписка
Планируемые общий чат и диалоги сотрудников один на один внутри Photo Boss предназначены для рабочих вопросов. Владелец компании имеет доступ к содержимому всех этих чатов, включая сообщения между двумя сотрудниками, для контроля рабочих процессов и решения проблем. Администратор и остальные сотрудники не получают доступ к чужим диалогам. Личные переписки в обычном Telegram не подключаются. Не следует размещать в рабочих чатах личные сведения, не относящиеся к работе.

3. Смены и материалы
Геолокация и фотография запрашиваются при подтверждении начала и завершения смены, а не для непрерывного отслеживания. Получение координат само по себе не доказывает нахождение в отеле. Загружать фотографии гостей и сотрудников можно только при наличии необходимых прав и законного основания.

4. Доступ и история
Владелец может отключить рабочий доступ, сохранив предусмотренную правилами историю. Изменения доступа фиксируются в аудите. Сроки хранения сообщений, основание обработки данных, реквизиты оператора и порядок обращений должны быть утверждены до включения чатов.

5. Статус этого документа
Это проект для согласования, а не действующий договор об оказании услуг, согласие на обработку персональных данных или подтверждение найма. Подписание сейчас отключено. В окончательной версии будут указаны заказчик, применимое право и порядок электронного подтверждения. Пользователь сможет сохранить принятую редакцию и дату подтверждения.
"""

DOCUMENT_VERSION = WORK_RULES_VERSION
GENERAL_RULES = WORK_RULES_TEXT


def positive_id(value, *, maximum=2**31 - 1):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise AccessError("Некорректный идентификатор.", 400)
    if isinstance(value, str) and (not value.isascii() or not value.isdecimal()):
        raise AccessError("Некорректный идентификатор.", 400)
    result = int(value)
    if not 0 < result <= maximum:
        raise AccessError("Некорректный идентификатор.", 400)
    return result


def staff_payload(body, *, editing=False):
    required = {"name", "roles", "hotelIds", "active", "revision"} if editing else {"name", "roles", "hotelIds", "telegramId"}
    if set(body) != required:
        raise AccessError("Набор полей сотрудника изменился. Откройте форму заново.", 400)
    name = body.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 150 or any(ord(c) < 32 for c in name):
        raise AccessError("Введите имя длиной 1–150 символов без управляющих знаков.", 400)
    roles, hotels = body.get("roles"), body.get("hotelIds")
    if not isinstance(roles, list) or not roles or len(roles) > 3 or any(not isinstance(r, str) or r not in ASSIGNABLE for r in roles):
        raise AccessError("Выберите роль: администратор, менеджер или фотограф.", 400)
    if len(set(roles)) != len(roles):
        raise AccessError("Роли не должны повторяться.", 400)
    if not isinstance(hotels, list) or len(hotels) > 50:
        raise AccessError("Некорректный список отелей.", 400)
    hotel_ids = [positive_id(h) for h in hotels]
    if len(set(hotel_ids)) != len(hotel_ids):
        raise AccessError("Отели не должны повторяться.", 400)
    value = {"name": name.strip(), "roles": sorted(roles), "hotelIds": sorted(hotel_ids)}
    if editing:
        if type(body["active"]) is not bool or not isinstance(body["revision"], str) or len(body["revision"]) != 64:
            raise AccessError("Некорректное состояние карточки.", 400)
        value.update(active=body["active"], revision=body["revision"])
    else:
        value["telegramId"] = positive_id(body["telegramId"], maximum=2**52 - 1)
    return value


def revision(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def check_editor(roles, target_roles=(), *, self_edit=False):
    roles, target_roles = set(roles), set(target_roles)
    if not roles & {"OWNER", "ADMIN"}:
        raise AccessError("Управление сотрудниками доступно владельцу и администратору.")
    if self_edit or "OWNER" in target_roles:
        raise AccessError("Эту учётную запись нельзя изменять через форму сотрудников.")
    if "OWNER" not in roles and "ADMIN" in target_roles:
        raise AccessError("Изменять администратора может только владелец.")


class People:
    def __init__(self, miniapp):
        self.api = miniapp
        self.engine = miniapp.engine
        self.bot = miniapp.bot

    async def current_editor(self, conn, actor_id, target_id=None):
        # Lock in a stable order. Re-read active/roles inside the mutation transaction.
        await self.api.rows(conn, "SELECT id FROM users WHERE id IN (:actor,:target) ORDER BY id FOR UPDATE",
                            actor=actor_id, target=target_id or actor_id)
        users = await self.api.rows(conn, "SELECT active FROM users WHERE id=:id", id=actor_id)
        if not users or not users[0]["active"]:
            raise AccessError("Рабочий доступ отключён.")
        rows = await self.api.rows(conn, "SELECT role FROM user_roles WHERE user_id=:id", id=actor_id)
        roles = {r["role"] for r in rows}
        check_editor(roles)
        return roles

    async def card(self, conn, uid):
        users = await self.api.rows(
            conn,
            "SELECT id,tg_id,name,active,terminated_at FROM users WHERE id=:id",
            id=uid,
        )
        if not users:
            raise AccessError("Сотрудник не найден.", 404)
        u = users[0]
        roles = await self.api.rows(conn, "SELECT role FROM user_roles WHERE user_id=:id ORDER BY role", id=uid)
        hotels = await self.api.rows(conn, "SELECT hotel_id FROM hotel_employees WHERE user_id=:id ORDER BY hotel_id", id=uid)
        role_names = sorted({r["role"] for r in roles})
        hotel_ids = sorted({r["hotel_id"] for r in hotels})
        setting_rows = await self.api.rows(
            conn,
            """SELECT key,value FROM settings
               WHERE key IN (:last_login,:screen_capture)""",
            last_login=f"miniapp:last_login:{uid}",
            screen_capture=f"miniapp:screen_capture:{uid}",
        )
        settings = {row["key"]: row["value"] for row in setting_rows}
        core = {
            "id": u["id"],
            "telegramId": u["tg_id"],
            "name": u["name"],
            "active": bool(u["active"]),
            "terminatedAt": (
                u["terminated_at"].isoformat()
                if u["terminated_at"] is not None
                and hasattr(u["terminated_at"], "isoformat")
                else (str(u["terminated_at"]) if u["terminated_at"] else None)
            ),
            "roles": role_names,
            "hotelIds": hotel_ids,
        }
        result = dict(core)
        result["revision"] = revision(core)
        result["lastLoginAt"] = settings.get(f"miniapp:last_login:{uid}")
        stored_capture = settings.get(f"miniapp:screen_capture:{uid}")
        result["screenCaptureAllowed"] = (
            stored_capture == "1" if stored_capture is not None else "OWNER" in role_names
        )
        return result

    async def listing(self, request):
        actor = request["miniapp_actor"]
        check_editor(actor["roles"])
        after = positive_id(request.query["after"]) if "after" in request.query else 0
        archived = request.query.get("archived") == "1"
        async with self.engine.begin() as conn:
            roles = await self.current_editor(conn, actor["id"])
            ids = await self.api.rows(
                conn,
                """SELECT id FROM users
                   WHERE id>:after AND active=:active
                   ORDER BY id LIMIT 101""",
                after=after,
                active=not archived,
            )
            items = [await self.card(conn, r["id"]) for r in ids[:100]]
            hotels = await self.api.rows(
                conn, "SELECT id,name FROM hotels WHERE active=TRUE ORDER BY name,id"
            )
        for item in items:
            item["editable"] = item["id"] != actor["id"] and "OWNER" not in item["roles"] and ("OWNER" in roles or "ADMIN" not in item["roles"])
        return web.json_response({"items": items, "hotels": [dict(h) for h in hotels],
                                  "canAssignAdmin": "OWNER" in roles,
                                  "canManageScreenCapture": "OWNER" in roles,
                                  "archived": archived,
                                  "next": ids[99]["id"] if len(ids) > 100 else None})

    async def verify_hotels(self, conn, ids):
        for hid in ids:
            rows = await self.api.rows(conn, "SELECT id FROM hotels WHERE id=:id AND active=TRUE", id=hid)
            if not rows:
                raise AccessError("Один из выбранных отелей недоступен. Обновите список.", 409)

    async def assignments(self, conn, uid, payload):
        await conn.execute(text("DELETE FROM user_roles WHERE user_id=:id"), {"id": uid})
        for role in payload["roles"]:
            await conn.execute(text("INSERT INTO user_roles(user_id,role) VALUES (:id,:role)"), {"id": uid, "role": role})
        await conn.execute(text("DELETE FROM hotel_employees WHERE user_id=:id"), {"id": uid})
        for hid in payload["hotelIds"]:
            await conn.execute(text("INSERT INTO hotel_employees(user_id,hotel_id) VALUES (:id,:hid)"), {"id": uid, "hid": hid})

    async def create(self, request):
        actor = request["miniapp_actor"]
        check_editor(actor["roles"])
        payload = staff_payload(await self.api.body(request))
        try:
            async with self.engine.begin() as conn:
                roles = await self.current_editor(conn, actor["id"])
                if "ADMIN" in payload["roles"] and "OWNER" not in roles:
                    raise AccessError("Назначать администратора может только владелец.")
                await self.verify_hotels(conn, payload["hotelIds"])
                found = await self.api.rows(conn, "SELECT id FROM users WHERE tg_id=:tg", tg=payload["telegramId"])
                if found:
                    raise AccessError("Этот Telegram ID уже есть в списке. Откройте существующую карточку — она не перезаписана.", 409)
                row = (await self.api.rows(conn, "INSERT INTO users(tg_id,name,active,created_at) VALUES (:tg,:name,TRUE,:now) RETURNING id",
                    tg=payload["telegramId"], name=payload["name"], now=datetime.now(timezone.utc).replace(tzinfo=None)))[0]
                await self.assignments(conn, row["id"], payload)
                await self.api.audit_write(conn, actor, "employee_created", "user", row["id"],
                    json.dumps({"source": "miniapp", "name": payload["name"], "roles": payload["roles"], "hotelIds": payload["hotelIds"]}, ensure_ascii=False))
                result = await self.card(conn, row["id"])
        except IntegrityError as exc:
            raise AccessError("Карточка уже создана другим запросом. Обновите список.", 409) from exc
        return web.json_response({"employee": result}, status=201)

    async def update(self, request):
        actor = request["miniapp_actor"]
        check_editor(actor["roles"])
        uid = positive_id(request.match_info["id"])
        payload = staff_payload(await self.api.body(request), editing=True)
        async with self.engine.begin() as conn:
            roles = await self.current_editor(conn, actor["id"], uid)
            before = await self.card(conn, uid)
            check_editor(roles, before["roles"], self_edit=uid == actor["id"])
            if "ADMIN" in payload["roles"] and "OWNER" not in roles:
                raise AccessError("Назначать администратора может только владелец.")
            if payload["revision"] != before["revision"]:
                raise AccessError("Карточка уже изменена. Закройте форму и откройте её заново.", 409)
            if payload["active"] != before["active"]:
                raise AccessError(
                    "Для изменения статуса используйте «Уволить» или «Восстановить».",
                    409,
                )
            await self.verify_hotels(conn, payload["hotelIds"])
            await conn.execute(
                text("UPDATE users SET name=:name WHERE id=:id"),
                {"id": uid, "name": payload["name"]},
            )
            await self.assignments(conn, uid, payload)
            after = await self.card(conn, uid)
            if before["revision"] != after["revision"]:
                fields = ("name", "active", "roles", "hotelIds")
                await self.api.audit_write(conn, actor, "miniapp_employee_updated", "user", uid,
                    json.dumps({"before": {k: before[k] for k in fields}, "after": {k: after[k] for k in fields}}, ensure_ascii=False))
        return web.json_response({"employee": after})

    async def fire(self, request):
        actor = request["miniapp_actor"]
        check_editor(actor["roles"])
        uid = positive_id(request.match_info["id"])
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with self.engine.begin() as conn:
            roles = await self.current_editor(conn, actor["id"], uid)
            before = await self.card(conn, uid)
            check_editor(roles, before["roles"], self_edit=uid == actor["id"])
            if not before["active"]:
                raise AccessError("Сотрудник уже находится в уволенных.", 409)
            assigned = await self.api.rows(
                conn,
                """SELECT id FROM bookings
                   WHERE photographer_id=:uid
                     AND status IN (
                       'NEW','PENDING_CONFIRMATION','CONFIRMED','ASSIGNED','RESCHEDULED'
                     )
                   ORDER BY id FOR UPDATE""",
                uid=uid,
            )
            await conn.execute(
                text("UPDATE users SET active=FALSE,terminated_at=:now WHERE id=:id"),
                {"id": uid, "now": now},
            )
            for row in assigned:
                await conn.execute(
                    text(
                        "UPDATE bookings SET photographer_id=NULL,status='CONFIRMED' "
                        "WHERE id=:id"
                    ),
                    {"id": row["id"]},
                )
                await conn.execute(
                    text(
                        """UPDATE shootings SET status='CONFIRMED'
                           WHERE booking_id=:id
                             AND status IN ('ASSIGNED','PENDING_CONFIRMATION')"""
                    ),
                    {"id": row["id"]},
                )
            await self.api.audit_write(
                conn, actor, "employee_fired", "user", uid,
                json.dumps({"source": "miniapp"}, ensure_ascii=False),
            )
            after = await self.card(conn, uid)
        try:
            await self.bot.send_message(
                after["telegramId"],
                "⛔ Ваш рабочий доступ к Photo Boss отключён.",
                disable_notification=False,
            )
        except TelegramAPIError:
            pass
        return web.json_response({"employee": after})

    async def restore(self, request):
        actor = request["miniapp_actor"]
        check_editor(actor["roles"])
        uid = positive_id(request.match_info["id"])
        async with self.engine.begin() as conn:
            roles = await self.current_editor(conn, actor["id"], uid)
            before = await self.card(conn, uid)
            check_editor(roles, before["roles"], self_edit=uid == actor["id"])
            if before["active"]:
                raise AccessError("Сотрудник уже работает.", 409)
            await conn.execute(
                text("UPDATE users SET active=TRUE,terminated_at=NULL WHERE id=:id"),
                {"id": uid},
            )
            await self.api.audit_write(
                conn, actor, "employee_restored", "user", uid,
                json.dumps({"source": "miniapp"}, ensure_ascii=False),
            )
            after = await self.card(conn, uid)
        try:
            await self.bot.send_message(
                after["telegramId"],
                "♻️ Доступ к Photo Boss восстановлен. Откройте бот и нажмите /start.",
                disable_notification=False,
            )
        except TelegramAPIError:
            pass
        return web.json_response({"employee": after})

    async def set_screen_capture(self, request):
        actor = request["miniapp_actor"]
        if "OWNER" not in actor["roles"]:
            raise AccessError("Разрешение на скриншоты может менять только владелец.", 403)
        uid = positive_id(request.match_info["id"])
        body = await self.api.body(request)
        if set(body) != {"allowed"} or type(body["allowed"]) is not bool:
            raise AccessError("Передайте только признак allowed.", 400)
        async with self.engine.begin() as conn:
            await self.current_editor(conn, actor["id"])
            target = await self.card(conn, uid)
            key = f"miniapp:screen_capture:{uid}"
            await conn.execute(
                text("""INSERT INTO settings (key,value) VALUES (:key,:value)
                    ON CONFLICT (key) DO UPDATE SET value=excluded.value"""),
                {"key": key, "value": "1" if body["allowed"] else "0"},
            )
            await self.api.audit_write(
                conn,
                actor,
                "miniapp_screen_capture_changed",
                "user",
                uid,
                json.dumps(
                    {
                        "employee": target["name"],
                        "allowed": body["allowed"],
                        "source": "owner",
                    },
                    ensure_ascii=False,
                ),
            )
            updated = await self.card(conn, uid)
        return web.json_response({"employee": updated})

    async def documents(self, request):
        return web.json_response({"version": DOCUMENT_VERSION, "status": "draft", "canSign": False,
            "canAcceptWorkRules": True,
            "general": GENERAL_RULES, "sha256": hashlib.sha256(GENERAL_RULES.encode()).hexdigest(),
            "servicesContract": "Не подготовлен: нужны реквизиты заказчика, страна, статус исполнителя, условия и способ подписания.",
            "dataConsent": "Отдельный документ при необходимости. Пользовательские правила не заменяют согласие на обработку данных.",
            "chat": "Рабочий чат подключён. Общий чат и личные рабочие диалоги доступны после принятия общих правил; сообщения хранятся до 365 дней.",
            "storage": "Яндекс.Диск подключён к рабочему контуру Photo Boss.",
            "aiReview": "Автоматический ИИ-разбор в этом выпуске ещё не подключён."})

    async def static(self, request):
        name = request.match_info["asset"]
        if name not in {"people.js", "people.css"}:
            raise web.HTTPNotFound()
        return web.FileResponse(Path(__file__).parent / "people_ui" / name,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def install_people(app, miniapp):
    service = People(miniapp)
    app.router.add_get("/api/miniapp/people", service.listing)
    app.router.add_post("/api/miniapp/people", service.create)
    app.router.add_put("/api/miniapp/people/{id}", service.update)
    app.router.add_post("/api/miniapp/people/{id}/fire", service.fire)
    app.router.add_post("/api/miniapp/people/{id}/restore", service.restore)
    app.router.add_put("/api/miniapp/people/{id}/screen-capture", service.set_screen_capture)
    app.router.add_get("/api/miniapp/documents", service.documents)
    app.router.add_get("/people/{asset}", service.static)
    return service

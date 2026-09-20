"""Authenticated internal work chat for the Photo Boss Mini App."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiohttp import web
from sqlalchemy import text

from .miniapp_security import AccessError
from .work_rules import WORK_RULES_TEXT, WORK_RULES_VERSION, work_rules_hash

RETENTION_DAYS = 365
MAX_MESSAGE = 2000
RATE_LIMIT_PER_MINUTE = 30


def positive_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise AccessError("Некорректный идентификатор.", 400)
    raw = str(value)
    if not raw.isascii() or not raw.isdecimal():
        raise AccessError("Некорректный идентификатор.", 400)
    result = int(raw)
    if not 0 < result < 2**31:
        raise AccessError("Некорректный идентификатор.", 400)
    return result


def cursor_id(value):
    if value in (None, "", 0, "0"):
        return 0
    return positive_id(value)


def iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def clean_message(value):
    if not isinstance(value, str):
        raise AccessError("Введите сообщение.", 400)
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value or len(value) > MAX_MESSAGE:
        raise AccessError(f"Сообщение должно содержать от 1 до {MAX_MESSAGE} символов.", 400)
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in value):
        raise AccessError("Сообщение содержит недопустимые символы.", 400)
    return value


class WorkChat:
    def __init__(self, miniapp):
        self.api = miniapp
        self.engine = miniapp.engine
        self.static_dir = Path(__file__).parent / "work_chat_ui"

    async def acceptance(self, conn, user_id):
        rows = await self.api.rows(
            conn,
            """SELECT version,text_sha256,accepted_at
               FROM work_rule_acceptances
               WHERE user_id=:uid AND version=:version
               ORDER BY id DESC LIMIT 1""",
            uid=user_id,
            version=WORK_RULES_VERSION,
        )
        if not rows:
            return None
        row = rows[0]
        return row if row["text_sha256"] == work_rules_hash() else None

    async def require_rules(self, conn, actor):
        if await self.acceptance(conn, actor["id"]) is None:
            raise AccessError("Перед использованием рабочего чата подтвердите общие правила Photo Boss.", 428)

    async def rules(self, request):
        actor = request["miniapp_actor"]
        async with self.engine.connect() as conn:
            accepted = await self.acceptance(conn, actor["id"])
        return web.json_response({
            "version": WORK_RULES_VERSION,
            "sha256": work_rules_hash(),
            "text": WORK_RULES_TEXT,
            "accepted": bool(accepted),
            "acceptedAt": iso(accepted["accepted_at"]) if accepted else None,
            "retentionDays": RETENTION_DAYS,
        })

    async def accept_rules(self, request):
        actor = request["miniapp_actor"]
        body = await self.api.body(request)
        if set(body) != {"version", "sha256"}:
            raise AccessError("Откройте правила заново.", 400)
        if body["version"] != WORK_RULES_VERSION or body["sha256"] != work_rules_hash():
            raise AccessError("Редакция правил изменилась. Прочитайте актуальный текст.", 409)
        now = datetime.now(UTC).replace(tzinfo=None)
        async with self.engine.begin() as conn:
            await conn.execute(
                text("""INSERT INTO work_rule_acceptances
                    (user_id,version,text_sha256,accepted_at)
                    VALUES (:uid,:version,:sha,:now)
                    ON CONFLICT (user_id,version)
                    DO UPDATE SET text_sha256=excluded.text_sha256,
                                  accepted_at=excluded.accepted_at"""),
                {"uid": actor["id"], "version": WORK_RULES_VERSION,
                 "sha": work_rules_hash(), "now": now},
            )
            await self.api.audit_write(
                conn, actor, "work_rules_accepted", "user", actor["id"],
                json.dumps({"version": WORK_RULES_VERSION, "sha256": work_rules_hash()},
                           ensure_ascii=False),
            )
        return web.json_response({"ok": True, "acceptedAt": now.isoformat()})

    async def people(self, request):
        actor = request["miniapp_actor"]
        async with self.engine.connect() as conn:
            await self.require_rules(conn, actor)
            users = await self.api.rows(
                conn,
                """SELECT id,name FROM users
                   WHERE active=TRUE AND id<>:uid
                   ORDER BY name,id LIMIT 500""",
                uid=actor["id"],
            )
            roles = await self.api.rows(
                conn,
                """SELECT user_id,role FROM user_roles
                   WHERE user_id IN (SELECT id FROM users WHERE active=TRUE)""",
            )
        role_map = {}
        for row in roles:
            role_map.setdefault(row["user_id"], []).append(row["role"])
        return web.json_response({
            "general": {"id": "general", "name": "Общий чат"},
            "people": [{"id": row["id"], "name": row["name"],
                        "roles": sorted(role_map.get(row["id"], []))}
                       for row in users],
            "ownerControl": "OWNER" in actor["roles"],
        })

    async def _messages(self, conn, *, actor_id, peer_id=None, after=0):
        params = {"uid": actor_id, "after": after}
        if peer_id is None:
            clause = "m.recipient_id IS NULL"
        else:
            params["peer"] = peer_id
            clause = """((m.sender_id=:uid AND m.recipient_id=:peer)
                      OR (m.sender_id=:peer AND m.recipient_id=:uid))"""
        return await self.api.rows(
            conn,
            f"""SELECT m.id,m.sender_id,m.recipient_id,m.body,m.created_at,u.name AS sender_name
                FROM work_chat_messages m
                JOIN users u ON u.id=m.sender_id
                WHERE {clause} AND m.id>:after
                ORDER BY m.id ASC LIMIT 100""",
            **params,
        )

    def pack_messages(self, rows):
        return [{
            "id": row["id"],
            "senderId": row["sender_id"],
            "recipientId": row["recipient_id"],
            "body": row["body"],
            "createdAt": iso(row["created_at"]),
            "senderName": row["sender_name"],
        } for row in rows]

    async def messages(self, request):
        actor = request["miniapp_actor"]
        peer_raw = request.query.get("peer", "general")
        peer_id = None if peer_raw == "general" else positive_id(peer_raw)
        after = cursor_id(request.query.get("after"))
        if peer_id == actor["id"]:
            raise AccessError("Нельзя открыть диалог с самим собой.", 400)
        async with self.engine.connect() as conn:
            await self.require_rules(conn, actor)
            if peer_id is not None:
                person = await self.api.rows(conn, "SELECT id,name FROM users WHERE id=:id", id=peer_id)
                if not person:
                    raise AccessError("Сотрудник не найден.", 404)
            rows = await self._messages(conn, actor_id=actor["id"], peer_id=peer_id, after=after)
        return web.json_response({"messages": self.pack_messages(rows)})

    async def send(self, request):
        actor = request["miniapp_actor"]
        body = await self.api.body(request)
        if set(body) != {"peerId", "body"}:
            raise AccessError("Некорректное сообщение.", 400)
        message = clean_message(body["body"])
        peer_id = None if body["peerId"] is None else positive_id(body["peerId"])
        if peer_id == actor["id"]:
            raise AccessError("Нельзя отправить сообщение самому себе.", 400)
        now = datetime.now(UTC).replace(tzinfo=None)
        minute_ago = now - timedelta(minutes=1)
        async with self.engine.begin() as conn:
            await self.require_rules(conn, actor)
            if peer_id is not None:
                target = await self.api.rows(
                    conn, "SELECT id FROM users WHERE id=:id AND active=TRUE", id=peer_id
                )
                if not target:
                    raise AccessError("Сотрудник сейчас недоступен.", 409)
            count = await self.api.rows(
                conn,
                """SELECT COUNT(*) AS n FROM work_chat_messages
                   WHERE sender_id=:uid AND created_at>=:since""",
                uid=actor["id"], since=minute_ago,
            )
            if int(count[0]["n"] or 0) >= RATE_LIMIT_PER_MINUTE:
                raise AccessError("Слишком много сообщений. Повторите через минуту.", 429)
            rows = await self.api.rows(
                conn,
                """INSERT INTO work_chat_messages(sender_id,recipient_id,body,created_at)
                   VALUES (:sender,:recipient,:body,:created)
                   RETURNING id,created_at""",
                sender=actor["id"], recipient=peer_id, body=message, created=now,
            )
        return web.json_response({
            "message": {"id": rows[0]["id"], "senderId": actor["id"],
                        "recipientId": peer_id, "body": message,
                        "createdAt": iso(rows[0]["created_at"]),
                        "senderName": actor["name"]},
        }, status=201)

    async def owner_threads(self, request):
        actor = request["miniapp_actor"]
        if "OWNER" not in actor["roles"]:
            raise AccessError("Контроль рабочих диалогов доступен только владельцу.")
        async with self.engine.connect() as conn:
            await self.require_rules(conn, actor)
            rows = await self.api.rows(
                conn,
                """SELECT m.id,m.sender_id,m.recipient_id,m.body,m.created_at,
                          s.name AS sender_name,r.name AS recipient_name
                   FROM work_chat_messages m
                   JOIN users s ON s.id=m.sender_id
                   JOIN users r ON r.id=m.recipient_id
                   WHERE m.recipient_id IS NOT NULL
                     AND m.sender_id<>:owner AND m.recipient_id<>:owner
                   ORDER BY m.id DESC LIMIT 1000""",
                owner=actor["id"],
            )
        seen = set()
        threads = []
        for row in rows:
            pair = tuple(sorted((row["sender_id"], row["recipient_id"])))
            if pair in seen:
                continue
            seen.add(pair)
            a_id, b_id = pair
            a_name = row["sender_name"] if row["sender_id"] == a_id else row["recipient_name"]
            b_name = row["recipient_name"] if row["recipient_id"] == b_id else row["sender_name"]
            threads.append({
                "a": {"id": a_id, "name": a_name},
                "b": {"id": b_id, "name": b_name},
                "last": row["body"][:120],
                "createdAt": iso(row["created_at"]),
            })
            if len(threads) >= 100:
                break
        return web.json_response({"threads": threads})

    async def owner_messages(self, request):
        actor = request["miniapp_actor"]
        if "OWNER" not in actor["roles"]:
            raise AccessError("Контроль рабочих диалогов доступен только владельцу.")
        a_id, b_id = positive_id(request.query.get("a")), positive_id(request.query.get("b"))
        if a_id == b_id or actor["id"] in {a_id, b_id}:
            raise AccessError("Некорректный диалог.", 400)
        after = positive_id(request.query["after"]) if "after" in request.query else 0
        async with self.engine.begin() as conn:
            await self.require_rules(conn, actor)
            people = await self.api.rows(
                conn, "SELECT id,name FROM users WHERE id IN (:a,:b) ORDER BY id", a=a_id, b=b_id
            )
            if len(people) != 2:
                raise AccessError("Диалог не найден.", 404)
            rows = await self.api.rows(
                conn,
                """SELECT m.id,m.sender_id,m.recipient_id,m.body,m.created_at,u.name AS sender_name
                   FROM work_chat_messages m
                   JOIN users u ON u.id=m.sender_id
                   WHERE ((m.sender_id=:a AND m.recipient_id=:b)
                      OR (m.sender_id=:b AND m.recipient_id=:a))
                     AND m.id>:after
                   ORDER BY m.id ASC LIMIT 100""",
                a=a_id, b=b_id, after=after,
            )
            if after == 0:
                await self.api.audit_write(
                    conn, actor, "work_chat_owner_thread_opened", "user", a_id,
                    json.dumps({"participants": [a_id, b_id]}, ensure_ascii=False),
                )
        return web.json_response({
            "participants": [{"id": p["id"], "name": p["name"]} for p in people],
            "messages": self.pack_messages(rows),
            "readOnly": True,
        })

    async def static(self, request):
        name = request.match_info["asset"]
        if name not in {"chat.js", "chat.css"}:
            raise web.HTTPNotFound()
        return web.FileResponse(
            self.static_dir / name,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )


async def cleanup_expired_chat(engine):
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=RETENTION_DAYS)
    async with engine.begin() as conn:
        result = await conn.execute(
            text("DELETE FROM work_chat_messages WHERE created_at<:cutoff"),
            {"cutoff": cutoff},
        )
    return int(result.rowcount or 0)


async def cleanup_loop(engine):
    while True:
        await cleanup_expired_chat(engine)
        await asyncio.sleep(24 * 60 * 60)


def install_work_chat(app, miniapp):
    service = WorkChat(miniapp)
    app["work_chat"] = service
    app.router.add_get("/api/miniapp/chat/rules", service.rules)
    app.router.add_post("/api/miniapp/chat/rules/accept", service.accept_rules)
    app.router.add_get("/api/miniapp/chat/people", service.people)
    app.router.add_get("/api/miniapp/chat/messages", service.messages)
    app.router.add_post("/api/miniapp/chat/messages", service.send)
    app.router.add_get("/api/miniapp/chat/owner/threads", service.owner_threads)
    app.router.add_get("/api/miniapp/chat/owner/messages", service.owner_messages)
    app.router.add_get("/work-chat/{asset}", service.static)
    return service

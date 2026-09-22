"""Small-team WebRTC signaling. Media never passes through or is recorded by the bot.

Ephemeral rooms intentionally expire on process restart. The current Render service
runs one process/instance; horizontal scaling requires a shared signaling store.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass, field

from aiogram.exceptions import TelegramAPIError
from aiohttp import web
from sqlalchemy.exc import SQLAlchemyError

from .miniapp_security import AccessError
from .models import utc_now
from .work_chat import positive_id

MAX_PARTICIPANTS = 6
HEARTBEAT_TTL = 60
RING_TTL = 90
MAX_DURATION = 2 * 60 * 60
MAX_ROOMS = 128
MAX_SIGNALS = 256
logger = logging.getLogger(__name__)


def ice_config(user_id):
    servers = [{"urls": ["stun:stun.l.google.com:19302", "stun:stun.cloudflare.com:3478"]}]
    urls = [v.strip() for v in os.getenv("CALLS_TURN_URLS", "").split(",") if v.strip()]
    secret = os.getenv("CALLS_TURN_SECRET", "")
    username = os.getenv("CALLS_TURN_USERNAME", "")
    credential = os.getenv("CALLS_TURN_CREDENTIAL", "")
    if urls and all(v.startswith(("turn:", "turns:")) for v in urls):
        if secret:
            username = f"{int(time.time()) + MAX_DURATION + 300}:pb-{user_id}"
            credential = base64.b64encode(hmac.new(
                secret.encode(), username.encode(), hashlib.sha1
            ).digest()).decode()
        if username and credential:
            servers.append({"urls": urls, "username": username, "credential": credential})
    return {"iceServers": servers, "relayConfigured": len(servers) > 1,
            "maxParticipants": MAX_PARTICIPANTS}


@dataclass
class Member:
    user_id: int
    name: str
    session: str
    seen: float
    signals: deque = field(default_factory=lambda: deque(maxlen=MAX_SIGNALS))
    sequence: int = 0
    audio: bool = True
    video: bool = False


@dataclass
class Room:
    id: str
    creator: int
    creator_name: str
    peer: int | None
    mode: str
    created: float
    members: dict = field(default_factory=dict)
    joined_once: bool = False
    participants: dict = field(default_factory=dict)
    connected: float | None = None


class WorkCalls:
    def __init__(self, chat, *, clock=time.monotonic):
        self.chat, self.api = chat, chat.api
        self.clock = clock
        self.rooms = {}
        self.lock = asyncio.Lock()
        self.starts = {}
        self.notifications = set()

    async def record(self, room, status, actor=None):
        payload = {"callId": room.id, "creatorId": room.creator, "creatorName": room.creator_name,
                   "peerId": room.peer, "mode": room.mode, "status": status,
                   "participants": [{"id": uid, "name": name} for uid, name in room.participants.items()],
                   "durationSeconds": int(max(0, self.clock()-room.connected)) if room.connected is not None else 0,
                   "at": utc_now().isoformat()}
        async with self.api.engine.begin() as conn:
            await self.api.audit_write(conn, actor or {"id": room.creator}, "work_call_history", "work_call", None,
                                       json.dumps(payload, ensure_ascii=False))

    async def history(self, request):
        actor, _users = await self.allowed(request)
        async with self.api.engine.connect() as conn:
            rows = await self.api.rows(conn, "SELECT details FROM audit_logs WHERE action='work_call_history' ORDER BY id DESC LIMIT 2000")
        seen, items = set(), []
        for row in rows:
            data = json.loads(row['details'])
            if data['callId'] in seen:
                continue
            seen.add(data['callId'])
            uid = actor['id']
            if uid not in {data['creatorId'], data['peerId']} and uid not in {p['id'] for p in data['participants']}:
                continue
            if data['status'] in ('ringing', 'connected') and data['callId'] not in self.rooms:
                data['status'] = 'interrupted'
            items.append(data)
            if len(items) == 50:
                break
        return web.json_response({'items': items})

    async def notify_start(self, room, actor):
        try:
            async with self.api.engine.connect() as conn:
                targets = await self.chat.notification_targets(conn, actor, room.peer)
            label = "Групповой звонок" if room.peer is None else "Видеозвонок" if room.mode == "video" else "Аудиозвонок"
            message = f"☎ Photo Boss · {label}\n{actor['name']} приглашает в звонок.\nОткройте Photo Boss и нажмите «Ответить»."
            async with asyncio.timeout(15):
                # Use the existing chat delivery policy and protect message content.
                for tg in targets:
                    if room.id not in self.rooms:
                        break
                    await self.chat._notify_one(tg, message)
        except (TimeoutError, OSError, SQLAlchemyError, TelegramAPIError):
            logger.warning("Call invitation delivery unavailable", exc_info=False)

    async def allowed(self, request):
        actor = request["miniapp_actor"]
        async with self.api.engine.connect() as conn:
            await self.chat.require_rules(conn, actor)
            rows = await self.api.rows(conn, """SELECT DISTINCT u.id,u.name FROM users u
                JOIN user_roles r ON r.user_id=u.id WHERE u.active=TRUE
                AND r.role IN ('OWNER','ADMIN','MANAGER','PHOTOGRAPHER')""")
        users = {r["id"]: r["name"] for r in rows}
        if actor["id"] not in users:
            raise AccessError("Аккаунт сотрудника не активен.")
        return actor, users

    async def prune(self, users):
        now = self.clock()
        for rid, room in list(self.rooms.items()):
            for uid, member in list(room.members.items()):
                if uid not in users or now - member.seen > HEARTBEAT_TTL:
                    del room.members[uid]
            ended = (not room.members or now - room.created > MAX_DURATION
                     or (not room.joined_once and now - room.created > RING_TTL)
                     or (room.peer is not None and room.joined_once and len(room.members) < 2))
            if ended:
                await self.record(room, "completed" if room.joined_once else "missed")
                del self.rooms[rid]
        self.starts = {uid: q for uid, q in self.starts.items() if q and now - q[-1] < 60}

    def visible(self, room, uid):
        return room.peer is None or uid in (room.creator, room.peer)

    def room_for(self, rid, uid):
        room = self.rooms.get(rid) if isinstance(rid, str) else None
        if room is None or not self.visible(room, uid):
            raise AccessError("Звонок завершён или недоступен.", 404)
        return room

    def summary(self, room):
        return {"id": room.id, "creatorId": room.creator, "creatorName": room.creator_name,
                "peerId": room.peer, "mode": room.mode, "group": room.peer is None,
                "participants": [{"id": m.user_id, "name": m.name, "session": m.session,
                                  "audio": m.audio, "video": m.video}
                                 for m in room.members.values()]}

    def busy(self, uid, except_id=None):
        return any(uid in r.members and r.id != except_id for r in self.rooms.values())

    def add_member(self, room, actor, mode):
        uid = actor["id"]
        if uid in room.members:
            raise AccessError("Вы уже подключены к звонку в другом окне.", 409)
        if self.busy(uid):
            raise AccessError("Сначала завершите текущий звонок.", 409)
        if len(room.members) >= MAX_PARTICIPANTS:
            raise AccessError(f"В звонке уже {MAX_PARTICIPANTS} участников.", 409)
        member = Member(uid, actor["name"], secrets.token_urlsafe(24), self.clock(),
                        video=mode == "video")
        room.members[uid] = member
        room.participants[uid] = actor["name"]
        if len(room.members) >= 2:
            room.joined_once = True
            if room.connected is None:
                room.connected = self.clock()
        return member

    async def list_calls(self, request):
        actor, users = await self.allowed(request)
        async with self.lock:
            await self.prune(users)
            rooms = [self.summary(r) for r in self.rooms.values() if self.visible(r, actor["id"])]
        return web.json_response({"calls": rooms, "maxParticipants": MAX_PARTICIPANTS})

    async def start(self, request):
        actor, users = await self.allowed(request)
        body = await self.api.body(request)
        if set(body) != {"peerId", "mode"} or body["mode"] not in ("audio", "video"):
            raise AccessError("Выберите аудио- или видеозвонок.", 400)
        peer = body["peerId"]
        if peer is not None:
            peer = positive_id(peer)
            if peer == actor["id"] or peer not in users:
                raise AccessError("Сотрудник сейчас недоступен.", 409)
        async with self.lock:
            await self.prune(users)
            if self.busy(actor["id"]):
                raise AccessError("Сначала завершите текущий звонок.", 409)
            existing = next((r for r in self.rooms.values() if
                             (peer is None and r.peer is None) or
                             (peer is not None and r.peer is not None and
                              {actor["id"], peer} == {r.creator, r.peer})), None)
            if existing:
                raise AccessError("Звонок уже идёт. Нажмите «Присоединиться».", 409)
            if peer is not None and self.busy(peer):
                raise AccessError("Сотрудник сейчас в другом звонке.", 409)
            recent = self.starts.setdefault(actor["id"], deque(maxlen=5))
            if len(recent) == 5 and self.clock() - recent[0] < 60:
                raise AccessError("Слишком много вызовов. Подождите минуту.", 429)
            if len(self.rooms) >= MAX_ROOMS:
                raise AccessError("Сервис звонков занят. Повторите позже.", 503)
            room = Room(secrets.token_urlsafe(18), actor["id"], actor["name"], peer,
                        body["mode"], self.clock())
            member = self.add_member(room, actor, body["mode"])
            await self.record(room, "ringing", actor)
            self.rooms[room.id] = room
            recent.append(self.clock())
            result = {"call": self.summary(room), "session": member.session, **ice_config(actor["id"])}
        task = asyncio.create_task(self.notify_start(room, actor))
        self.notifications.add(task)
        task.add_done_callback(self.notifications.discard)
        return web.json_response(result, status=201)

    async def join(self, request):
        actor, users = await self.allowed(request)
        body = await self.api.body(request)
        if set(body) != {"callId", "mode"} or body["mode"] not in ("audio", "video"):
            raise AccessError("Некорректный запрос подключения.", 400)
        async with self.lock:
            await self.prune(users)
            room = self.room_for(body["callId"], actor["id"])
            member = self.add_member(room, actor, body["mode"])
            await self.record(room, "connected", actor)
            result = {"call": self.summary(room), "session": member.session, **ice_config(actor["id"])}
        return web.json_response(result)

    def member_for(self, room, actor, body):
        member = room.members.get(actor["id"])
        session = body.get("session")
        if member is None or not isinstance(session, str) or not secrets.compare_digest(session, member.session):
            raise AccessError("Подключение к звонку завершено.", 409)
        return member

    async def sync(self, request):
        actor, users = await self.allowed(request)
        body = await self.api.body(request)
        if set(body) != {"callId", "session", "after", "audio", "video"}:
            raise AccessError("Некорректный запрос звонка.", 400)
        after = body["after"]
        if type(after) is not int or after < 0 or any(type(body[k]) is not bool for k in ("audio", "video")):
            raise AccessError("Некорректный запрос звонка.", 400)
        async with self.lock:
            await self.prune(users)
            room = self.room_for(body["callId"], actor["id"])
            member = self.member_for(room, actor, body)
            if after > member.sequence or (member.signals and after < member.signals[0]["seq"] - 1):
                raise AccessError("Связь была прервана. Подключитесь к звонку заново.", 409)
            member.seen = self.clock()
            member.audio, member.video = body["audio"], body["video"]
            while member.signals and member.signals[0]["seq"] <= after:
                member.signals.popleft()
            signals = [s for s in member.signals if s["seq"] > after]
            result = {"call": self.summary(room), "signals": signals, "cursor": member.sequence}
        return web.json_response(result)

    async def signal(self, request):
        actor, users = await self.allowed(request)
        # SDP can exceed the general Mini App 8KB body limit. Bound before decoding.
        raw = bytearray()
        async for chunk in request.content.iter_chunked(8192):
            raw.extend(chunk)
            if len(raw) > 65536:
                raise AccessError("Слишком большой запрос.", 413)
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or set(body) != {"callId", "session", "to", "toSession", "data"}:
                raise ValueError
            data = body["data"]
            if not isinstance(data, dict) or data.get("type") not in ("offer", "answer", "candidate"):
                raise ValueError
            if data["type"] in ("offer", "answer"):
                if set(data) != {"type", "sdp"} or not isinstance(data["sdp"], str) or len(data["sdp"]) > 60000:
                    raise ValueError
            elif set(data) != {"type", "candidate"} or not isinstance(data["candidate"], dict) or len(json.dumps(data)) > 4096:
                raise ValueError
        except (ValueError, TypeError, UnicodeDecodeError):
            raise AccessError("Некорректный сигнал звонка.", 400) from None
        target_id = positive_id(body["to"])
        async with self.lock:
            await self.prune(users)
            room = self.room_for(body["callId"], actor["id"])
            member = self.member_for(room, actor, body)
            target = room.members.get(target_id)
            if target is None or target_id == actor["id"] or target.session != body["toSession"]:
                raise AccessError("Участник отключился.", 409)
            # Bound unacknowledged signals; do not allow a peer to exhaust memory.
            if target.signals and len(target.signals) == MAX_SIGNALS and self.clock() - target.signals[0]["at"] < 10:
                raise AccessError("Слишком много сигналов.", 429)
            if sum(len(json.dumps(s["data"])) for s in target.signals) + len(raw) > 512000:
                raise AccessError("Слишком много сигналов.", 429)
            target.sequence += 1
            target.signals.append({"seq": target.sequence, "from": actor["id"],
                                   "session": member.session, "data": data, "at": self.clock()})
        return web.json_response({"ok": True})

    async def leave(self, request):
        actor, users = await self.allowed(request)
        body = await self.api.body(request)
        if set(body) != {"callId", "session", "endForAll"} or type(body["endForAll"]) is not bool:
            raise AccessError("Некорректный запрос завершения.", 400)
        async with self.lock:
            await self.prune(users)
            room = self.room_for(body["callId"], actor["id"])
            self.member_for(room, actor, body)
            if body["endForAll"] and room.creator != actor["id"]:
                raise AccessError("Завершить групповой звонок может его создатель.")
            del room.members[actor["id"]]
            if not room.members or room.peer is not None or body["endForAll"]:
                await self.record(room, "completed" if room.joined_once else "cancelled", actor)
                del self.rooms[room.id]
        return web.json_response({"ok": True})

    async def decline(self, request):
        actor, users = await self.allowed(request)
        body = await self.api.body(request)
        if set(body) != {"callId"}:
            raise AccessError("Некорректный запрос.", 400)
        async with self.lock:
            await self.prune(users)
            room = self.room_for(body["callId"], actor["id"])
            if room.peer != actor["id"] or actor["id"] in room.members:
                raise AccessError("Этот вызов нельзя отклонить.")
            await self.record(room, "declined", actor)
            del self.rooms[room.id]
        return web.json_response({"ok": True})


def install_work_calls(app, chat):
    service = WorkCalls(chat)
    app["work_calls"] = service
    app.router.add_get("/api/miniapp/chat/calls", service.list_calls)
    app.router.add_get("/api/miniapp/chat/calls/history", service.history)
    for name in ("start", "join", "sync", "signal", "leave", "decline"):
        app.router.add_post(f"/api/miniapp/chat/calls/{name}", getattr(service, name))
    async def cleanup(_app):
        tasks = list(service.notifications)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        service.rooms.clear()
    app.on_cleanup.append(cleanup)
    return service

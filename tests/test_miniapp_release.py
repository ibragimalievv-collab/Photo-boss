"""Isolated release tests. No Telegram calls or production database access."""
import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web
from multidict import MultiDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.miniapp_api import MiniApp
from app.miniapp_security import AccessError, financial_period, validate_init_data, webhook_secret
from app.release_policy import LegacyFinanceGuard, app_url, legacy_destination
from app.services.core import menu

TOKEN = "123456:CI_ONLY_DO_NOT_USE_FOR_TELEGRAM"


def signed(uid=1003, **extra):
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid}), **extra}
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_valid_signed_user():
    assert validate_init_data(signed(), TOKEN) == 1003
    assert validate_init_data(signed(signature="additional-signed-field"), TOKEN) == 1003


@pytest.mark.parametrize("raw", ["", "user=1003", "hash=abc", "a=1&a=2", "x=" + "a" * 17000])
def test_bad_init_data(raw):
    with pytest.raises(AccessError):
        validate_init_data(raw, TOKEN)


@pytest.mark.parametrize("raw", [signed(auth_date="1"), signed(auth_date="9999999999"), signed(uid=True)])
def test_expired_future_non_integer_uid(raw):
    with pytest.raises(AccessError):
        validate_init_data(raw, TOKEN)


def test_forged_signature_and_duplicate_fields():
    with pytest.raises(AccessError):
        validate_init_data(signed().replace("1003", "1001"), TOKEN)
    with pytest.raises(AccessError):
        validate_init_data(signed() + "&auth_date=1", TOKEN)
    assert webhook_secret(TOKEN) != TOKEN
    assert webhook_secret(TOKEN) == webhook_secret(TOKEN)


@pytest.mark.parametrize("period", ["week", "month", "custom"])
def test_admin_period_guard(period):
    with pytest.raises(AccessError):
        financial_period({"ADMIN"}, period, date(2026, 9, 20))


@pytest.mark.parametrize("button", ["💰 Продажи", "📊 Отчёты", "💵 Зарплаты/выплаты"])
def test_legacy_finance_links_route_to_today_only_api(button):
    assert legacy_destination({"ADMIN"}, text=button) == "finance"
    assert legacy_destination({"OWNER"}, text=button) is None


@pytest.mark.parametrize("callback", [
    "admin:report:period:month", "admin:report:date:2025-01-01",
    "admin:sales:day:0:2025-01-01", "admin:sales:period:0:week",
    "admin:sales:search:0", "admin:report:page:7",
])
def test_old_forged_history_buttons_blocked(callback):
    assert legacy_destination({"ADMIN"}, callback=callback) == "finance"


def test_audit_owner_and_training_menu():
    assert legacy_destination({"ADMIN"}, text="📜 Аудит") == "denied"
    assert legacy_destination({"OWNER"}, text="📜 Аудит") == "audit"
    assert legacy_destination({"ADMIN"}, mode="report") == "finance"
    assert legacy_destination({"ADMIN"}, mode="bookings") == "shootings"
    for roles in [{"OWNER"}, {"ADMIN"}, {"MANAGER"}, {"PHOTOGRAPHER"}]:
        items = menu(roles)
        assert "📱 Приложение" in items and "📚 Академия" in items
        assert "🎓 Обучение" not in items
        assert ("📜 Аудит" in items) == ("OWNER" in roles)


def test_app_origin_validation(monkeypatch):
    monkeypatch.setenv("WEBHOOK_BASE_URL", "https://example.test")
    assert app_url("academy") == "https://example.test/app/#academy"
    for url in ["http://example.test", "https://user:secret@example.test", "https://example.test/path"]:
        monkeypatch.setenv("WEBHOOK_BASE_URL", url)
        with pytest.raises(ValueError):
            app_url()


def test_inner_middleware_never_calls_old_audit_handler():
    async def check():
        replies = []

        async def reply(message, **kwargs):
            replies.append(message)

        async def forbidden(*args):
            raise AssertionError("Legacy audit query must not run")

        event = SimpleNamespace(text="📜 Аудит", answer=reply)
        await LegacyFinanceGuard()(forbidden, event, {"current_roles": {"ADMIN"}})
        assert replies == ["Аудит доступен только владельцу."]
    asyncio.run(check())


class Bot:
    token = TOKEN

    def __init__(self):
        self.sent = []

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))

    async def me(self):
        return SimpleNamespace(username="unit_test_bot")


class Body:
    def __init__(self, data):
        self.data, self.offset = data, 0

    def at_eof(self):
        return self.offset >= len(self.data)

    async def read(self, length):
        chunk = self.data[self.offset:self.offset + min(length, 13)]
        self.offset += len(chunk)
        return chunk


class Request(dict):
    def __init__(self, path, uid=1001, body=None, credential=None):
        super().__init__()
        url = urlsplit(path)
        self.path, self.query = url.path, MultiDict(parse_qsl(url.query))
        raw = json.dumps(body or {}).encode()
        self.content, self.content_length = Body(raw), len(raw)
        self.headers = {"X-Telegram-Init-Data": signed(uid) if credential is None else credential}
        self.match_info = {}


class TestEngine:
    __test__ = False

    def __init__(self, engine, sqlite=False):
        self.engine, self.sqlite = engine, sqlite

    @asynccontextmanager
    async def connect(self):
        async with self.engine.connect() as conn:
            yield Connection(conn, self.sqlite)

    @asynccontextmanager
    async def begin(self):
        async with self.engine.begin() as conn:
            yield Connection(conn, self.sqlite)


class Connection:
    def __init__(self, conn, sqlite):
        self.conn, self.sqlite = conn, sqlite

    async def execute(self, stmt, params=None):
        if self.sqlite:
            stmt = text(str(stmt).replace(" FOR UPDATE", ""))
        return await self.conn.execute(stmt, params or {})


async def invoke(svc, path, handler, *, uid=1001, body=None, credential=None, match=None):
    req = Request("/api/miniapp" + path, uid, body, credential)
    req.match_info = match or {}
    response = await svc.middleware(req, handler)
    return response.status, json.loads(response.text), response.headers


async def setup_database(engine, sqlite):
    ident = "INTEGER PRIMARY KEY" if sqlite else "SERIAL PRIMARY KEY"
    schema = {
        "users": "id INTEGER PRIMARY KEY,tg_id BIGINT,name TEXT,active BOOLEAN",
        "user_roles": "user_id INTEGER,role TEXT",
        "settings": "key TEXT PRIMARY KEY,value TEXT",
        "hotels": "id INTEGER PRIMARY KEY,name TEXT,active BOOLEAN",
        "hotel_employees": "user_id INTEGER,hotel_id INTEGER",
        "clients": "id INTEGER PRIMARY KEY,name TEXT",
        "packages": "id INTEGER PRIMARY KEY,name TEXT",
        "bookings": "id INTEGER PRIMARY KEY,room TEXT,shoot_date DATE,shoot_time TEXT,status TEXT,photographer_id INTEGER,manager_id INTEGER,hotel_id INTEGER,client_id INTEGER,package_id INTEGER",
        "shootings": "id INTEGER PRIMARY KEY,booking_id INTEGER",
        "photos": "id INTEGER PRIMARY KEY,shooting_id INTEGER",
        "sales": "id INTEGER PRIMARY KEY,booking_id INTEGER,credited_user_id INTEGER,commission_role TEXT,amount NUMERIC,commission NUMERIC,payment_status TEXT,created_at TIMESTAMP",
        "payroll_entries": "id INTEGER PRIMARY KEY,user_id INTEGER,kind TEXT,amount NUMERIC,created_at TIMESTAMP",
        "receipts": "id INTEGER PRIMARY KEY,status TEXT,verified_amount NUMERIC,reviewed_at TIMESTAMP",
        "shifts": f"id {ident},user_id INTEGER,hotel_id INTEGER,start_at TIMESTAMP,end_at TIMESTAMP,status TEXT",
        "shift_check_ins": "id INTEGER PRIMARY KEY,user_id INTEGER,shift_date DATE,started_at TIMESTAMP",
        "shift_check_outs": "id INTEGER PRIMARY KEY,user_id INTEGER,shift_date DATE,ended_at TIMESTAMP",
        "audit_logs": f"id {ident},user_id INTEGER,action TEXT,entity TEXT,entity_id INTEGER,details TEXT,created_at TIMESTAMP",
        "academy_lesson_progress": f"id {ident},user_id INTEGER,topic_slug TEXT,completed_at TIMESTAMP,UNIQUE(user_id,topic_slug)",
        "training_assignments": "id INTEGER PRIMARY KEY,user_id INTEGER,category_slug TEXT,status TEXT",
        "academy_reviews": "id INTEGER PRIMARY KEY,user_id INTEGER,quality_score INTEGER,issues TEXT,recommendation TEXT",
    }
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with engine.begin() as conn:
        for table, fields in schema.items():
            await conn.execute(text(f"CREATE TABLE {table} ({fields})"))
        for uid, role in [(1, "OWNER"), (2, "ADMIN"), (3, "PHOTOGRAPHER"), (4, "MANAGER"), (5, "PHOTOGRAPHER")]:
            await conn.execute(text("INSERT INTO users VALUES (:id,:tg,:name,TRUE)"), {"id": uid, "tg": 1000 + uid, "name": f"User {uid}"})
            await conn.execute(text("INSERT INTO user_roles VALUES (:id,:role)"), {"id": uid, "role": role})
        await conn.execute(text("INSERT INTO users VALUES (6,1006,'Inactive',FALSE),(7,1007,'No role',TRUE)"))
        await conn.execute(text("INSERT INTO hotels VALUES (1,'Test hotel',TRUE),(2,'Other hotel',TRUE)"))
        await conn.execute(text("INSERT INTO clients VALUES (1,'Own guest'),(2,'Other guest')"))
        await conn.execute(text("INSERT INTO packages VALUES (1,'Family')"))
        await conn.execute(text("INSERT INTO sales VALUES (1,1,3,'PHOTOGRAPHER',21000,3150,'PAID',:now),(2,2,5,'PHOTOGRAPHER',26000,3900,'UNPAID',:now)"), {"now": now})
        await conn.execute(text("INSERT INTO payroll_entries VALUES (1,3,'Premium',1000,:now),(2,5,'Premium',1500,:now)"), {"now": now})
        await conn.execute(text("INSERT INTO receipts VALUES (1,'APPROVED',21000,:now),(2,'PENDING',26000,:now)"), {"now": now})
    return now


async def exercise(engine, sqlite):
    await setup_database(engine, sqlite)
    svc = MiniApp(TestEngine(engine, sqlite), Bot(), [SimpleNamespace(slug="light", title="Light", body="Lesson")])
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO bookings VALUES (1,'101',:day,'10:00','ASSIGNED',3,4,1,1,1),(2,'201',:day,'11:00','ASSIGNED',5,4,2,2,1)"), {"day": svc.today()})
    status, result, headers = await invoke(svc, "/me", svc.me, credential="")
    assert status == 401 and "user" not in result and headers["Cache-Control"] == "no-store"
    for uid in (1006, 1007, 9999):
        assert (await invoke(svc, "/me", svc.me, uid=uid))[0] == 403
    for path, handler in [("/finance?period=month", svc.finance), ("/finance?period=week", svc.finance), ("/finance?period=today&from=2025-01-01", svc.finance), ("/audit", svc.audit)]:
        status, result, _ = await invoke(svc, path, handler, uid=1002)
        assert status == 403 and "items" not in result and "employees" not in result
    assert (await invoke(svc, "/finance?user_id=1", svc.finance, uid=1003))[0] == 400
    assert (await invoke(svc, "/dashboard?day=2025-01-01", svc.dashboard, uid=1002))[0] == 400
    status, money, _ = await invoke(svc, "/finance", svc.finance)
    assert status == 200 and money["sales"] == 4700000
    assert money["cashReceived"] == 2100000 and money["payroll"] == 955000
    status, own, _ = await invoke(svc, "/finance?period=month", svc.finance, uid=1003)
    assert status == 200 and own["cashReceived"] is None and own["payroll"] == 415000
    assert [item["id"] for item in own["employees"]] == [3]
    _, bookings, _ = await invoke(svc, "/bookings", svc.bookings, uid=1003)
    assert [item["id"] for item in bookings["items"]] == [1]
    assert (await invoke(svc, "/preferences", svc.preferences, uid=1003, body={"theme": "premium"}))[0] == 200
    _, me, _ = await invoke(svc, "/me", svc.me, uid=1003)
    assert me["user"]["theme"] == "premium" and me["permissions"]["financeScope"] == "self"
    assert (await invoke(svc, "/preferences", svc.preferences, uid=1003, body={"theme": "premium", "role": "OWNER"}))[0] == 400
    for _ in range(2):
        assert (await invoke(svc, "/academy/lessons/light", svc.complete_lesson, uid=1003, match={"slug": "light"}))[0] == 200
    _, academy, _ = await invoke(svc, "/academy", svc.academy, uid=1003)
    assert academy["points"] == 10 and academy["completed"] == ["light"]
    _, other, _ = await invoke(svc, "/academy", svc.academy, uid=1005)
    assert not other["completed"]
    day = str(svc.today() + timedelta(days=1))
    shift = {"userId": "3", "hotelId": "1", "date": day, "start": "09:00", "end": "19:00"}
    assert (await invoke(svc, "/schedule", svc.create_shift, uid=1003, body=shift))[0] == 403
    status, created, _ = await invoke(svc, "/schedule", svc.create_shift, uid=1002, body=shift)
    assert status == 201
    assert (await invoke(svc, "/schedule", svc.create_shift, uid=1002, body=shift))[0] == 409
    _, schedule, _ = await invoke(svc, f"/schedule?from={day}&to={day}", svc.schedule, uid=1003)
    assert schedule["items"][0]["start"] == "09:00" and schedule["items"][0]["attendance"] == "PLANNED"
    assert not schedule["employees"] and not schedule["hotels"]
    assert (await invoke(svc, "/schedule/1", svc.cancel_shift, uid=1002, match={"id": str(created["id"])}))[0] == 200
    _, logs, _ = await invoke(svc, "/audit", svc.audit)
    assert sum(item["action"] == "academy_lesson_completed" for item in logs["items"]) == 1
    assert any(item["action"] == "miniapp_shift_created" and "User 3" in item["details"] for item in logs["items"])
    assert (await invoke(svc, "/handoff", svc.handoff, uid=1003, body={"action": "new-booking"}))[0] == 403
    assert (await invoke(svc, "/handoff", svc.handoff, uid=1003, body={"action": "practice"}))[0] == 200
    assert svc.bot.sent[-1][1]["protect_content"] is True
    app = web.Application()
    svc.register(app)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/api/miniapp/me")).status == 401
        response = await client.get("/app/")
        assert response.status == 200 and "ТЕСТОВЫЙ ВЫПУСК" in await response.text()
        for asset in ["css/styles.css", "js/app.js", "js/academy.js", "assets/academy/hero.jpg"]:
            assert (await client.get("/app/" + asset)).status == 200
        assert (await client.get("/app/js/demo.js")).status == 404
        assert (await client.get("/app/config.py")).status == 404


def test_sqlite_release_end_to_end():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            await exercise(engine, True)
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.skipif(not os.getenv("MINIAPP_TEST_DATABASE_URL"), reason="PostgreSQL test service is explicitly opt-in")
def test_postgresql_release_end_to_end():
    async def run():
        url = os.environ["MINIAPP_TEST_DATABASE_URL"]
        # Safety: only a specifically named local CI database may be touched.
        parsed = urlsplit(url)
        assert parsed.hostname in {"127.0.0.1", "localhost", "postgres"}
        assert parsed.path == "/miniapp_ci"
        schema = "miniapp_test_" + uuid.uuid4().hex
        base = create_async_engine(url)
        async with base.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        try:
            await exercise(engine, False)
        finally:
            await engine.dispose()
            async with base.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await base.dispose()
    asyncio.run(run())


def test_live_bundle_cannot_toggle_demo():
    root = Path(__file__).parents[1] / "app" / "webapp"
    source = (root / "js" / "app.js").read_text()
    assert "demo.js" not in source and "new URLSearchParams(location.search)" not in source
    assert "X-Telegram-Init-Data" in (root / "js" / "api.js").read_text()
    assert "AI" not in (root / "config.js").read_text()

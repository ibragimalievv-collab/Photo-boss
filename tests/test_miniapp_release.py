"""Production Mini App contract tests. Disposable SQLite, no real Telegram.

FOR UPDATE is removed by the SQLite adapter only. The production implementation
retains PostgreSQL row locking; these tests do not simulate network failures.
"""
import asyncio
import hashlib
import hmac
import json
import subprocess
import time
import unittest
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qsl, urlencode, urlsplit
from zoneinfo import ZoneInfo

import pytest
from aiohttp import web
from multidict import MultiDict
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from app.miniapp_api import MiniApp
from app.miniapp_security import (
    AccessError,
    financial_period,
    parse_shift,
    role_permissions,
    utc_bounds,
    validate_init_data,
    webhook_secret,
)

TOKEN = "123456789:unit-test-only-not-a-real-token"
ROOT = Path(__file__).resolve().parents[1]


def signed(uid=1001, *, issued=None, extra=None):
    values = {"auth_date": str(int(time.time()) if issued is None else issued),
              "user": json.dumps({"id": uid, "first_name": "Test"}, separators=(",", ":"))}
    values.update(extra or {})
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{k}={values[k]}" for k in sorted(values))
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


@pytest.mark.parametrize("raw", ["", "hash=1", 'user={"id":1}', "x=y&x=z", "x=" * 10000])
def test_reject_unsigned_init_data(raw):
    with pytest.raises(AccessError) as error:
        validate_init_data(raw, TOKEN)
    assert error.value.status == 401


@pytest.mark.parametrize("uid", [True, False, 0, -1, 2**52, "123", None])
def test_reject_wrong_user_id_type(uid):
    with pytest.raises(AccessError):
        validate_init_data(signed(uid), TOKEN)


@pytest.mark.parametrize("offset", [-3601, 31, 86400])
def test_expired_or_future_signature(offset):
    now = int(time.time())
    with pytest.raises(AccessError):
        validate_init_data(signed(issued=now + offset), TOKEN, now=now)


def test_signature_includes_extra_telegram_signature_field():
    assert validate_init_data(signed(extra={"signature": "telegram-signature"}), TOKEN) == 1001


def test_duplicate_or_tampered_authentication_rejected():
    for value in [signed() + "&user=x", signed().replace("%3A1001", "%3A1002")]:
        with pytest.raises(AccessError):
            validate_init_data(value, TOKEN)


def test_webhook_secret_not_public_token_and_rotates():
    assert len(webhook_secret(TOKEN)) == 64
    assert webhook_secret(TOKEN) == webhook_secret(TOKEN)
    assert webhook_secret(TOKEN) != webhook_secret(TOKEN + "rotated")
    assert TOKEN not in webhook_secret(TOKEN)


def test_admin_plus_staff_remains_today_only():
    roles = ["ADMIN", "PHOTOGRAPHER"]
    assert role_permissions(roles)["financeScope"] == "today"
    assert not role_permissions(roles)["audit"]
    for period in ["week", "month", "custom"]:
        with pytest.raises(AccessError):
            financial_period(roles, period, date(2026, 9, 20))
    with pytest.raises(AccessError):
        financial_period(roles, "today", date(2026, 9, 20), start="2026-01-01")
    assert role_permissions(["OWNER", "ADMIN"])["audit"]


def test_day_boundaries_and_shift_timezone():
    zone = ZoneInfo("Europe/Moscow")
    assert utc_bounds(date(2026, 9, 20), date(2026, 9, 20), zone) == (
        datetime(2026, 9, 19, 21, tzinfo=timezone.utc).replace(tzinfo=None),
        datetime(2026, 9, 20, 21, tzinfo=timezone.utc).replace(tzinfo=None))
    payload = {"userId": "3", "hotelId": "1", "date": "2026-09-20", "start": "09:00", "end": "19:00"}
    assert parse_shift(payload, zone, date(2026, 9, 20))[2].hour == 6


@pytest.mark.parametrize("change", [{"userId": True}, {"userId": 3.1}, {"userId": []},
                                  {"role": "OWNER"}, {"end": "08:00"}, {"start": "bad"},
                                  {"date": "2026-09-19"}, {"start": "00:00", "end": "23:59"}])
def test_bad_schedule_payload(change):
    payload = {"userId": "3", "hotelId": "1", "date": "2026-09-20", "start": "09:00", "end": "19:00"} | change
    with pytest.raises(AccessError):
        parse_shift(payload, ZoneInfo("Europe/Moscow"), date(2026, 9, 20))


class Connection:
    def __init__(self, connection):
        self.connection = connection
        self.dialect = connection.dialect

    async def execute(self, statement, parameters=None):
        return self.connection.execute(text(str(statement).replace(" FOR UPDATE", "")), parameters or {})


class Engine:
    def __init__(self):
        self.inner = create_engine("sqlite://", poolclass=StaticPool)

    @asynccontextmanager
    async def connect(self):
        with self.inner.connect() as connection:
            yield Connection(connection)

    @asynccontextmanager
    async def begin(self):
        with self.inner.begin() as connection:
            yield Connection(connection)


class Body:
    def __init__(self, content):
        self.content, self.offset = content, 0

    def at_eof(self):
        return self.offset >= len(self.content)

    async def read(self, count):
        part = self.content[self.offset:self.offset + min(count, 13)]
        self.offset += len(part)
        return part


class Request(dict):
    def __init__(self, path, uid, method, body, token):
        super().__init__()
        url = urlsplit(path)
        self.path, self.query, self.method = url.path, MultiDict(parse_qsl(url.query)), method
        raw = json.dumps(body if body is not None else {}).encode()
        self.content, self.content_length = Body(raw), len(raw)
        self.headers = {"X-Telegram-Init-Data": signed(uid) if token is None else token}
        self.match_info = {}


class MiniAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = Engine()
        self.bot = SimpleNamespace(token=TOKEN, send_message=AsyncMock(),
                                   me=AsyncMock(return_value=SimpleNamespace(username="unit_test_bot")))
        lessons = [
            SimpleNamespace(slug="light", title="Light", body="Lesson", day=1, block=1),
            SimpleNamespace(slug="next", title="Next", body="Lesson", day=5, block=2),
        ]
        blocks = [
            SimpleNamespace(number=1, title="First", practice_categories=("woman",), practice_title="Practice"),
            SimpleNamespace(number=2, title="Second", practice_categories=(), practice_title="Final"),
        ]
        self.service = MiniApp(self.engine, self.bot, lessons, blocks=blocks,
                               static_dir=ROOT / "app" / "webapp")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.engine.inner.begin() as connection:
            ddls = [
                "CREATE TABLE users(id INTEGER PRIMARY KEY,tg_id BIGINT,name TEXT,active BOOLEAN)",
                "CREATE TABLE user_roles(user_id INTEGER,role TEXT)",
                "CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)",
                "CREATE TABLE hotels(id INTEGER PRIMARY KEY,name TEXT,active BOOLEAN)",
                "CREATE TABLE hotel_employees(user_id INTEGER,hotel_id INTEGER)",
                "CREATE TABLE clients(id INTEGER PRIMARY KEY,name TEXT)",
                "CREATE TABLE packages(id INTEGER PRIMARY KEY,name TEXT)",
                "CREATE TABLE bookings(id INTEGER PRIMARY KEY,room TEXT,shoot_date DATE,shoot_time TEXT,status TEXT,photographer_id INTEGER,manager_id INTEGER,hotel_id INTEGER,client_id INTEGER,package_id INTEGER)",
                "CREATE TABLE shootings(id INTEGER PRIMARY KEY,booking_id INTEGER)",
                "CREATE TABLE photos(id INTEGER PRIMARY KEY,shooting_id INTEGER)",
                "CREATE TABLE sales(id INTEGER PRIMARY KEY,booking_id INTEGER,credited_user_id INTEGER,commission_role TEXT,amount NUMERIC,commission NUMERIC,payment_status TEXT,created_at DATETIME)",
                "CREATE TABLE payroll_entries(id INTEGER PRIMARY KEY,user_id INTEGER,kind TEXT,amount NUMERIC,created_at DATETIME)",
                "CREATE TABLE receipts(id INTEGER PRIMARY KEY,status TEXT,verified_amount NUMERIC,reviewed_at DATETIME)",
                "CREATE TABLE shifts(id INTEGER PRIMARY KEY,user_id INTEGER,hotel_id INTEGER,start_at DATETIME,end_at DATETIME,status TEXT)",
                "CREATE TABLE shift_check_ins(id INTEGER PRIMARY KEY,user_id INTEGER,shift_date DATE,started_at DATETIME)",
                "CREATE TABLE shift_check_outs(id INTEGER PRIMARY KEY,user_id INTEGER,shift_date DATE,ended_at DATETIME)",
                "CREATE TABLE audit_logs(id INTEGER PRIMARY KEY,user_id INTEGER,action TEXT,entity TEXT,entity_id INTEGER,details TEXT,created_at DATETIME)",
                "CREATE TABLE academy_lesson_progress(id INTEGER PRIMARY KEY,user_id INTEGER,topic_slug TEXT,completed_at DATETIME,UNIQUE(user_id,topic_slug))",
                "CREATE TABLE training_assignments(id INTEGER PRIMARY KEY,user_id INTEGER,category_slug TEXT,status TEXT,ai_score INTEGER,ai_analysis TEXT)",
                "CREATE TABLE academy_reviews(id INTEGER PRIMARY KEY,user_id INTEGER,quality_score INTEGER,issues TEXT,recommendation TEXT)",
                "CREATE TABLE academy_certificates(id INTEGER PRIMARY KEY,user_id INTEGER,certificate_no TEXT,verification_code TEXT,final_score INTEGER,issued_at DATETIME,revoked_at DATETIME)",
                "CREATE TABLE academy_locations(id INTEGER PRIMARY KEY,hotel_id INTEGER,name TEXT,description TEXT,shot_plan TEXT,active BOOLEAN,created_at DATETIME)",
            ]
            for ddl in ddls:
                connection.execute(text(ddl))
            for uid, role in [(1, "OWNER"), (2, "ADMIN"), (3, "PHOTOGRAPHER"), (4, "MANAGER"), (5, "PHOTOGRAPHER")]:
                connection.execute(text("INSERT INTO users VALUES (:id,:tg,:name,TRUE)"),
                                   {"id": uid, "tg": uid + 1000, "name": f"User {uid}"})
                connection.execute(text("INSERT INTO user_roles VALUES (:id,:role)"), {"id": uid, "role": role})
            connection.execute(text("INSERT INTO users VALUES (6,1006,'Inactive',FALSE),(7,1007,'No role',TRUE)"))
            connection.execute(text("INSERT INTO hotels VALUES (1,'Test hotel',TRUE),(2,'Other hotel',TRUE)"))
            connection.execute(text("INSERT INTO clients VALUES (1,'Own guest'),(2,'Other guest')"))
            connection.execute(text("INSERT INTO packages VALUES (1,'Family')"))
            connection.execute(text("INSERT INTO bookings VALUES (1,'101',:day,'10:00','ASSIGNED',3,4,1,1,1),(2,'201',:day,'11:00','ASSIGNED',5,4,2,2,1)"), {"day": self.service.today()})
            connection.execute(text("INSERT INTO sales VALUES (1,1,3,'PHOTOGRAPHER',21000,3150,'PAID',:now),(2,2,5,'PHOTOGRAPHER',26000,3900,'UNPAID',:now)"), {"now": now})
            connection.execute(text("INSERT INTO payroll_entries VALUES (1,3,'Премия',1000,:now),(2,5,'Премия',1500,:now)"), {"now": now})
            connection.execute(text("INSERT INTO receipts VALUES (1,'APPROVED',21000,:now),(2,'PENDING',26000,:now)"), {"now": now})
            connection.execute(text("INSERT INTO audit_logs VALUES (1,1,'unknown_legacy_event','sale',1,'Full retained details',:now)"), {"now": now})

    async def asyncTearDown(self):
        self.engine.inner.dispose()

    async def call(self, path, uid=1001, method="GET", body=None, token=None):
        service = self.service
        request = Request("/api/miniapp" + path, uid, method, body, token)
        route = urlsplit(path).path
        table = {("/session", "POST"): service.session_open, ("/me", "GET"): service.me,
                 ("/preferences", "PUT"): service.preferences, ("/finance", "GET"): service.finance,
                 ("/bookings", "GET"): service.bookings, ("/dashboard", "GET"): service.dashboard,
                 ("/schedule", "GET"): service.schedule, ("/schedule", "POST"): service.create_shift,
                 ("/audit", "GET"): service.audit, ("/academy", "GET"): service.academy,
                 ("/academy/locations", "POST"): service.create_academy_location,
                 ("/handoff", "POST"): service.handoff}
        handler = table.get((route, method))
        if route.startswith("/academy/lessons/"):
            request.match_info = {"slug": route.rsplit("/", 1)[1]}
            handler = service.complete_lesson
        if route.startswith("/schedule/") and method == "DELETE":
            request.match_info = {"id": route.rsplit("/", 1)[1]}
            handler = service.cancel_shift
        assert handler is not None
        response = await service.middleware(request, handler)
        return response.status, json.loads(response.text), response.headers

    async def test_reused_telegram_webview_init_data_is_accepted_for_one_day(self):
        two_hours_old = int(time.time()) - 2 * 60 * 60
        status, data, _ = await self.call("/me", uid=1001, token=signed(1001, issued=two_hours_old))
        assert status == 200 and data["user"]["id"] == 1

        older_than_one_day = int(time.time()) - 24 * 60 * 60 - 1
        assert (await self.call("/me", uid=1001, token=signed(1001, issued=older_than_one_day)))[0] == 401

    async def test_no_credentials_and_inactive_users(self):
        status, data, headers = await self.call("/me", token="")
        assert status == 401 and "user" not in data and headers["Cache-Control"] == "no-store"
        for uid in [9999, 1006, 1007]:
            assert (await self.call("/me", uid=uid))[0] == 403

    async def test_access_screen_shows_only_signed_own_identity(self):
        for uid in [9999, 1006, 1007]:
            status, data, _ = await self.call("/me", uid=uid)
            assert status == 403 and data["telegramId"] == uid
            assert set(data) == {"error", "telegramId"}
        for token in ["", signed(9999, issued=int(time.time()) - 7200)]:
            status, data, _ = await self.call("/me", uid=9999, token=token)
            assert status == 401 and "telegramId" not in data

    async def test_owner_cash_is_not_unpaid_sales(self):
        status, data, _ = await self.call("/finance")
        assert status == 200
        assert data["sales"] == 4700000 and data["cashReceived"] == 2100000
        assert data["payroll"] == 955000

    async def test_staff_only_own_money_and_bookings(self):
        _, data, _ = await self.call("/finance?period=month", uid=1003)
        assert data["cashReceived"] is None and data["sales"] == 2100000
        assert data["payroll"] == 415000 and [x["id"] for x in data["employees"]] == [3]
        _, bookings, _ = await self.call("/bookings", uid=1003)
        assert [x["id"] for x in bookings["items"]] == [1]
        _, bookings, _ = await self.call("/bookings", uid=1004)
        assert len(bookings["items"]) == 2

    async def test_admin_history_and_audit_are_denied(self):
        for url in ["/finance?period=month", "/finance?period=week", "/finance?period=today&from=2025-01-01", "/audit"]:
            status, data, _ = await self.call(url, uid=1002)
            assert status == 403 and "items" not in data and "employees" not in data
        assert (await self.call("/finance", uid=1002))[0] == 200
        assert (await self.call("/dashboard?day=2025-01-01", uid=1002))[0] == 400

    async def test_role_and_user_overrides_are_denied(self):
        for suffix in ["user_id=1", "userId=1", "role=OWNER", "scope=all"]:
            assert (await self.call("/finance?" + suffix, uid=1003))[0] == 400

    async def test_theme_is_personal_not_permission(self):
        assert (await self.call("/preferences", uid=1003, method="PUT", body={"theme": "premium"}))[0] == 200
        _, own, _ = await self.call("/me", uid=1003)
        _, other, _ = await self.call("/me", uid=1005)
        assert own["user"]["theme"] == "premium" and own["user"]["roles"] == ["PHOTOGRAPHER"]
        assert other["user"]["theme"] == "light"
        for value in [[], {}, True, None, 42]:
            assert (await self.call("/preferences", method="PUT", body={"theme": value}))[0] == 400
        assert (await self.call("/preferences", method="PUT", body={"theme": "premium", "userId": 1}))[0] == 400

    async def test_lessons_idempotent_and_isolated(self):
        for _ in range(2):
            assert (await self.call("/academy/lessons/light", uid=1003, method="POST"))[0] == 200
        _, own, _ = await self.call("/academy", uid=1003)
        _, other, _ = await self.call("/academy", uid=1005)
        assert own["completed"] == ["light"] and own["points"] == 10 and not other["completed"]
        _, audit, _ = await self.call("/audit")
        assert sum(x["action"] == "academy_lesson_completed" for x in audit["items"]) == 1

    async def test_next_academy_block_requires_accepted_practice(self):
        assert (await self.call("/academy/lessons/next", uid=1003, method="POST"))[0] == 409
        assert (await self.call("/academy/lessons/light", uid=1003, method="POST"))[0] == 200
        assert (await self.call("/academy/lessons/next", uid=1003, method="POST"))[0] == 409
        with self.engine.inner.begin() as connection:
            connection.execute(text(
                "INSERT INTO training_assignments VALUES (1,3,'woman','COMPLETED',90,NULL)"
            ))
        assert (await self.call("/academy/lessons/next", uid=1003, method="POST"))[0] == 200
        _, academy, _ = await self.call("/academy", uid=1003)
        assert academy["blocks"][0]["practiceDone"] is True
        assert academy["lessons"][1]["locked"] is False

    async def test_owner_creates_location_profile_and_team_metrics_are_private(self):
        body = {"name": "Lobby", "description": "Window light", "hotelId": 1,
                "shotPlan": [f"Shot {index}" for index in range(1, 6)]}
        assert (await self.call("/academy/locations", uid=1003, method="POST", body=body))[0] == 403
        assert (await self.call("/academy/locations", method="POST", body=body))[0] == 201
        _, owner, _ = await self.call("/academy")
        _, photographer, _ = await self.call("/academy", uid=1003)
        assert owner["locations"][0]["name"] == "Lobby"
        assert owner["team"] and photographer["team"] == []

    async def test_public_certificate_page_verifies_database_record(self):
        with self.engine.inner.begin() as connection:
            connection.execute(text("""INSERT INTO academy_certificates
                VALUES (1,3,'PBIC-2026-000003-ABCD','verify-code',92,:now,NULL)"""),
                {"now": datetime.now(timezone.utc).replace(tzinfo=None)})
        request = SimpleNamespace(match_info={"code": "verify-code"})
        response = await self.service.certificate_page(request)
        assert response.status == 200
        assert "INTERNATIONAL CERTIFICATE" in response.text
        assert "PBIC-2026-000003-ABCD" in response.text

    async def test_schedule_conflicts_cancellation_and_scope(self):
        day = str(self.service.today() + timedelta(days=1))
        body = {"userId": "3", "hotelId": "1", "date": day, "start": "09:00", "end": "19:00"}
        status, data, _ = await self.call("/schedule", uid=1002, method="POST", body=body)
        assert status == 201
        assert (await self.call("/schedule", uid=1002, method="POST", body=body))[0] == 409
        _, schedule, _ = await self.call(f"/schedule?from={day}&to={day}", uid=1003)
        assert schedule["items"][0]["start"] == "09:00"
        assert schedule["items"][0]["attendance"] == "PLANNED" and not schedule["employees"]
        assert (await self.call("/schedule", uid=1003, method="POST", body=body))[0] == 403
        assert (await self.call(f"/schedule/{data['id']}", uid=1003, method="DELETE"))[0] == 403
        assert (await self.call(f"/schedule/{data['id']}", uid=1002, method="DELETE"))[0] == 200
        _, audit, _ = await self.call("/audit")
        assert audit["items"][0]["action"] == "miniapp_shift_cancelled"

    async def test_audit_details_and_pagination_validation(self):
        _, audit, _ = await self.call("/audit")
        assert "unknown_legacy_event" in audit["items"][0]["title"]
        assert audit["items"][0]["details"] == "Full retained details"
        assert (await self.call("/audit?before=" + str(2**80)))[0] == 400

    async def test_session_open_once_no_credential_in_audit(self):
        token = signed(1003)
        for _ in range(2):
            assert (await self.call("/session", uid=1003, method="POST", token=token))[0] == 200
        _, audit, _ = await self.call("/audit")
        assert sum(x["action"] == "miniapp_opened" for x in audit["items"]) == 1
        assert token not in json.dumps(audit)
        with self.engine.inner.connect() as connection:
            last_login = connection.execute(text(
                "SELECT value FROM settings WHERE key='miniapp:last_login:3'"
            )).scalar()
        assert last_login
        with self.engine.inner.begin() as connection:
            connection.execute(text(
                "INSERT INTO settings(key,value) VALUES ('miniapp:screen_capture:1','0')"
            ))
        _, owner_me, _ = await self.call("/me", uid=1001)
        _, staff_me, _ = await self.call("/me", uid=1003)
        assert owner_me["screenCaptureAllowed"] is True
        assert staff_me["screenCaptureAllowed"] is False

    async def test_handoff_does_not_create_sale(self):
        status, result, _ = await self.call("/handoff", uid=1003, method="POST", body={"action": "new-sale"})
        assert status == 200 and result["url"] == "/app/#workflow"
        self.bot.send_message.assert_not_awaited()
        self.bot.me.assert_not_awaited()
        assert (await self.call("/finance"))[1]["salesCount"] == 2
        assert (await self.call("/handoff", method="POST", body={"action": []}))[0] == 400

    async def test_legacy_navigation_stays_in_app_with_role_checks(self):
        for action, path in [("new-booking", "workflow"), ("new-sale", "workflow"),
                             ("shift", "schedule"), ("practice", "academy")]:
            status, data, _ = await self.call("/handoff", uid=1004, method="POST", body={"action": action})
            assert status == 200 and data == {"url": f"/app/#{path}"}
        assert (await self.call("/handoff", uid=1003, method="POST", body={"action": "new-booking"}))[0] == 403
        assert (await self.call("/handoff", method="POST", token="", body={"action": "shift"}))[0] == 401
        self.bot.send_message.assert_not_awaited()
        self.bot.me.assert_not_awaited()

    async def test_static_allowlist_and_route_registration(self):
        for name in ["js/demo.js", "preview.html", ".env", "../config.py", "package.json"]:
            with pytest.raises(web.HTTPNotFound):
                await self.service.static_file(SimpleNamespace(match_info={"asset": name}))
        app = web.Application()
        self.service.register(app)
        paths = {route.resource.canonical for route in app.router.routes()}
        assert {"/api/miniapp/finance", "/api/miniapp/audit", "/app/"} <= paths
        assert (ROOT / "app" / "webapp" / "js" / "academy.js").is_file()


def test_javascript_syntax_and_no_live_demo_import():
    for path in (ROOT / "app" / "webapp" / "js").glob("*.js"):
        subprocess.run(["node", "--check", str(path)], check=True, capture_output=True, text=True)
    source = (ROOT / "app" / "webapp" / "js" / "app.js").read_text()
    assert "./demo.js" not in source and "demoRequest" not in source


def test_runtime_menu_hides_training_duplicate_and_admin_audit():
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    from app.launch_policy import safe_menu
    markup = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📚 Академия"), KeyboardButton(text="🎓 Обучение")],
                                         [KeyboardButton(text="📜 Аудит")]])
    admin = safe_menu(markup, {"ADMIN"})
    assert [b.text for row in admin.keyboard for b in row] == ["📚 Академия"]
    owner = safe_menu(markup, {"OWNER"})
    assert [b.text for row in owner.keyboard for b in row] == ["📚 Академия", "📜 Аудит"]


def test_legacy_bot_audit_denied_before_handler():
    from app.launch_policy import LaunchPolicy
    async def old_handler(event, data):
        raise AssertionError("Legacy audit must not run")
    def callback():
        pass
    callback.__module__ = "app.handlers.admin"
    callback.__name__ = "auditlog"
    data = {"current_user": SimpleNamespace(id=2), "current_roles": {"ADMIN"},
            "handler": SimpleNamespace(callback=callback)}
    event = SimpleNamespace(text="📜 Аудит")
    with patch("app.launch_policy.respond", new_callable=AsyncMock) as response:
        asyncio.run(LaunchPolicy()(old_handler, event, data))
        assert "только владельцу" in response.await_args.args[1]


def test_runtime_entry_points_install_policy():
    for name in ["main.py", "render_main.py"]:
        source = (ROOT / "app" / name).read_text()
        assert "install_launch_policies(dispatcher, bot)" in source
    source = (ROOT / "app" / "render_main.py").read_text()
    assert source.count("secret_token=webhook_secret(config.bot_token)") == 2
    assert "drop_pending_updates=False" in source

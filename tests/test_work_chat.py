"""Work-chat policy and authenticated API checks."""
import json
import unittest
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest
import test_miniapp_release as baseline
from sqlalchemy import text

from app.miniapp_security import AccessError
from app.work_chat import (
    MAX_ATTACHMENT_BYTES,
    MAX_MESSAGE,
    RETENTION_DAYS,
    WorkChat,
    attachment_kind,
    clean_message,
    cleanup_expired_chat,
    positive_id,
    safe_filename,
)
from app.work_rules import WORK_RULES_TEXT, WORK_RULES_VERSION, work_rules_hash


@pytest.mark.parametrize("bad", [True, 0, -1, "0", "x", "", 2**31])
def test_bad_chat_ids(bad):
    with pytest.raises(AccessError):
        positive_id(bad)


def test_message_cleaning_and_limits():
    assert clean_message("  привет\r\nкоманда  ") == "привет\nкоманда"
    assert clean_message("x" * MAX_MESSAGE) == "x" * MAX_MESSAGE
    for bad in ("", "   ", "x" * (MAX_MESSAGE + 1), "ok\x00bad"):
        with pytest.raises(AccessError):
            clean_message(bad)


def test_attachment_validation_and_magic_detection():
    assert MAX_ATTACHMENT_BYTES == 20 * 1024 * 1024
    assert safe_filename("../folder\\guest photo.jpg") == "guest photo.jpg"
    assert attachment_kind("x.jpg", b"\xff\xd8\xff" + b"x" * 20) == (
        "image/jpeg", "jpg", True
    )
    assert attachment_kind("x.pdf", b"%PDF-1.7\nbody") == (
        "application/pdf", "pdf", False
    )
    assert attachment_kind("notes.zip", b"PK\x03\x04data") == (
        "application/octet-stream", "bin", False
    )
    for name in ("bad.exe", "page.html", "script.js", "vector.svg"):
        with pytest.raises(AccessError):
            attachment_kind(name, b"payload")


def test_voice_and_video_attachment_formats():
    webm = b"\x1a\x45\xdf\xa3\x42\x82\x84webm" + b"x" * 30
    mp4 = b"\x00\x00\x00\x18ftypisom" + b"x" * 30
    assert attachment_kind("voice.weba", webm)[:2] == ("audio/webm", "weba")
    assert attachment_kind("video.webm", webm)[:2] == ("video/webm", "webm")
    assert attachment_kind("voice.m4a", mp4)[:2] == ("audio/mp4", "m4a")
    assert attachment_kind("video.mp4", mp4)[:2] == ("video/mp4", "mp4")
    assert attachment_kind("fake.webm", b"<html>bad</html>")[0] == "application/octet-stream"


def test_rules_are_explicit_and_whole_document_acceptance():
    assert "Владелец компании имеет доступ" in WORK_RULES_TEXT
    assert "включая сообщения между двумя сотрудниками" in WORK_RULES_TEXT
    assert "не является договором оказания услуг" in WORK_RULES_TEXT
    assert str(RETENTION_DAYS) in WORK_RULES_TEXT
    assert len(work_rules_hash()) == 64
    assert WORK_RULES_VERSION


class WorkChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await baseline.MiniAppTests.asyncSetUp(self)
        self.chat = WorkChat(self.service)
        with self.engine.inner.begin() as conn:
            from app.models import WorkChatSendReceipt
            WorkChatSendReceipt.__table__.create(conn)
            conn.execute(text("""CREATE TABLE work_rule_acceptances(
                id INTEGER PRIMARY KEY,user_id INTEGER,version TEXT,text_sha256 TEXT,
                accepted_at DATETIME,UNIQUE(user_id,version))"""))
            conn.execute(text("""CREATE TABLE work_chat_attachments(
                id INTEGER PRIMARY KEY,uploader_id INTEGER,storage_path TEXT,
                original_name TEXT,mime_type TEXT,byte_size INTEGER,sha256 TEXT,
                created_at DATETIME)"""))
            conn.execute(text("""CREATE TABLE work_chat_messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,sender_id INTEGER,recipient_id INTEGER,
                attachment_id INTEGER,body TEXT,created_at DATETIME)"""))
            conn.execute(text("""CREATE TABLE work_chat_deletions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,message_id INTEGER UNIQUE,
                sender_id INTEGER,recipient_id INTEGER,deleted_at DATETIME)"""))
            conn.execute(text("""CREATE TABLE work_chat_read_states(
                id INTEGER PRIMARY KEY,user_id INTEGER,scope_key TEXT,
                last_read_message_id INTEGER,updated_at DATETIME,
                UNIQUE(user_id,scope_key))"""))

    async def asyncTearDown(self):
        await baseline.MiniAppTests.asyncTearDown(self)

    async def call(self, path, *, uid=1001, method="GET", body=None, token=None):
        request = baseline.Request("/api/miniapp" + path, uid, method, body, token)
        route = urlsplit(path).path
        handlers = {
            ("/chat/rules", "GET"): self.chat.rules,
            ("/chat/rules/accept", "POST"): self.chat.accept_rules,
            ("/chat/people", "GET"): self.chat.people,
            ("/chat/presence", "GET"): self.chat.presence_status,
            ("/chat/presence", "POST"): self.chat.presence_heartbeat,
            ("/chat/unread", "GET"): self.chat.unread,
            ("/chat/messages", "GET"): self.chat.messages,
            ("/chat/messages", "POST"): self.chat.send,
            ("/chat/owner/threads", "GET"): self.chat.owner_threads,
            ("/chat/owner/messages", "GET"): self.chat.owner_messages,
        }
        if route.startswith("/chat/messages/") and method == "DELETE":
            request.match_info["id"] = route.rsplit("/", 1)[-1]
            handler = self.chat.delete_message
        elif route.startswith("/chat/attachments/"):
            request.match_info["id"] = route.rsplit("/", 1)[-1]
            handler = self.chat.attachment
        else:
            handler = handlers[(route, method)]
        request.app = {"yandex_disk": getattr(self, "storage", None)}
        response = await self.service.middleware(request, handler)
        return response.status, json.loads(response.text)

    async def accept(self, uid):
        status, rules = await self.call("/chat/rules", uid=uid)
        assert status == 200
        return await self.call(
            "/chat/rules/accept", uid=uid, method="POST",
            body={"version": rules["version"], "sha256": rules["sha256"]},
        )

    async def test_retry_after_lost_ack_is_one_message_and_payload_is_bound(self):
        await self.accept(1003)
        body = {"peerId": 4, "body": "Сохранить один раз", "clientId": "retry-fixture-0001"}
        status, first = await self.call("/chat/messages", uid=1003, method="POST", body=body)
        assert status == 201
        status, replay = await self.call("/chat/messages", uid=1003, method="POST", body=body)
        assert status == 200 and replay["message"] == first["message"]
        assert replay["replayed"] is True
        for update in ({"body": "Другой текст"}, {"peerId": None}):
            assert (await self.call("/chat/messages", uid=1003, method="POST", body={**body, **update}))[0] == 409
        _, history = await self.call("/chat/messages?peer=4", uid=1003)
        assert len(history["messages"]) == 1
        await self.accept(1004)
        # Same client ID from a different signed sender is an independent request.
        assert (await self.call("/chat/messages", uid=1004, method="POST", body={**body, "peerId": 3}))[0] == 201

    async def test_deleted_message_cannot_be_resurrected_by_retry(self):
        await self.accept(1001)
        body = {"peerId": None, "body": "Удалить", "clientId": "retry-fixture-0002"}
        _, sent = await self.call("/chat/messages", method="POST", body=body)
        assert (await self.call(f"/chat/messages/{sent['message']['id']}", method="DELETE"))[0] == 200
        assert (await self.call("/chat/messages", method="POST", body=body))[0] == 409
        assert (await self.call("/chat/messages?peer=general"))[1]["messages"] == []

    async def test_retry_key_validation_and_rollback(self):
        await self.accept(1003)
        for key in ("short", True, "x" * 65, "bad key contains spaces"):
            assert (await self.call("/chat/messages", uid=1003, method="POST",
                body={"peerId": 4, "body": "test", "clientId": key}))[0] == 400
        # A rejected target must not consume a key.
        body = {"peerId": 999, "body": "test", "clientId": "retry-fixture-0003"}
        assert (await self.call("/chat/messages", uid=1003, method="POST", body=body))[0] == 409
        assert (await self.call("/chat/messages", uid=1003, method="POST", body={**body, "peerId": 4}))[0] == 201

    async def test_rules_gate_all_chat_data(self):
        assert (await self.call("/chat/rules", uid=1003))[0] == 200
        for path,method,body in [
            ("/chat/people","GET",None),
            ("/chat/presence","GET",None),
            ("/chat/presence","POST",{}),
            ("/chat/messages?peer=general&after=0","GET",None),
            ("/chat/messages","POST",{"peerId": None, "body": "hello"}),
        ]:
            assert (await self.call(path, uid=1003, method=method, body=body))[0] == 428
        assert (await self.accept(1003))[0] == 200
        assert (await self.call("/chat/people", uid=1003))[0] == 200

    async def test_presence_auth_identity_and_inactive_accounts(self):
        for uid in (1003, 1004):
            await self.accept(uid)
        body = {"sessionId": "fixture-session-123456", "sequence": 1, "online": True}
        assert (await self.call("/chat/presence", token="forged"))[0] == 401
        for uid in (1006, 1007):
            assert (await self.call("/chat/presence", uid=uid))[0] == 403
        assert (await self.call("/chat/presence", uid=1003, method="POST", body=body))[0] == 200
        _, data = await self.call("/chat/presence", uid=1004)
        assert data["online"] == [3]
        _, people = await self.call("/chat/people", uid=1004)
        assert next(p for p in people["people"] if p["id"] == 3)["online"] is True
        assert (await self.call("/chat/presence", uid=1003, method="POST",
                               body={**body, "userId": 4}))[0] == 400
        with self.engine.inner.begin() as conn:
            conn.execute(text("UPDATE users SET active=FALSE WHERE id=3"))
        assert (await self.call("/chat/presence", uid=1004))[1]["online"] == []

    async def test_general_and_private_visibility(self):
        for uid in (1001,1003,1004,1005):
            await self.accept(uid)
        assert (await self.call(
            "/chat/messages", uid=1003, method="POST",
            body={"peerId": None, "body": "Общая рабочая новость"},
        ))[0] == 201
        _, general = await self.call("/chat/messages?peer=general&after=0", uid=1005)
        assert [m["body"] for m in general["messages"]] == ["Общая рабочая новость"]

        assert (await self.call(
            "/chat/messages", uid=1003, method="POST",
            body={"peerId": 4, "body": "Личный рабочий вопрос"},
        ))[0] == 201
        _, own = await self.call("/chat/messages?peer=3&after=0", uid=1004)
        assert [m["body"] for m in own["messages"]] == ["Личный рабочий вопрос"]
        _, outsider = await self.call("/chat/messages?peer=3&after=0", uid=1005)
        assert outsider["messages"] == []

        _, threads = await self.call("/chat/owner/threads", uid=1001)
        assert threads["threads"][0]["a"]["id"] == 3
        assert threads["threads"][0]["b"]["id"] == 4
        _, controlled = await self.call("/chat/owner/messages?a=3&b=4&after=0", uid=1001)
        assert [m["body"] for m in controlled["messages"]] == ["Личный рабочий вопрос"]
        assert controlled["readOnly"] is True

    async def test_unread_counts_clear_only_when_thread_is_opened(self):
        for uid in (1003, 1004, 1005):
            await self.accept(uid)
        await self.call(
            "/chat/messages", uid=1003, method="POST",
            body={"peerId": 4, "body": "Личное непрочитанное"},
        )
        await self.call(
            "/chat/messages", uid=1003, method="POST",
            body={"peerId": None, "body": "Общее непрочитанное"},
        )
        _, unread = await self.call("/chat/unread", uid=1004)
        assert unread["total"] == 2
        assert unread["general"] == 1
        assert unread["people"]["3"] == 1

        _, listing = await self.call("/chat/people", uid=1004)
        peer = next(item for item in listing["people"] if item["id"] == 3)
        assert peer["unread"] == 1
        assert listing["general"]["unread"] == 1
        assert listing["totalUnread"] == 2

        await self.call("/chat/messages?peer=3&after=0", uid=1004)
        _, after_private = await self.call("/chat/unread", uid=1004)
        assert after_private["total"] == 1
        assert after_private["people"].get("3", 0) == 0
        assert after_private["general"] == 1

        await self.call("/chat/messages?peer=general&after=0", uid=1004)
        _, after_all = await self.call("/chat/unread", uid=1004)
        assert after_all["total"] == 0

    async def test_unread_counts_clear_per_conversation_when_opened(self):
        for uid in (1003, 1004, 1005):
            await self.accept(uid)
        await self.call(
            "/chat/messages", uid=1003, method="POST",
            body={"peerId": 4, "body": "Личное 1"},
        )
        await self.call(
            "/chat/messages", uid=1003, method="POST",
            body={"peerId": 4, "body": "Личное 2"},
        )
        await self.call(
            "/chat/messages", uid=1005, method="POST",
            body={"peerId": None, "body": "Общее"},
        )
        status, unread = await self.call("/chat/unread", uid=1004)
        assert status == 200
        assert unread["people"]["3"] == 2
        assert unread["general"] == 1
        assert unread["total"] == 3

        status, data = await self.call(
            "/chat/messages?peer=3&after=0", uid=1004
        )
        assert status == 200
        assert len(data["messages"]) == 2
        assert data["unread"]["people"].get("3", 0) == 0
        assert data["unread"]["general"] == 1

        status, data = await self.call(
            "/chat/messages?peer=general&after=0", uid=1004
        )
        assert status == 200
        assert data["unread"]["total"] == 0

    async def test_admin_cannot_monitor_other_people(self):
        await self.accept(1002)
        assert (await self.call("/chat/owner/threads", uid=1002))[0] == 403
        assert (await self.call("/chat/owner/messages?a=3&b=4&after=0", uid=1002))[0] == 403

    async def test_private_message_sends_telegram_push_without_body_preview(self):
        for uid in (1003, 1004):
            await self.accept(uid)
        self.bot.send_message.reset_mock()
        status, _ = await self.call(
            "/chat/messages", uid=1003, method="POST",
            body={"peerId": 4, "body": "Секретный рабочий текст"},
        )
        assert status == 201
        assert self.bot.send_message.await_count == 1
        call = self.bot.send_message.await_args
        assert call.args[0] == 1004
        assert "Секретный рабочий текст" not in call.args[1]
        assert "User 3" in call.args[1]
        assert call.kwargs["disable_notification"] is False
        assert call.kwargs["protect_content"] is True
        assert call.kwargs["reply_markup"] is not None

    async def test_general_message_notifies_other_active_staff_not_sender(self):
        await self.accept(1003)
        self.bot.send_message.reset_mock()
        status, _ = await self.call(
            "/chat/messages", uid=1003, method="POST",
            body={"peerId": None, "body": "Общая новость"},
        )
        assert status == 201
        recipients = {call.args[0] for call in self.bot.send_message.await_args_list}
        assert 1003 not in recipients
        assert {1001, 1002, 1004, 1005}.issubset(recipients)
        assert all("Общая новость" not in call.args[1] for call in self.bot.send_message.await_args_list)

    async def test_inactive_account_cannot_use_chat(self):
        assert (await self.call("/chat/rules", uid=1006))[0] == 403

    async def test_rate_limit_is_server_side(self):
        await self.accept(1003)
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.engine.inner.begin() as conn:
            for i in range(30):
                conn.execute(text(
                    "INSERT INTO work_chat_messages(sender_id,recipient_id,body,created_at) "
                    "VALUES (3,NULL,:body,:created)"
                ), {"body": f"m{i}", "created": now})
        status, _ = await self.call(
            "/chat/messages", uid=1003, method="POST",
            body={"peerId": None, "body": "31"},
        )
        assert status == 429

    async def test_retention_cleanup_removes_only_expired(self):
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.engine.inner.begin() as conn:
            conn.execute(text(
                "INSERT INTO work_chat_messages(sender_id,recipient_id,body,created_at) "
                "VALUES (3,NULL,'old',:old),(3,NULL,'new',:new)"
            ), {"old": now - timedelta(days=RETENTION_DAYS + 1), "new": now})
        assert await cleanup_expired_chat(self.engine) == 1
        with self.engine.inner.connect() as conn:
            assert conn.execute(text("SELECT body FROM work_chat_messages")).scalar() == "new"

    async def test_owner_control_view_is_audited_without_message_text(self):
        for uid in (1001,1003,1004):
            await self.accept(uid)
        await self.call(
            "/chat/messages", uid=1003, method="POST",
            body={"peerId": 4, "body": "Содержимое не должно попасть в аудит"},
        )
        await self.call("/chat/owner/messages?a=3&b=4&after=0", uid=1001)
        with self.engine.inner.connect() as conn:
            row = conn.execute(text(
                "SELECT action,details FROM audit_logs "
                "WHERE action='work_chat_owner_thread_opened' ORDER BY id DESC LIMIT 1"
            )).first()
        assert row[0] == "work_chat_owner_thread_opened"
        assert "Содержимое" not in row[1]

    async def test_only_owner_can_delete_only_their_own_messages(self):
        for uid in (1001, 1002, 1003, 1004, 1005):
            await self.accept(uid)
            _, result = await self.call("/chat/messages", uid=uid, method="POST",
                body={"peerId": None, "body": f"Message by {uid}"})
            message_id = result["message"]["id"]
            if uid != 1001:
                assert (await self.call(f"/chat/messages/{message_id}", uid=uid, method="DELETE"))[0] == 403
                assert (await self.call(f"/chat/messages/{message_id}", uid=1001, method="DELETE"))[0] == 404
            else:
                owner_id = message_id
        assert (await self.call(f"/chat/messages/{owner_id}", uid=1002, method="DELETE"))[0] == 403
        assert (await self.call(f"/chat/messages/{owner_id}", token="forged", method="DELETE"))[0] == 401
        self.bot.send_message.reset_mock()
        assert (await self.call(f"/chat/messages/{owner_id}", method="DELETE"))[0] == 200
        assert (await self.call(f"/chat/messages/{owner_id}", method="DELETE"))[0] == 200
        assert self.bot.send_message.await_count == 0
        with self.engine.inner.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM work_chat_messages")).scalar() == 4
            assert conn.execute(text("SELECT COUNT(*) FROM work_chat_deletions")).scalar() == 1
            audit = conn.execute(text("SELECT details FROM audit_logs WHERE action='work_chat_message_deleted'")).scalar()
            assert audit == "{}"

    async def test_deletion_changes_are_scoped_and_unread_count_disappears(self):
        for uid in (1001, 1003, 1004):
            await self.accept(uid)
        _, before = await self.call("/chat/messages?peer=1", uid=1003)
        _, result = await self.call("/chat/messages", method="POST",
            body={"peerId": 3, "body": "Delete this private text"})
        message_id = result["message"]["id"]
        assert (await self.call("/chat/unread", uid=1003))[1]["total"] == 1
        assert (await self.call(f"/chat/messages/{message_id}", method="DELETE"))[0] == 200
        _, changes = await self.call(f"/chat/messages?peer=1&after={message_id}&deletedAfter={before['deletionCursor']}", uid=1003)
        assert changes["messages"] == []
        assert changes["deletedIds"] == [message_id]
        assert changes["unread"]["total"] == 0
        for peer in ("general", "1", "3"):
            _, unrelated = await self.call(f"/chat/messages?peer={peer}&deletedAfter=0", uid=1004)
            assert unrelated["deletedIds"] == []
        _, latest = await self.call("/chat/messages?peer=1", uid=1003)
        assert latest["messages"] == []
        assert latest["deletionCursor"] == changes["deletionCursor"]
        _, again = await self.call(f"/chat/messages?peer=1&deletedAfter={changes['deletionCursor']}", uid=1003)
        assert again["deletedIds"] == []

    async def test_deleted_attachment_is_inaccessible_and_storage_cleanup_retries(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from app.work_chat import cleanup_orphan_attachments
        from app.yandex_disk import YandexDiskError
        await self.accept(1001)
        await self.accept(1003)
        with self.engine.inner.begin() as conn:
            conn.execute(text("INSERT INTO work_chat_attachments(id,uploader_id,storage_path) VALUES (12,1,'fixture-file')"))
            conn.execute(text("INSERT INTO work_chat_messages(sender_id,recipient_id,attachment_id,body,created_at) VALUES (1,3,12,'',:now)"), {"now": datetime.now(UTC).replace(tzinfo=None)})
            message_id = conn.execute(text("SELECT MAX(id) FROM work_chat_messages")).scalar()
        self.storage = SimpleNamespace(delete=AsyncMock(side_effect=YandexDiskError('fixture unavailable')))
        assert (await self.call(f"/chat/messages/{message_id}", method="DELETE"))[0] == 200
        for uid in (1001, 1003):
            assert (await self.call('/chat/attachments/12', uid=uid))[0] == 404
        with self.engine.inner.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM work_chat_attachments")).scalar() == 1
        self.storage.delete.side_effect = None
        await cleanup_orphan_attachments(self.engine, self.storage)
        self.storage.delete.assert_awaited_with('fixture-file')
        with self.engine.inner.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM work_chat_attachments")).scalar() == 0

    async def test_delete_requires_rules_and_rejects_bad_ids(self):
        assert (await self.call('/chat/messages/1', method='DELETE'))[0] == 428
        await self.accept(1001)
        for value in ('0', '-1', 'bad', '2147483648'):
            assert (await self.call(f'/chat/messages/{value}', method='DELETE'))[0] == 400

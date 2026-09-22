"""Real HTTP signaling tests with signed fixture users and no Telegram traffic."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import test_miniapp_release as baseline
import test_work_chat as chat_tests
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import text

from app.work_calls import (
    HEARTBEAT_TTL,
    MAX_DURATION,
    RING_TTL,
    ice_config,
    install_work_calls,
)
from app.work_chat import install_work_chat
from app.work_rules import WORK_RULES_VERSION, work_rules_hash


class CallsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await chat_tests.WorkChatTests.asyncSetUp(self)
        with self.engine.inner.begin() as conn:
            for uid in range(1, 8):
                conn.execute(text("""INSERT INTO work_rule_acceptances
                    (user_id,version,text_sha256) VALUES (:uid,:version,:sha)"""),
                    {"uid": uid, "version": WORK_RULES_VERSION, "sha": work_rules_hash()})
        app = web.Application()
        self.service.register(app)
        self.chat = install_work_chat(app, self.service)
        self.calls = install_work_calls(app, self.chat)
        self.calls.notify_start = AsyncMock()
        self.now = 1000
        self.calls.clock = lambda: self.now
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await chat_tests.WorkChatTests.asyncTearDown(self)

    async def request(self, path="", uid=3, body=None, token=None):
        headers = {"X-Telegram-Init-Data": baseline.signed(uid + 1000) if token is None else token}
        response = await self.client.request("GET" if body is None else "POST",
            "/api/miniapp/chat/calls" + path, json=body, headers=headers)
        return response.status, await response.json()

    async def start(self, uid=3, peer=4, mode="audio"):
        status, data = await self.request("/start", uid, {"peerId": peer, "mode": mode})
        assert status == 201, data
        return data

    async def join(self, data, uid=4, mode="audio"):
        status, joined = await self.request("/join", uid, {"callId": data["call"]["id"], "mode": mode})
        assert status == 200, joined
        return joined

    async def sync(self, data, uid=3, after=0):
        return await self.request("/sync", uid, {"callId": data["call"]["id"],
            "session": data["session"], "after": after, "audio": True, "video": False})

    async def leave(self, data, uid=3, end=False):
        return await self.request("/leave", uid, {"callId": data["call"]["id"],
            "session": data["session"], "endForAll": end})

    async def test_auth_rules_and_staff_required(self):
        assert (await self.request(token="forged"))[0] == 401
        for uid in (6, 7, 999):
            assert (await self.request(uid=uid))[0] == 403
        with self.engine.inner.begin() as conn:
            conn.execute(text("DELETE FROM work_rule_acceptances WHERE user_id=3"))
        assert (await self.request())[0] == 428

    async def test_private_calls_exclude_outsiders_and_owner(self):
        data = await self.start()
        for uid in (1, 2, 5):
            assert (await self.request(uid=uid))[1]["calls"] == []
            assert (await self.request("/join", uid, {"callId": data["call"]["id"], "mode": "audio"}))[0] == 404
        assert len((await self.request(uid=4))[1]["calls"]) == 1
        assert (await self.request("/start", 3, {"peerId": 5, "mode": "video"}))[0] == 409

    async def test_signal_is_session_scoped_and_delivered_once(self):
        data = await self.start(mode="video")
        joined = await self.join(data, mode="video")
        payload = {"callId": data["call"]["id"], "session": data["session"],
                   "to": 4, "toSession": joined["session"], "data": {"type": "offer", "sdp": "v=0\n" + "x" * 9000}}
        assert (await self.request("/signal", 3, payload))[0] == 200
        _, receiver = await self.sync(joined, 4)
        assert receiver["signals"][0]["data"] == payload["data"]
        assert (await self.sync(data, 3))[1]["signals"] == []
        assert (await self.sync(joined, 4, receiver["cursor"]))[1]["signals"] == []
        for update in ({"session": "wrong"}, {"to": 5}, {"toSession": "stale"}):
            assert (await self.request("/signal", 3, {**payload, **update}))[0] == 409
        assert (await self.request("/signal", 5, payload))[0] == 404
        assert (await self.request("/signal", 3, {**payload, "data": {"type": "offer", "sdp": "x" * 70000}}))[0] == 413

    async def test_retried_signals_are_not_delivered_twice(self):
        data = await self.start(mode="video")
        joined = await self.join(data, mode="video")
        payload = {"callId": data["call"]["id"], "session": data["session"],
            "to": 4, "toSession": joined["session"], "signalId": "fixture-signal-0001",
            "data": {"type": "offer", "sdp": "v=0"}}
        responses = await asyncio.gather(*(self.request("/signal", 3, payload) for _ in range(3)))
        assert all(status == 200 for status, _ in responses)
        _, receiver = await self.sync(joined, 4)
        assert len(receiver["signals"]) == 1
        await self.sync(joined, 4, receiver["cursor"])
        assert (await self.request("/signal", 3, payload))[0] == 200
        assert (await self.sync(joined, 4, receiver["cursor"]))[1]["signals"] == []
        assert (await self.request("/signal", 3, {**payload, "data": {"type": "offer", "sdp": "changed"}}))[0] == 409

    async def test_attachment_retry_is_atomic_and_removes_redundant_upload(self):
        from types import SimpleNamespace

        from aiohttp import FormData
        storage = SimpleNamespace(state={"connected": True}, ensure_dir=AsyncMock(),
            upload_bytes=AsyncMock(), delete=AsyncMock())
        self.client.server.app["yandex_disk"] = storage
        async def upload(caption="caption"):
            form = FormData()
            for key, value in {"peerId": "4", "caption": caption, "clientId": "fixture-upload-0001"}.items():
                form.add_field(key, value)
            form.add_field("file", b"%PDF-1.7 fixture", filename="document.pdf", content_type="application/pdf")
            response = await self.client.post("/api/miniapp/chat/attachments", data=form,
                headers={"X-Telegram-Init-Data": baseline.signed(1003)})
            return response.status, await response.json()
        first, second = await upload(), await upload()
        assert first[0] == 201 and second[0] == 200
        assert first[1]["message"] == second[1]["message"]
        assert storage.delete.await_count == 1
        assert (await upload("changed"))[0] == 409
        assert storage.delete.await_count == 2
        with self.engine.inner.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM work_chat_messages")) == 1
            assert conn.scalar(text("SELECT count(*) FROM work_chat_attachments")) == 1

    async def test_decline_and_direct_leave_end_call(self):
        data = await self.start()
        assert (await self.request("/decline", 4, {"callId": data["call"]["id"]}))[0] == 200
        assert (await self.sync(data))[0] == 404
        data = await self.start()
        joined = await self.join(data)
        assert (await self.leave(joined, 4))[0] == 200
        assert (await self.sync(data))[0] == 404

    async def test_group_join_leave_rejoin_and_host_permissions(self):
        data = await self.start(peer=None)
        joined = await self.join(data)
        third = await self.join(data, 5, "video")
        assert len((await self.sync(data))[1]["call"]["participants"]) == 3
        assert (await self.leave(third, 5, end=True))[0] == 403
        assert (await self.leave(joined, 4))[0] == 200
        rejoined = await self.join(data)
        assert rejoined["session"] != joined["session"]
        assert (await self.sync(joined, 4))[0] == 409
        assert (await self.leave(data, 3, end=True))[0] == 200
        assert (await self.sync(third, 5))[0] == 404

    async def test_concurrent_starts_and_group_capacity(self):
        results = await asyncio.gather(*[self.request("/start", 3, {"peerId": None, "mode": "audio"}) for _ in range(3)])
        assert sorted(s for s, _ in results) == [201, 409, 409]
        data = next(d for s, d in results if s == 201)
        with self.engine.inner.begin() as conn:
            for uid in (8, 9):
                conn.execute(text("INSERT INTO users VALUES (:uid,:tg,'Extra',TRUE)"), {"uid": uid, "tg": uid+1000})
                conn.execute(text("INSERT INTO user_roles VALUES (:uid,'PHOTOGRAPHER')"), {"uid": uid})
                conn.execute(text("INSERT INTO work_rule_acceptances(user_id,version,text_sha256) VALUES (:uid,:v,:sha)"),
                             {"uid": uid, "v": WORK_RULES_VERSION, "sha": work_rules_hash()})
        for uid in (1, 2, 4, 5, 8):
            await self.join(data, uid)
        assert (await self.request("/join", 9, {"callId": data["call"]["id"], "mode": "audio"}))[0] == 409

    async def test_heartbeat_timeout_and_revoked_employee(self):
        data = await self.start(peer=None)
        joined = await self.join(data)
        self.now += HEARTBEAT_TTL - 1
        assert (await self.sync(data))[0] == 200
        self.now += 2
        assert (await self.sync(joined, 4))[0] == 409
        assert len((await self.sync(data))[1]["call"]["participants"]) == 1
        joined = await self.join(data)
        with self.engine.inner.begin() as conn:
            conn.execute(text("UPDATE users SET active=FALSE WHERE id=4"))
        assert len((await self.sync(data))[1]["call"]["participants"]) == 1
        assert (await self.sync(joined, 4))[0] == 403

    async def test_ring_and_maximum_duration(self):
        data = await self.start()
        self.now += 50
        await self.sync(data)
        self.now += RING_TTL - 49
        assert (await self.sync(data))[0] == 404
        data = await self.start(peer=None)
        await self.join(data)
        room = self.calls.rooms[data["call"]["id"]]
        room.created -= MAX_DURATION + 1
        assert (await self.sync(data))[0] == 404

    async def test_device_session_and_payload_validation(self):
        data = await self.start()
        assert (await self.request("/join", 3, {"callId": data["call"]["id"], "mode": "audio"}))[0] == 409
        assert (await self.request("/start", 4, {"peerId": True, "mode": "audio"}))[0] == 400
        assert (await self.request("/start", 4, {"peerId": None, "mode": "record"}))[0] == 400
        assert (await self.sync(data, after=999))[0] == 409

    async def test_history_survives_room_loss_and_excludes_outsiders(self):
        call = await self.start(3, 4)
        await self.join(call, 4)
        self.now += 12
        await self.leave(call, 3)
        status, history = await self.request('/history', 3)
        assert status == 200
        assert history['items'][0]['status'] == 'completed'
        assert history['items'][0]['durationSeconds'] == 12
        assert (await self.request('/history', 1))[1]['items'] == []
        assert (await self.request('/history', 4))[1]['items'][0]['callId'] == call['call']['id']
        await self.start(3, 4)
        self.calls.rooms.clear()
        assert (await self.request('/history', 3))[1]['items'][0]['status'] == 'interrupted'


def test_turn_credentials_are_generated_only_on_server_and_expire():
    with patch.dict("os.environ", {"CALLS_TURN_URLS": "turns:relay.example:5349", "CALLS_TURN_SECRET": "fixture-secret"}):
        data = ice_config(3)
    assert data["relayConfigured"] is True
    assert "fixture-secret" not in str(data)
    assert data["iceServers"][1]["username"].endswith(":pb-3")

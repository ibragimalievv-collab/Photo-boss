import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_miniapp_release as baseline
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import text

from app.academy_practice import install_academy_practice
from app.services.academy import ACADEMY_BLOCKS
from app.services.training import CATEGORY_BY_SLUG
from app.yandex_disk import YandexDiskError


def test_every_academy_block_has_available_practice_and_five_reference_photos():
    for block in ACADEMY_BLOCKS:
        assert block.practice_categories
        for slug in block.practice_categories:
            category = CATEGORY_BY_SLUG[slug]
            assert len(category.image_paths) == 5
            assert all(path.is_file() for path in category.image_paths)


class PracticeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await baseline.MiniAppTests.asyncSetUp(self)
        with self.engine.inner.begin() as conn:
            for name in ("assigned_date", "assigned_at", "completed_at", "review_source"):
                conn.execute(text(f"ALTER TABLE training_assignments ADD COLUMN {name} TEXT"))
            conn.execute(text("""CREATE TABLE training_submissions(id INTEGER PRIMARY KEY,
                assignment_id INTEGER,pose_index INTEGER,reference_filename TEXT,
                submitted_file_id TEXT,submitted_at TEXT,UNIQUE(assignment_id,pose_index))"""))
        self.files = {}

        async def upload(path, data, **kwargs):
            self.files[path] = data

        async def download(path, **kwargs):
            return self.files[path]

        self.storage = SimpleNamespace(ensure_dir=AsyncMock(), upload_bytes=AsyncMock(side_effect=upload),
                                       download_bytes=AsyncMock(side_effect=download), delete=AsyncMock())
        app = web.Application(client_max_size=10 * 1024 * 1024)
        self.service.register(app)
        self.practice = install_academy_practice(app, self.service)
        app["yandex_disk"] = self.storage
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await baseline.MiniAppTests.asyncTearDown(self)

    async def request(self, path="", uid=3, body=None, payload=None, token=None):
        headers = {"X-Telegram-Init-Data": baseline.signed(uid + 1000) if token is None else token}
        kwargs = {}
        if payload is not None:
            form = FormData(); form.add_field("file", payload, filename="practice.jpg", content_type="image/jpeg")
            kwargs["data"] = form
        elif body is not None:
            kwargs["json"] = body
        response = await self.client.request("POST" if kwargs else "GET",
            "/api/miniapp/academy/practice" + path, headers=headers, **kwargs)
        if response.content_type == "image/jpeg":
            return response.status, await response.read()
        return response.status, await response.json()

    def finish_lessons(self):
        with self.engine.inner.begin() as conn:
            conn.execute(text("INSERT INTO academy_lesson_progress(user_id,topic_slug) VALUES(3,'light')"))

    async def start(self):
        self.finish_lessons()
        status, data = await self.request("/start", body={"category": "woman"})
        assert status == 201, data
        return data["id"]

    async def upload_all(self, aid):
        for pose in range(1, 6):
            status, data = await self.request(f"/{aid}/photos/{pose}", payload=b"\xff\xd8\xfffixture"+bytes([pose])+b"\xff\xd9")
            assert status == 201, data
        return data

    async def test_auth_gating_and_private_photo_access(self):
        assert (await self.request(token="forged"))[0] == 401
        assert (await self.request(uid=6))[0] == 403
        assert (await self.request("/start", body={"category": []}))[0] == 400
        assert (await self.request("/start", body={"category": "woman"}))[0] == 409
        aid = await self.start()
        assert (await self.request("/start", body={"category": "child"}))[1]["id"] == aid
        assert (await self.request(f"/{aid}", uid=4))[0] == 404
        assert (await self.request(f"/{aid}", uid=1))[0] == 200
        await self.upload_all(aid)
        assert (await self.request(f"/{aid}/photos/1", uid=4))[0] == 404
        assert (await self.request(f"/{aid}/photos/1", uid=1))[0] == 200
        assert (await self.request(f"/{aid}/photos/1", uid=3))[0] == 200

    async def test_upload_retries_duplicates_and_storage_failure(self):
        aid = await self.start()
        path = f"/{aid}/photos/1"
        assert (await self.request(path, payload=b"<script>bad</script>"))[0] == 415
        payload = b"\xff\xd8\xfffixture\xff\xd9"
        results = await asyncio.gather(self.request(path, payload=payload), self.request(path, payload=payload))
        assert sorted(status for status, _ in results) == [201, 409]
        assert (await self.request(f"/{aid}/photos/2", payload=payload))[0] == 409
        self.storage.upload_bytes.side_effect = YandexDiskError("offline")
        assert (await self.request(f"/{aid}/photos/2", payload=payload[:-2]+b"2\xff\xd9"))[0] == 503
        assert sum(s["uploaded"] for s in (await self.request(f"/{aid}"))[1]["shots"]) == 1

    async def test_owner_review_reshoot_and_progress_unlock(self):
        aid = await self.start()
        data = await self.upload_all(aid)
        assert data["status"] == "PENDING_REVIEW"
        await self.practice.process_pending(self.storage)
        assert (await self.request(f"/{aid}"))[1]["reviewSource"] == "OWNER"
        assert (await self.request(uid=1))[1]["queue"][0]["id"] == aid
        body = {"decision": "revision", "indexes": [2], "comment": "Измените ракурс второго кадра."}
        assert (await self.request(f"/{aid}/review", body=body, uid=3))[0] == 403
        _, revised = await self.request(f"/{aid}/review", body=body, uid=1)
        assert revised["status"] == "ACTIVE"
        assert [s["index"] for s in revised["shots"] if not s["uploaded"]] == [2]
        assert revised["analysis"]["comment"] == body["comment"]
        await self.request(f"/{aid}/photos/2", payload=b"\xff\xd8\xffreshoot\xff\xd9")
        body = {"decision": "accept", "indexes": [], "comment": "Отлично"}
        _, accepted = await self.request(f"/{aid}/review", body=body, uid=1)
        assert accepted["status"] == "COMPLETED"
        assert (await self.request())[1]["todayDone"] is True
        assert (await self.request("/start", body={"category": "child"}))[0] == 409
        assert self.bot.send_message.await_count == 0

    async def test_ai_review_saves_result_without_sending_user_out_of_app(self):
        aid = await self.start()
        await self.upload_all(aid)
        result = {"status": "completed", "review": {"decision": "ACCEPT", "score": 90,
                  "reshoot_indexes": [], "issues": [], "strengths": ["Хороший свет"], "next_action": "Следующий блок"}}
        with patch("app.academy_practice.config", SimpleNamespace(openai_api_key="fixture")), \
                patch("app.academy_practice.analyze_training_set", AsyncMock(return_value=result)):
            await self.practice.process_pending(self.storage)
        _, data = await self.request(f"/{aid}")
        assert data["status"] == "COMPLETED" and data["score"] == 90
        assert data["analysis"] == result
        assert self.bot.send_message.await_count == 0

    async def test_old_accepted_practice_is_not_lost_after_thirty_new_assignments(self):
        self.finish_lessons()
        with self.engine.inner.begin() as conn:
            conn.execute(text("INSERT INTO training_assignments(id,user_id,category_slug,status) VALUES(1,3,'woman','COMPLETED')"))
            for i in range(2,34):
                conn.execute(text("INSERT INTO training_assignments(id,user_id,category_slug,status) VALUES(:id,3,'man','ACTIVE')"),{'id':i})
        response=await self.client.get('/api/miniapp/academy',headers={'X-Telegram-Init-Data':baseline.signed(1003)})
        data=await response.json()
        assert response.status==200
        assert data['blocks'][0]['practiceDone']
        assert not data['blocks'][1]['locked']
        assert len(data['practices'])==30

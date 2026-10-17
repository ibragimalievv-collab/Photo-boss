import asyncio
import json
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

    async def test_ai_only_review_retry_reshoot_and_archived_originals(self):
        aid = await self.start()
        data = await self.upload_all(aid)
        assert data["status"] == "PENDING_REVIEW"
        await self.practice.process_pending(self.storage)
        assert (await self.request(f"/{aid}"))[1]["reviewSource"] == "AI_FAILED"
        body = {"decision": "accept", "indexes": [], "comment": "Manual"}
        for uid in (1, 2, 3):
            assert (await self.request(f"/{aid}/review", body=body, uid=uid))[0] == 403
        assert (await self.request(f"/{aid}/retry", body={}, uid=4))[0] == 404
        assert (await self.request(f"/{aid}/retry", body={}))[0] == 200
        revised_result = {"status": "completed", "review": {"decision": "REVISION", "score": 75,
                          "reshoot_indexes": [2], "issues": ["Другой ракурс"], "strengths": [], "next_action": "Переснимите второй кадр"}}
        with patch("app.academy_practice.config", SimpleNamespace(openai_api_key="fixture")), \
                patch("app.academy_practice.analyze_training_set", AsyncMock(return_value=revised_result)):
            await self.practice.process_pending(self.storage)
        revised = (await self.request(f"/{aid}"))[1]
        assert revised["status"] == "ACTIVE"
        assert [s["index"] for s in revised["shots"] if not s["uploaded"]] == [2]
        with self.engine.inner.begin() as conn:
            import json
            archive = conn.execute(text("SELECT photos FROM academy_practice_reviews WHERE assignment_id=:id"), {"id": aid}).scalar()
            assert len(json.loads(archive)) == 5
            assert all(p['file'][len('academy-disk:'):] in self.files for p in json.loads(archive))
        await self.request(f"/{aid}/photos/2", payload=b"\xff\xd8\xffreshoot\xff\xd9")
        accepted_result = {"status": "completed", "review": {"decision": "ACCEPT", "score": 90,
                          "reshoot_indexes": [], "issues": [], "strengths": ["Разные планы"], "next_action": "Следующий блок"}}
        with patch("app.academy_practice.config", SimpleNamespace(openai_api_key="fixture")), \
                patch("app.academy_practice.analyze_training_set", AsyncMock(return_value=accepted_result)):
            await self.practice.process_pending(self.storage)
        assert (await self.request(f"/{aid}"))[1]["status"] == "COMPLETED"
        assert (await self.request())[1]["todayDone"] is True
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

    async def test_stalled_review_reopens_only_unreadable_storage_photo(self):
        aid = await self.start()
        await self.upload_all(aid)
        with self.engine.inner.begin() as conn:
            conn.execute(text("""UPDATE training_assignments
                SET review_source='AI_FAILED',ai_analysis='{"status":"unavailable"}'
                WHERE id=:id"""), {"id": aid})

        original_download = self.storage.download_bytes.side_effect

        async def unavailable_second(path, **kwargs):
            if f"/{aid}-2-" in path:
                raise YandexDiskError("provider 500")
            return await original_download(path, **kwargs)

        self.storage.download_bytes.side_effect = unavailable_second
        await self.practice.recover_stalled_storage(self.storage)
        _, data = await self.request(f"/{aid}")
        assert data["status"] == "ACTIVE"
        assert data["reviewSource"] == "STORAGE_RETRY"
        assert data["analysis"] == {"status": "storage_unavailable", "unavailable_indexes": [2]}
        assert [shot["index"] for shot in data["shots"] if not shot["uploaded"]] == [2]
        with self.engine.inner.begin() as conn:
            archive = conn.execute(text(
                "SELECT photos FROM academy_practice_reviews WHERE assignment_id=:id"
            ), {"id": aid}).scalar()
            assert len(json.loads(archive)) == 5

    async def test_stalled_review_with_readable_photos_is_requeued(self):
        aid = await self.start()
        await self.upload_all(aid)
        with self.engine.inner.begin() as conn:
            conn.execute(text("""UPDATE training_assignments
                SET review_source='AI_FAILED',ai_analysis='{"status":"unavailable"}'
                WHERE id=:id"""), {"id": aid})

        await self.practice.recover_stalled_storage(self.storage)
        _, queued = await self.request(f"/{aid}")
        assert queued["status"] == "PENDING_REVIEW"
        assert queued["reviewSource"] == "AI_PENDING"

        accepted_result = {"status": "completed", "review": {"decision": "ACCEPT", "score": 91,
                          "reshoot_indexes": [], "issues": [], "strengths": ["Хорошая серия"],
                          "next_action": "Следующий блок"}}
        with patch("app.academy_practice.config", SimpleNamespace(openai_api_key="fixture")), \
                patch("app.academy_practice.analyze_training_set", AsyncMock(return_value=accepted_result)):
            await self.practice.process_pending(self.storage)
        _, completed = await self.request(f"/{aid}")
        assert completed["status"] == "COMPLETED"
        assert completed["score"] == 91

    async def test_transient_review_retries_three_times_then_exposes_failure(self):
        aid = await self.start()
        await self.upload_all(aid)
        failure = {"status": "unavailable", "errorCode": "timeout", "retryable": True,
                   "message": "Провайдер не ответил вовремя."}
        analyzer = AsyncMock(return_value=failure)
        with patch("app.academy_practice.config", SimpleNamespace(openai_api_key="fixture")), \
                patch("app.academy_practice.analyze_training_set", analyzer):
            for attempt in range(1, 4):
                await self.practice.process_pending(self.storage)
                _, data = await self.request(f"/{aid}")
                assert data["analysis"]["attempts"] == attempt
                assert data["reviewSource"] == ("AI_PENDING" if attempt < 3 else "AI_FAILED")
                assert data["canRetry"] is (attempt == 3)
                await self.practice.process_pending(self.storage)
                assert analyzer.await_count == attempt
                if attempt < 3:
                    with self.engine.inner.begin() as conn:
                        changed = data["analysis"] | {"nextRetryAt": "2000-01-01T00:00:00+00:00"}
                        conn.execute(text("UPDATE training_assignments SET ai_analysis=:data WHERE id=:id"),
                                     {"id": aid, "data": json.dumps(changed)})
            await self.practice.recover_stalled_storage(self.storage)
            await self.practice.process_pending(self.storage)
            assert analyzer.await_count == 3

    async def test_nonretryable_quota_failure_does_not_repeat_on_worker_or_startup(self):
        aid = await self.start()
        await self.upload_all(aid)
        failure = {"status": "unavailable", "errorCode": "insufficient_quota", "retryable": False,
                   "message": "Лимит API исчерпан."}
        analyzer = AsyncMock(return_value=failure)
        with patch("app.academy_practice.config", SimpleNamespace(openai_api_key="fixture")), \
                patch("app.academy_practice.analyze_training_set", analyzer):
            await self.practice.process_pending(self.storage)
            await self.practice.recover_stalled_storage(self.storage)
            await self.practice.process_pending(self.storage)
        assert analyzer.await_count == 1
        _, data = await self.request(f"/{aid}")
        assert data["reviewSource"] == "AI_FAILED"
        assert data["analysis"]["errorCode"] == "insufficient_quota"

    async def test_provider_call_is_claimed_across_worker_instances(self):
        from app.academy_practice import AcademyPractice

        aid = await self.start()
        await self.upload_all(aid)
        second_worker = AcademyPractice(self.service)
        entered, release = asyncio.Event(), asyncio.Event()

        async def analyze(*args):
            entered.set()
            await release.wait()
            return {"status": "completed", "review": {"decision": "ACCEPT", "score": 90,
                    "reshoot_indexes": [], "issues": [], "strengths": [], "next_action": "Далее"}}

        analyzer = AsyncMock(side_effect=analyze)
        with patch("app.academy_practice.config", SimpleNamespace(openai_api_key="fixture")), \
                patch("app.academy_practice.analyze_training_set", analyzer):
            running = asyncio.create_task(self.practice.process_pending(self.storage))
            await asyncio.wait_for(entered.wait(), timeout=2)
            _, checking = await self.request(f"/{aid}")
            assert checking["reviewSource"] == "AI_PROCESSING" and not checking["canRetry"]
            await second_worker.process_pending(self.storage)
            assert analyzer.await_count == 1
            release.set()
            await running
        assert (await self.request(f"/{aid}"))[1]["status"] == "COMPLETED"

    async def test_ready_review_is_not_starved_by_first_twenty_deferred_rows(self):
        from datetime import UTC, datetime

        aid = await self.start()
        await self.upload_all(aid)
        ready_id = 25
        with self.engine.inner.begin() as conn:
            conn.execute(text("UPDATE training_assignments SET id=:ready WHERE id=:id"),
                         {"ready": ready_id, "id": aid})
            conn.execute(text("UPDATE training_submissions SET assignment_id=:ready WHERE assignment_id=:id"),
                         {"ready": ready_id, "id": aid})
            for index in range(1, ready_id):
                source = "AI_PENDING" if index % 2 else "AI_PROCESSING"
                metadata = ({"nextRetryAt": "2099-01-01T00:00:00+00:00"} if index % 2 else
                            {"processingStartedAt": datetime.now(UTC).isoformat(), "processingLease": f"held-{index}"})
                conn.execute(text("""INSERT INTO training_assignments
                    (id,user_id,category_slug,status,review_source,ai_analysis)
                    VALUES (:id,3,'woman','PENDING_REVIEW',:source,:data)"""),
                    {"id": index, "source": source, "data": json.dumps(metadata)})
        result = {"status": "completed", "review": {"decision": "ACCEPT", "score": 90,
                  "reshoot_indexes": [], "issues": [], "strengths": [], "next_action": "Далее"}}
        analyzer = AsyncMock(return_value=result)
        with patch("app.academy_practice.config", SimpleNamespace(openai_api_key="fixture")), \
                patch("app.academy_practice.analyze_training_set", analyzer):
            outcomes = await self.practice.process_pending(self.storage)
        assert set(outcomes) == {ready_id}
        assert analyzer.await_count == 1
        assert (await self.request(f"/{ready_id}"))[1]["status"] == "COMPLETED"
        with self.engine.inner.connect() as conn:
            assert conn.execute(text("""SELECT COUNT(*) FROM training_assignments
                WHERE id<25 AND status='PENDING_REVIEW'""")).scalar() == 24

    async def test_stale_processing_lease_is_recovered_without_photo_reupload(self):
        aid = await self.start()
        await self.upload_all(aid)
        with self.engine.inner.begin() as conn:
            conn.execute(text("""UPDATE training_assignments SET review_source='AI_PROCESSING',
                ai_analysis=:data WHERE id=:id"""), {"id": aid, "data": json.dumps({
                    "status": "processing", "processingStartedAt": "2000-01-01T00:00:00+00:00",
                    "processingLease": "old", "attempts": 0})})
        result = {"status": "completed", "review": {"decision": "REVISION", "score": 75,
                  "reshoot_indexes": [3], "issues": ["Поменяйте ракурс"], "strengths": [],
                  "next_action": "Переснимите третий кадр"}}
        with patch("app.academy_practice.config", SimpleNamespace(openai_api_key="fixture")), \
                patch("app.academy_practice.analyze_training_set", AsyncMock(return_value=result)):
            await self.practice.process_pending(self.storage)
        _, catalog = await self.request()
        assert catalog["assignment"]["status"] == "ACTIVE"
        assert catalog["history"][0]["analysis"]["review"]["next_action"] == "Переснимите третий кадр"
        assert catalog["history"][0]["score"] == 75

    async def test_expired_storage_worker_cannot_reopen_newer_review_claim(self):
        aid = await self.start()
        await self.upload_all(aid)
        original_download = self.storage.download_bytes.side_effect
        replacement = {"status": "processing", "processingLease": "new-worker",
                       "processingStartedAt": "2099-01-01T00:00:00+00:00"}

        async def superseded_download(path, **kwargs):
            if f"/{aid}-2-" in path:
                # A paused worker's lease expired while reading storage and a
                # replacement worker has already claimed the same assignment.
                with self.engine.inner.begin() as conn:
                    conn.execute(text("UPDATE training_assignments SET ai_analysis=:data WHERE id=:id"),
                                 {"id": aid, "data": json.dumps(replacement)})
                raise YandexDiskError("old download failed")
            return await original_download(path, **kwargs)

        self.storage.download_bytes.side_effect = superseded_download
        with patch("app.academy_practice.config", SimpleNamespace(openai_api_key="fixture")):
            assert await self.practice.process_pending(self.storage) == {}
        _, data = await self.request(f"/{aid}")
        assert data["status"] == "PENDING_REVIEW"
        assert data["reviewSource"] == "AI_PROCESSING"
        assert data["analysis"] == replacement
        assert all(shot["uploaded"] for shot in data["shots"])
        with self.engine.inner.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM academy_practice_reviews")).scalar() == 0

    async def test_startup_storage_recovery_does_not_replace_newer_failure(self):
        aid = await self.start()
        await self.upload_all(aid)
        with self.engine.inner.begin() as conn:
            conn.execute(text("""UPDATE training_assignments SET review_source='AI_FAILED',
                ai_analysis='{"status":"unavailable"}' WHERE id=:id"""), {"id": aid})
        original_download = self.storage.download_bytes.side_effect
        failure = {"status": "unavailable", "errorCode": "insufficient_quota",
                   "retryable": False, "attempts": 1}

        async def newer_failure(path, **kwargs):
            with self.engine.inner.begin() as conn:
                conn.execute(text("UPDATE training_assignments SET ai_analysis=:data WHERE id=:id"),
                             {"id": aid, "data": json.dumps(failure)})
            return await original_download(path, **kwargs)

        self.storage.download_bytes.side_effect = newer_failure
        await self.practice.recover_stalled_storage(self.storage)
        _, data = await self.request(f"/{aid}")
        assert data["reviewSource"] == "AI_FAILED"
        assert data["analysis"] == failure
        assert all(shot["uploaded"] for shot in data["shots"])

    async def test_startup_recovery_scans_past_capped_failed_rows(self):
        aid = await self.start()
        await self.upload_all(aid)
        with self.engine.inner.begin() as conn:
            conn.execute(text("""UPDATE training_assignments SET id=25,review_source='AI_FAILED',
                ai_analysis='{"status":"unavailable"}' WHERE id=:id"""), {"id": aid})
            conn.execute(text("UPDATE training_submissions SET assignment_id=25 WHERE assignment_id=:id"), {"id": aid})
            for index in range(1, 25):
                conn.execute(text("""INSERT INTO training_assignments
                    (id,user_id,category_slug,status,review_source,ai_analysis)
                    VALUES (:id,3,'woman','PENDING_REVIEW','AI_FAILED',:data)"""),
                    {"id": index, "data": json.dumps({"status": "unavailable", "attempts": 3})})
        await self.practice.recover_stalled_storage(self.storage)
        assert (await self.request("/25"))[1]["reviewSource"] == "AI_PENDING"
        with self.engine.inner.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM training_assignments WHERE review_source='AI_FAILED'")).scalar() == 24

    async def test_corrupt_stored_photo_reopens_only_invalid_slot(self):
        aid = await self.start()
        await self.upload_all(aid)
        key = next(path for path in self.files if f"/{aid}-3-" in path)
        self.files[key] = b"<html>upstream error</html>"
        with patch("app.academy_practice.config", SimpleNamespace(openai_api_key="fixture")):
            await self.practice.process_pending(self.storage)
        _, data = await self.request(f"/{aid}")
        assert data["status"] == "ACTIVE" and data["reviewSource"] == "STORAGE_RETRY"
        assert [shot["index"] for shot in data["shots"] if not shot["uploaded"]] == [3]


    async def test_uncertain_ai_result_has_retake_path_without_manual_acceptance(self):
        aid = await self.start()
        await self.upload_all(aid)
        result = {"status": "completed", "review": {"decision": "MANUAL_REVIEW", "score": 65,
                  "reshoot_indexes": [2, 4], "issues": ["Недостаточно деталей"], "strengths": [],
                  "next_action": "Снимите второй и четвёртый кадры при лучшем освещении"}}
        with patch("app.academy_practice.config", SimpleNamespace(openai_api_key="fixture")), \
                patch("app.academy_practice.analyze_training_set", AsyncMock(return_value=result)):
            await self.practice.process_pending(self.storage)
        _, data = await self.request(f"/{aid}")
        assert data["status"] == "PENDING_REVIEW"
        assert data["reviewSource"] == "AI_UNCERTAIN"
        assert data["canRetake"] and data["canRetry"]
        assert (await self.request(f"/{aid}/retake", uid=4, body={"indexes": [2, 4]}))[0] == 404
        assert (await self.request(f"/{aid}/retake", uid=1, body={"indexes": [2, 4]}))[0] == 403
        assert (await self.request(f"/{aid}/retake", body={"indexes": [0]}))[0] == 400
        assert (await self.request(f"/{aid}/retake", body={"indexes": [2, 2]}))[0] == 400
        status, changed = await self.request(f"/{aid}/retake", body={"indexes": [2, 4]})
        assert status == 200 and changed["status"] == "ACTIVE"
        assert [s["index"] for s in changed["shots"] if s["uploaded"]] == [1, 3, 5]
        assert len(self.files) == 5
        self.storage.delete.assert_not_awaited()
        with self.engine.inner.begin() as conn:
            import json
            archive = conn.execute(text("SELECT photos FROM academy_practice_reviews WHERE assignment_id=:id"), {"id": aid}).scalar()
            assert len(json.loads(archive)) == 5
        assert (await self.request(f"/{aid}/review", body={"decision": "accept"}))[0] == 403

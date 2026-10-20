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
            conn.execute(text("""CREATE TABLE IF NOT EXISTS operation_requests(id INTEGER PRIMARY KEY,
                user_id INTEGER,request_key TEXT,payload_hash TEXT,result TEXT,created_at DATETIME,
                UNIQUE(user_id,request_key))"""))
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

    async def test_admin_practice_access_requires_current_shared_hotel(self):
        aid = await self.start()
        await self.upload_all(aid)
        with self.engine.inner.begin() as conn:
            conn.execute(text("""INSERT INTO training_assignments
                (id,user_id,category_slug,status,review_source)
                VALUES (100,5,'woman','PENDING_REVIEW','AI_FAILED'),
                       (101,2,'woman','PENDING_REVIEW','AI_FAILED')"""))
        # An administrator without hotels has only their own Academy work.
        assert (await self.request(f"/{aid}", uid=2))[0] == 404
        assert (await self.request(f"/{aid}/photos/1", uid=2))[0] == 404
        assert (await self.request(f"/{aid}/retry", uid=2, body={}))[0] == 404
        assert (await self.request(f"/{aid}/retake", uid=2, body={"indexes": [1]}))[0] == 404
        assert (await self.request(f"/{aid}/photos/1", uid=2,
                                   payload=b"\xff\xd8\xffadmin\xff\xd9"))[0] == 404
        assert (await self.request("/101", uid=2))[0] == 200
        assert [r["id"] for r in (await self.request(uid=2))[1]["queue"]] == [101]
        with self.engine.inner.begin() as conn:
            conn.execute(text("INSERT INTO hotel_employees VALUES (2,1),(3,1),(5,2)"))
            conn.execute(text("UPDATE training_assignments SET review_source='AI_FAILED' WHERE id=:id"), {"id": aid})
        # Supervising one hotel permits its staff's result, photo and deliberate
        # retry, while another hotel's assignment remains undiscoverable.
        assert (await self.request(f"/{aid}", uid=2))[0] == 200
        assert (await self.request(f"/{aid}/photos/1", uid=2))[0] == 200
        assert (await self.request(f"/{aid}/retry", uid=2, body={}))[0] == 200
        assert (await self.request("/100", uid=2))[0] == 404
        assert (await self.request("/100/retry", uid=2, body={}))[0] == 404
        assert [r["id"] for r in (await self.request(uid=2))[1]["queue"]] == [aid, 101]
        # Viewing a trainee does not allow submitting or retaking for them.
        assert (await self.request(f"/{aid}/photos/1", uid=2,
                                   payload=b"\xff\xd8\xffadmin\xff\xd9"))[0] == 409
        assert (await self.request(f"/{aid}/retake", uid=2, body={"indexes": [1]}))[0] == 403
        assert (await self.request(f"/{aid}/review", uid=2, body={}))[0] == 403
        with self.engine.inner.begin() as conn:
            conn.execute(text("DELETE FROM hotel_employees WHERE user_id=2"))
        # A previously authorized URL cannot retain access after reassignment.
        assert (await self.request(f"/{aid}", uid=2))[0] == 404
        assert (await self.request(f"/{aid}/photos/1", uid=2))[0] == 404
        assert (await self.request(f"/{aid}/retry", uid=2, body={}))[0] == 404
        assert [r["id"] for r in (await self.request(uid=2))[1]["queue"]] == [101]
        assert (await self.request(f"/{aid}", uid=1))[0] == 200
        assert (await self.request(f"/{aid}/photos/1", uid=1))[0] == 200
        assert [r["id"] for r in (await self.request(uid=1))[1]["queue"]] == [aid, 100, 101]
        assert sum(s["uploaded"] for s in (await self.request(f"/{aid}"))[1]["shots"]) == 5

    async def test_upload_retries_duplicates_and_storage_failure(self):
        aid = await self.start()
        path = f"/{aid}/photos/1"
        assert (await self.request(path, payload=b"<script>bad</script>"))[0] == 415
        payload = b"\xff\xd8\xfffixture\xff\xd9"
        results = await asyncio.gather(self.request(path, payload=payload), self.request(path, payload=payload))
        assert sorted(status for status, _ in results) == [200, 201]
        assert (await self.request(f"/{aid}/photos/2", payload=payload))[0] == 409
        self.storage.upload_bytes.side_effect = YandexDiskError("offline")
        assert (await self.request(f"/{aid}/photos/2", payload=payload[:-2]+b"2\xff\xd9"))[0] == 503
        assert sum(s["uploaded"] for s in (await self.request(f"/{aid}"))[1]["shots"]) == 1

    async def test_lost_upload_response_can_replay_the_fifth_frame_without_a_second_write(self):
        aid = await self.start()
        await self.upload_all(aid)
        before = self.storage.upload_bytes.await_count
        status, result = await self.request(f'/{aid}/photos/5', payload=b'\xff\xd8\xfffixture'+bytes([5])+b'\xff\xd9')
        assert status == 200 and result['status'] == 'PENDING_REVIEW'
        assert sum(s['uploaded'] for s in result['shots']) == 5
        assert self.storage.upload_bytes.await_count == before
        assert (await self.request(f'/{aid}/photos/5', payload=b'\xff\xd8\xffdifferent\xff\xd9'))[0] == 409

    async def test_academy_storage_probe_verifies_its_own_folder_and_cleans_only_its_file(self):
        assert await self.practice.verify_upload_storage(self.storage)
        path = self.storage.upload_bytes.await_args.args[0]
        assert path.startswith('app:/PhotoBoss/academy/connection-check-')
        self.storage.delete.assert_awaited_once_with(path)
        self.storage.download_bytes.side_effect = YandexDiskError('Yandex.Disk HTTP 503')
        assert not await self.practice.verify_upload_storage(self.storage)
        assert self.storage.delete.await_count == 2

    async def test_assignment_keeps_original_goal_and_revisits_concrete_previous_review(self):
        initial = {"category": "woman", "focus": "light", "title": "Мягкий свет",
                   "instructions": ["Снимите лицо у окна"], "successCriteria": ["В глазах видны блики"]}
        with self.engine.inner.begin() as conn:
            conn.execute(text("""INSERT INTO academy_coaching_plans(user_id,track,fingerprint,data,created_at)
                VALUES (3,'photographer','original',:data,'2020-01-01')"""), {"data": json.dumps(initial)})
        aid = await self.start()
        newer = initial | {"instructions": ["Снимите жёсткий контровой свет"], "successCriteria": ["Контур волос отделён"]}
        with self.engine.inner.begin() as conn:
            conn.execute(text("""INSERT INTO academy_coaching_plans(user_id,track,fingerprint,data,created_at)
                VALUES (3,'photographer','later',:data,'2020-01-02')"""), {"data": json.dumps(newer)})
        data = (await self.request(f"/{aid}"))[1]
        assert data["personalInstructions"] == initial["instructions"]
        assert data["successCriteria"] == initial["successCriteria"]
        await self.upload_all(aid)
        revision = {"status": "completed", "review": {"decision": "REVISION", "score": 75,
                    "reshoot_indexes": [2], "issues": ["Кадр 2: глаз в глубокой тени"],
                    "strengths": [], "next_action": "Повернитесь к окну"}}
        analyzer = AsyncMock(return_value=revision)
        with patch("app.academy_practice.config", SimpleNamespace(openai_api_key="fixture")), \
                patch("app.academy_practice.analyze_training_set", analyzer):
            await self.practice.process_pending(self.storage)
        context = analyzer.await_args.kwargs["assignment_context"]
        assert context["successCriteria"] == initial["successCriteria"]
        assert context["planFingerprint"] == "original"
        await self.request(f"/{aid}/photos/2", payload=b"\xff\xd8\xffreshoot\xff\xd9")
        analyzer.reset_mock()
        with patch("app.academy_practice.config", SimpleNamespace(openai_api_key="fixture")), \
                patch("app.academy_practice.analyze_training_set", analyzer):
            await self.practice.process_pending(self.storage)
        assert analyzer.await_args.kwargs["assignment_context"] == context
        assert analyzer.await_args.kwargs["previous_review"]["review"] == revision["review"]
        assert analyzer.await_args.kwargs["previous_review"]["sameAssignment"] is True

    async def test_legacy_assignment_never_adopts_a_plan_created_after_assignment(self):
        aid = await self.start()
        with self.engine.inner.begin() as conn:
            conn.execute(text("DELETE FROM operation_requests"))
            conn.execute(text("UPDATE training_assignments SET assigned_at='2020-01-01' WHERE id=:id"), {"id": aid})
            conn.execute(text("""INSERT INTO academy_coaching_plans(user_id,track,fingerprint,data,created_at)
                VALUES (3,'photographer','future',:data,'2021-01-01')"""), {"data": json.dumps({
                    "category": "woman", "instructions": ["Будущее задание"], "successCriteria": ["Не было при старте"]})})
        await self.upload_all(aid)
        analyzer = AsyncMock(return_value={"status": "unavailable"})
        with patch("app.academy_practice.config", SimpleNamespace(openai_api_key="fixture")), \
                patch("app.academy_practice.analyze_training_set", analyzer):
            await self.practice.process_pending(self.storage)
        assert analyzer.await_args.kwargs["assignment_context"]["planId"] is None
        assert analyzer.await_args.kwargs["assignment_context"]["successCriteria"] == []

    async def test_first_attempt_uses_same_category_history_without_claiming_reshoot(self):
        previous = {"status": "completed", "review": {"decision": "ACCEPT", "score": 86,
                    "issues": ["Кадр 1: фон отвлекает"], "next_action": "Упростите фон"}}
        with self.engine.inner.begin() as conn:
            conn.execute(text("""INSERT INTO training_assignments(id,user_id,category_slug,status,completed_at)
                VALUES (99,3,'woman','COMPLETED','2020-01-01')"""))
            conn.execute(text("""INSERT INTO academy_practice_reviews(assignment_id,result,photos,created_at)
                VALUES (99,:result,'[]','2020-01-01')"""), {"result": json.dumps(previous)})
            conn.execute(text("INSERT INTO academy_lesson_progress(user_id,topic_slug) VALUES(3,'next')"))
        aid = await self.start()
        await self.upload_all(aid)
        analyzer = AsyncMock(return_value={"status": "unavailable"})
        with patch("app.academy_practice.config", SimpleNamespace(openai_api_key="fixture")), \
                patch("app.academy_practice.analyze_training_set", analyzer):
            await self.practice.process_pending(self.storage)
        history = analyzer.await_args.kwargs["previous_review"]
        assert history["assignmentId"] == 99 and history["sameAssignment"] is False
        assert history["review"] == previous["review"]

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

        async def analyze(*args, **kwargs):
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

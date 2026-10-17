"""Authenticated Academy assignments, uploads and reviews inside the Mini App."""
import asyncio
import contextlib
import hashlib
import io
import json
import logging
import secrets
from datetime import UTC, datetime, timedelta

from aiogram.exceptions import TelegramAPIError
from aiohttp import web
from sqlalchemy import text

from .config import config
from .miniapp_security import AccessError
from .services.ai_diagnostics import unavailable as ai_unavailable
from .services.training import CATEGORY_BY_SLUG, TRAINING_CATEGORIES, training_day
from .services.training_ai import MAX_IMAGE_BYTES, analyze_training_set
from .work_chat import positive_id
from .yandex_disk import ROOT, YandexDiskError

logger = logging.getLogger(__name__)
DISK_PREFIX = "academy-disk:"
CHECKING_SOURCES = {"AI_PENDING", "AI_PROCESSING"}
REVIEW_LEASE_SECONDS = 15 * 60


def analysis_data(value):
    try:
        result = json.loads(value or "null")
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


class BoundedPhoto(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_IMAGE_BYTES:
            raise ValueError("Фото больше 8 МБ.")
        return super().write(data)


async def photo_bytes(bot, storage, file_id):
    if file_id.startswith(DISK_PREFIX):
        return await storage.download_bytes(file_id[len(DISK_PREFIX):], max_bytes=MAX_IMAGE_BYTES)
    buffer = BoundedPhoto()
    await bot.download(file_id, destination=buffer, timeout=25)
    return buffer.getvalue()


class AcademyPractice:
    def __init__(self, api):
        self.api, self.engine = api, api.engine
        self.lock = asyncio.Lock()

    async def assignment(self, conn, actor, aid, *, lock=False):
        rows = await self.api.rows(conn,
            "SELECT * FROM training_assignments WHERE id=:id" + (" FOR UPDATE" if lock else ""), id=aid)
        if not rows or (rows[0]["user_id"] != actor["id"] and not {"OWNER", "ADMIN"} & set(actor["roles"])):
            raise AccessError("Задание не найдено.", 404)
        return rows[0]

    async def detail_data(self, conn, actor, aid):
        row = await self.assignment(conn, actor, aid)
        category = CATEGORY_BY_SLUG[row["category_slug"]]
        shots = await self.api.rows(conn,
            "SELECT pose_index FROM training_submissions WHERE assignment_id=:id ORDER BY pose_index", id=aid)
        uploaded = {s["pose_index"] for s in shots}
        user = await self.api.rows(conn, "SELECT name FROM users WHERE id=:id", id=row["user_id"])
        try:
            analysis = json.loads(row["ai_analysis"] or "null")
        except ValueError:
            analysis = None
        plans = await self.api.rows(conn, "SELECT data FROM academy_coaching_plans WHERE user_id=:uid AND track='photographer' ORDER BY id DESC LIMIT 1", uid=row["user_id"])
        plan = json.loads(plans[0]["data"]) if plans else {}
        return {"id": aid, "personalInstructions": plan.get("instructions", []) if plan.get("category") == category.slug else [], "category": category.slug, "title": category.title,
                "userName": user[0]["name"], "own": row["user_id"] == actor["id"],
                "canReview": False,
                "canRetake": bool(row["user_id"] == actor["id"] and row["status"] == "PENDING_REVIEW"
                                  and row["review_source"] not in CHECKING_SOURCES and isinstance(analysis, dict)
                                  and analysis.get("review", {}).get("decision") == "MANUAL_REVIEW"),
                "canRetry": row["status"] == "PENDING_REVIEW" and row["review_source"] not in CHECKING_SOURCES,
                "status": row["status"], "reviewSource": row["review_source"],
                "score": row["ai_score"], "analysis": analysis,
                "shots": [{"index": i, "instruction": instruction, "uploaded": i in uploaded,
                    "reference": f"/academy/practice/references/{category.slug}/{i}",
                    "photo": f"/academy/practice/{aid}/photos/{i}" if i in uploaded else None}
                    for i, instruction in enumerate(category.shot_plan, 1)]}

    async def catalog(self, request):
        actor = request["miniapp_actor"]
        async with self.engine.connect() as conn:
            assignments = await self.api.rows(conn,
                "SELECT * FROM training_assignments WHERE user_id=:uid ORDER BY id DESC", uid=actor["id"])
            done = await self.api.rows(conn,
                "SELECT topic_slug FROM academy_lesson_progress WHERE user_id=:uid", uid=actor["id"])
            completed = {r["topic_slug"] for r in done}
            accepted = {r["category_slug"] for r in assignments if r["status"] == "COMPLETED"}
            block_number = self.api.academy_state(completed, accepted)
            block = next((b for b in self.api.blocks if b["number"] == block_number), None)
            lessons = {l["slug"] for l in self.api.lessons if l["block"] == block_number}
            ready = lessons.issubset(completed)
            active = next((r for r in assignments if r["status"] in {"ACTIVE", "PENDING_REVIEW"}), None)
            latest = assignments[0] if assignments else None
            today_done = bool(latest and latest["status"] == "COMPLETED" and latest["completed_at"]
                              and training_day(datetime.fromisoformat(str(latest["completed_at"]))) == training_day())
            course_done = all(l["slug"] in completed for l in self.api.lessons) and all(
                not b["practiceCategories"] or bool(set(b["practiceCategories"]) & accepted) for b in self.api.blocks)
            allowed = set(CATEGORY_BY_SLUG) if course_done else set(block["practiceCategories"]) if block else set(CATEGORY_BY_SLUG)
            if not allowed:
                allowed = set(CATEGORY_BY_SLUG)
            queue = []
            if {"OWNER", "ADMIN"} & set(actor["roles"]):
                queue = await self.api.rows(conn, """SELECT a.id,u.name,a.category_slug
                    FROM training_assignments a JOIN users u ON u.id=a.user_id
                    WHERE a.status='PENDING_REVIEW' ORDER BY a.id LIMIT 100""")
            result = {"assignment": await self.detail_data(conn, actor, active["id"]) if active else None,
                "ready": ready and not today_done, "todayDone": today_done,
                "categories": [{"slug": c.slug, "title": c.title} for c in TRAINING_CATEGORIES
                               if c.slug in allowed],
                "history": [{"id": r["id"], "title": CATEGORY_BY_SLUG[r["category_slug"]].title,
                             "status": r["status"], "reviewSource": r["review_source"],
                             "score": r["ai_score"], "analysis": analysis_data(r["ai_analysis"])}
                            for r in assignments[:30]],
                "queue": [{"id": r["id"], "name": r["name"],
                           "title": CATEGORY_BY_SLUG[r["category_slug"]].title} for r in queue]}
        return web.json_response(result)

    async def start(self, request):
        actor = request["miniapp_actor"]
        body = await self.api.body(request)
        if (set(body) != {"category"} or not isinstance(body["category"], str)
                or body["category"] not in CATEGORY_BY_SLUG):
            raise AccessError("Выберите категорию практики.", 400)
        async with self.lock, self.engine.begin() as conn:
            # Serialize with the existing bot workflow as well as other app tabs.
            await self.api.rows(conn, "SELECT id FROM users WHERE id=:id FOR UPDATE", id=actor["id"])
            if len(self.api.lessons) == 28:
                plans = await self.api.rows(conn, "SELECT data FROM academy_coaching_plans WHERE user_id=:uid AND track='photographer' ORDER BY id DESC LIMIT 1", uid=actor["id"])
                if not plans or json.loads(plans[0]["data"]).get("category") != body["category"]:
                    raise AccessError("Откройте AI-наставника: он назначит практику по вашим результатам.", 409)
            catalog = json.loads((await self.catalog(request)).text)
            if catalog["assignment"]:
                return web.json_response(catalog["assignment"])
            if not catalog["ready"] or body["category"] not in {c["slug"] for c in catalog["categories"]}:
                raise AccessError("Сначала завершите уроки текущего блока. После принятой практики новый набор откроется завтра.", 409)
            rows = await self.api.rows(conn, """INSERT INTO training_assignments
                (user_id,assigned_date,category_slug,status,assigned_at)
                VALUES (:uid,:day,:category,'ACTIVE',:now) RETURNING id""",
                uid=actor["id"], day=training_day(), category=body["category"], now=datetime.now(UTC).replace(tzinfo=None))
            aid = rows[0]["id"]
            await self.api.audit_write(conn, actor, "training_started", "training_assignment", aid, body["category"])
            result = await self.detail_data(conn, actor, aid)
        return web.json_response(result, status=201)

    async def detail(self, request):
        async with self.engine.connect() as conn:
            data = await self.detail_data(conn, request["miniapp_actor"], positive_id(request.match_info["id"]))
        return web.json_response(data)

    async def reference(self, request):
        category = CATEGORY_BY_SLUG.get(request.match_info["category"])
        pose = positive_id(request.match_info["pose"])
        if not category or pose not in range(1, 6):
            raise AccessError("Референс не найден.", 404)
        return web.FileResponse(category.image_paths[pose - 1], headers={"Content-Type": "image/jpeg"})

    async def photo(self, request):
        aid, pose = positive_id(request.match_info["id"]), positive_id(request.match_info["pose"])
        async with self.engine.connect() as conn:
            await self.assignment(conn, request["miniapp_actor"], aid)
            rows = await self.api.rows(conn, """SELECT submitted_file_id FROM training_submissions
                WHERE assignment_id=:id AND pose_index=:pose""", id=aid, pose=pose)
        if not rows:
            raise AccessError("Фото не найдено.", 404)
        payload = await photo_bytes(self.api.bot, request.app["yandex_disk"], rows[0]["submitted_file_id"])
        return web.Response(body=payload, content_type="image/jpeg")

    async def upload(self, request):
        actor = request["miniapp_actor"]
        aid, pose = positive_id(request.match_info["id"]), positive_id(request.match_info["pose"])
        if pose not in range(1, 6):
            raise AccessError("Некорректный номер кадра.", 400)
        async with self.engine.connect() as conn:
            row = await self.assignment(conn, actor, aid)
            if row["user_id"] != actor["id"] or row["status"] != "ACTIVE":
                raise AccessError("Это задание сейчас нельзя изменять.", 409)
        if not request.content_type.startswith("multipart/"):
            raise AccessError("Выберите фото.", 400)
        if request.content_length and request.content_length > MAX_IMAGE_BYTES + 8192:
            raise AccessError("Фото больше 8 МБ.", 413)
        reader = await request.multipart()
        part = await reader.next()
        if part is None or part.name != "file":
            raise AccessError("Выберите фото.", 400)
        payload = bytearray()
        while chunk := await part.read_chunk(64 * 1024):
            payload.extend(chunk)
            if len(payload) > MAX_IMAGE_BYTES:
                raise AccessError("Фото больше 8 МБ.", 413)
        if await reader.next() is not None or not payload.startswith(b"\xff\xd8\xff") or not payload.endswith(b"\xff\xd9"):
            raise AccessError("Нужно одно корректное JPEG-фото.", 415)
        digest = hashlib.sha256(payload).hexdigest()
        storage = request.app["yandex_disk"]
        folder = ROOT + "/academy"
        path = f"{folder}/{aid}-{pose}-{secrets.token_hex(8)}-{digest}.jpg"
        saved = False
        try:
            async with self.lock, self.engine.begin() as conn:
                row = await self.assignment(conn, actor, aid, lock=True)
                if row["user_id"] != actor["id"] or row["status"] != "ACTIVE":
                    raise AccessError("Задание уже отправлено на проверку.", 409)
                shots = await self.api.rows(conn, "SELECT * FROM training_submissions WHERE assignment_id=:id", id=aid)
                if any(s["pose_index"] == pose for s in shots):
                    raise AccessError("Кадр уже сохранён. Обновите задание.", 409)
                if any(s["submitted_file_id"].endswith(digest + ".jpg") for s in shots):
                    raise AccessError("Этот снимок уже загружен. Нужен другой кадр.", 409)
                await storage.ensure_dir(folder)
                await storage.upload_bytes(path, bytes(payload), content_type="image/jpeg")
                saved = True
                await conn.execute(text("""INSERT INTO training_submissions
                    (assignment_id,pose_index,reference_filename,submitted_file_id,submitted_at)
                    VALUES (:id,:pose,:reference,:file,:now)"""),
                    {"id": aid, "pose": pose, "reference": f"{pose:02d}.jpg",
                     "file": DISK_PREFIX + path, "now": datetime.now(UTC).replace(tzinfo=None)})
                if len(shots) == 4:
                    await conn.execute(text("""UPDATE training_assignments SET status='PENDING_REVIEW',
                        review_source='AI_PENDING' WHERE id=:id"""), {"id": aid})
                    await self.api.audit_write(conn, actor, "training_submitted", "training_assignment", aid, row["category_slug"])
                result = await self.detail_data(conn, actor, aid)
        except Exception:
            if saved:
                with contextlib.suppress(YandexDiskError):
                    await storage.delete(path)
            raise
        return web.json_response(result, status=201)

    async def review(self, request):
        raise AccessError("Практику проверяет AI-помощник. Ручное принятие отключено.", 403)

    async def retry(self, request):
        actor = request["miniapp_actor"]
        aid = positive_id(request.match_info["id"])
        if await self.api.body(request) != {}:
            raise AccessError("Некорректный запрос.", 400)
        async with self.lock, self.engine.begin() as conn:
            row = await self.assignment(conn, actor, aid, lock=True)
            if row["status"] != "PENDING_REVIEW":
                raise AccessError("Это задание не ожидает проверки.", 409)
            if row["review_source"] in CHECKING_SOURCES:
                return web.json_response({"ok": True})
            await conn.execute(text("UPDATE training_assignments SET review_source='AI_PENDING',ai_analysis=NULL WHERE id=:id"), {"id": aid})
        return web.json_response({"ok": True})

    async def retake(self, request):
        """Resolve an uncertain AI review without manual acceptance or lost history."""
        actor = request["miniapp_actor"]
        aid = positive_id(request.match_info["id"])
        body = await self.api.body(request)
        indexes = body.get("indexes")
        if (set(body) != {"indexes"} or not isinstance(indexes, list) or not 1 <= len(indexes) <= 5
                or any(type(i) is not int or not 1 <= i <= 5 for i in indexes)
                or len(set(indexes)) != len(indexes)):
            raise AccessError("Укажите разные номера кадров от 1 до 5.", 400)
        async with self.lock, self.engine.begin() as conn:
            row = await self.assignment(conn, actor, aid, lock=True)
            if row["user_id"] != actor["id"]:
                raise AccessError("Переснять практику может только её автор.", 403)
            try:
                analysis = json.loads(row["ai_analysis"] or "null")
            except ValueError:
                analysis = None
            if (row["status"] != "PENDING_REVIEW" or row["review_source"] in CHECKING_SOURCES
                    or not isinstance(analysis, dict)
                    or analysis.get("review", {}).get("decision") != "MANUAL_REVIEW"):
                raise AccessError("Пересъёмка доступна после неуверенного ответа ИИ. Обновите задание.", 409)
            # Immutable snapshots in academy_practice_reviews keep every old file.
            # Only unlink the selected current slots; never delete stored images.
            for index in indexes:
                await conn.execute(text("DELETE FROM training_submissions WHERE assignment_id=:id AND pose_index=:pose"),
                                   {"id": aid, "pose": index})
            await conn.execute(text("UPDATE training_assignments SET status='ACTIVE',review_source='AI_UNCERTAIN' WHERE id=:id"),
                               {"id": aid})
            await self.api.audit_write(conn, actor, "training_uncertain_retake", "training_assignment", aid,
                                       json.dumps({"indexes": indexes}))
            result = await self.detail_data(conn, actor, aid)
        return web.json_response(result)

    async def _reopen_unavailable_photos(self, row, shots, indexes, *, expected_sources):
        """Reopen only unreadable slots while retaining an immutable failure snapshot."""
        indexes = sorted(set(indexes))
        result = {"status": "storage_unavailable", "unavailable_indexes": indexes}
        async with self.lock, self.engine.begin() as conn:
            current = await self.api.rows(
                conn,
                "SELECT status,review_source FROM training_assignments WHERE id=:id FOR UPDATE",
                id=row["id"],
            )
            if (not current or current[0]["status"] != "PENDING_REVIEW"
                    or current[0]["review_source"] not in expected_sources):
                return False
            await conn.execute(text("""INSERT INTO academy_practice_reviews
                (assignment_id,result,photos,created_at) VALUES (:id,:result,:photos,:now)"""),
                {"id": row["id"], "result": json.dumps(result, ensure_ascii=False),
                 "photos": json.dumps([{"index": s["pose_index"], "file": s["submitted_file_id"]}
                                       for s in shots]),
                 "now": datetime.now(UTC).replace(tzinfo=None)})
            for index in indexes:
                await conn.execute(text(
                    "DELETE FROM training_submissions WHERE assignment_id=:id AND pose_index=:pose"
                ), {"id": row["id"], "pose": index})
            await conn.execute(text("""UPDATE training_assignments SET status='ACTIVE',
                review_source='STORAGE_RETRY',ai_score=NULL,ai_analysis=:analysis,completed_at=NULL
                WHERE id=:id"""),
                {"id": row["id"], "analysis": json.dumps(result, ensure_ascii=False)})
        logger.warning("Academy assignment %s reopened unreadable photos %s", row["id"], indexes)
        return True

    async def _load_submissions(self, row, shots, storage):
        by_index = {s["pose_index"]: s for s in shots}
        unavailable = [index for index in range(1, 6) if index not in by_index]
        uploaded = []
        for index in range(1, 6):
            shot = by_index.get(index)
            if shot is None:
                continue
            try:
                payload = await photo_bytes(self.api.bot, storage, shot["submitted_file_id"])
                if (not payload.startswith(b"\xff\xd8\xff") or not payload.endswith(b"\xff\xd9")
                        or len(payload) > MAX_IMAGE_BYTES):
                    raise ValueError("Stored practice photo is not a bounded JPEG")
                uploaded.append(payload)
            except (OSError, ValueError, TelegramAPIError, YandexDiskError):
                unavailable.append(index)
        return uploaded, unavailable

    async def recover_stalled_storage(self, storage):
        """Repair old AI_FAILED reviews after checking every stored practice photo."""
        async for row in self._pending_candidates(sources=("AI_FAILED",)):
            previous = analysis_data(row["ai_analysis"])
            # Credentials, quota, permissions and refusals need a deliberate
            # retry after remediation, not another paid request on every deploy.
            if (previous.get("retryable") is False
                    or (type(previous.get("attempts")) is int and previous["attempts"] >= 3)):
                continue
            async with self.engine.connect() as conn:
                shots = await self.api.rows(conn, """SELECT pose_index,submitted_file_id
                    FROM training_submissions WHERE assignment_id=:id ORDER BY pose_index""", id=row["id"])
            _, unavailable = await self._load_submissions(row, shots, storage)
            if unavailable:
                await self._reopen_unavailable_photos(
                    row, shots, unavailable, expected_sources={"AI_FAILED"}
                )
                continue
            async with self.lock, self.engine.begin() as conn:
                current = await self.api.rows(conn, """SELECT status,review_source
                    FROM training_assignments WHERE id=:id FOR UPDATE""", id=row["id"])
                if (not current or current[0]["status"] != "PENDING_REVIEW"
                        or current[0]["review_source"] != "AI_FAILED"):
                    continue
                await conn.execute(text("""UPDATE training_assignments
                    SET review_source='AI_PENDING' WHERE id=:id"""), {"id": row["id"]})
            logger.info("Academy assignment %s requeued after storage recovery", row["id"])

    async def _pending_candidates(self, assignment_id=None, *, sources=("AI_PENDING", "AI_PROCESSING")):
        """Keyset-scan bounded pages, so deferred leases cannot hide ready work.

        A snapshot ceiling keeps this pass finite even when new assignments are
        submitted continuously. Eligibility metadata stays in Python for the
        same behavior on PostgreSQL and the SQLite contract-test adapter.
        """
        source_params = {f"source{i}": source for i, source in enumerate(sources)}
        source_sql = ",".join(f":{key}" for key in source_params)
        async with self.engine.connect() as conn:
            ceiling_rows = await self.api.rows(conn, f"""SELECT MAX(id) AS ceiling
                FROM training_assignments WHERE status='PENDING_REVIEW'
                AND review_source IN ({source_sql})""", **source_params)
        ceiling = ceiling_rows[0]["ceiling"] if ceiling_rows else None
        if ceiling is None:
            return
        after = 0
        while after < ceiling:
            query = f"""SELECT * FROM training_assignments
                WHERE status='PENDING_REVIEW' AND review_source IN ({source_sql})
                AND id>:after AND id<=:ceiling
                """ + ("AND id=:id " if assignment_id is not None else "") + "ORDER BY id LIMIT 20"
            params = source_params | {"after": after, "ceiling": ceiling}
            if assignment_id is not None:
                params["id"] = assignment_id
            async with self.engine.connect() as conn:
                rows = await self.api.rows(conn, query, **params)
            if not rows:
                return
            for row in rows:
                yield row
            after = rows[-1]["id"]
            if assignment_id is not None or len(rows) < 20:
                return

    async def process_pending(self, storage, *, assignment_id=None):
        processed = 0
        outcomes = {}
        async for row in self._pending_candidates(assignment_id):
            if processed >= 2:
                break
            previous = analysis_data(row["ai_analysis"])
            now = datetime.now(UTC)
            due = previous.get("nextRetryAt")
            started = previous.get("processingStartedAt")
            try:
                if due and datetime.fromisoformat(due) > now:
                    continue
                if (row["review_source"] == "AI_PROCESSING" and started
                        and (now - datetime.fromisoformat(started)).total_seconds() < REVIEW_LEASE_SECONDS):
                    continue
            except (TypeError, ValueError):
                pass  # Legacy metadata must not permanently block a review.
            lease = secrets.token_hex(12)
            attempts = previous.get("attempts", 0)
            attempts = attempts if type(attempts) is int and 0 <= attempts <= 3 else 0
            queued = {"status": "processing", "attempts": attempts,
                      "processingStartedAt": now.isoformat(), "processingLease": lease}
            async with self.lock, self.engine.begin() as conn:
                current = await self.api.rows(conn, """SELECT status,review_source,ai_analysis
                    FROM training_assignments WHERE id=:id FOR UPDATE""", id=row["id"])
                if (not current or current[0]["status"] != "PENDING_REVIEW"
                        or current[0]["review_source"] != row["review_source"]
                        or current[0]["ai_analysis"] != row["ai_analysis"]):
                    continue
                await conn.execute(text("""UPDATE training_assignments SET review_source='AI_PROCESSING',
                    ai_analysis=:analysis WHERE id=:id"""),
                    {"id": row["id"], "analysis": json.dumps(queued)})
            processed += 1
            result = ai_unavailable('academy_review', config.academy_analysis_model, 'not_configured') if not config.openai_api_key else {"status": "unavailable"}
            async with self.engine.connect() as conn:
                shots = await self.api.rows(conn, """SELECT pose_index,submitted_file_id FROM training_submissions
                    WHERE assignment_id=:id ORDER BY pose_index""", id=row["id"])
            category = CATEGORY_BY_SLUG[row["category_slug"]]
            if config.openai_api_key:
                uploaded, unavailable = await self._load_submissions(row, shots, storage)
                if unavailable:
                    await self._reopen_unavailable_photos(
                        row, shots, unavailable, expected_sources={"AI_PROCESSING"}
                    )
                    outcomes[row["id"]] = {"status": "storage_unavailable", "unavailable_indexes": unavailable}
                    continue
                try:
                    references = [p.read_bytes() for p in category.image_paths]
                except OSError:
                    result = ai_unavailable('academy_review', config.academy_analysis_model, 'invalid_output')
                else:
                    result = await analyze_training_set(category, references, uploaded)
            review = result.get("review", {}) if result.get("status") == "completed" else {}
            decision = review.get("decision")
            reshoot = review.get("reshoot_indexes", [])
            if result.get("status") != "completed":
                result = result | {"attempts": attempts + 1}
                if result.get("retryable") and attempts + 1 < 3:
                    result["nextRetryAt"] = (datetime.now(UTC) + timedelta(seconds=(30, 90)[attempts])).isoformat()
            async with self.lock, self.engine.begin() as conn:
                current = await self.api.rows(conn, "SELECT status,review_source,ai_analysis FROM training_assignments WHERE id=:id FOR UPDATE", id=row["id"])
                if (not current or current[0]["status"] != "PENDING_REVIEW"
                        or current[0]["review_source"] != "AI_PROCESSING"
                        or analysis_data(current[0]["ai_analysis"]).get("processingLease") != lease):
                    continue
                status = "COMPLETED" if decision == "ACCEPT" else "ACTIVE" if decision == "REVISION" and reshoot else "PENDING_REVIEW"
                if review:
                    await conn.execute(text("""INSERT INTO academy_practice_reviews
                        (assignment_id,result,photos,created_at) VALUES (:id,:result,:photos,:now)"""),
                        {"id": row["id"], "result": json.dumps(result, ensure_ascii=False),
                         "photos": json.dumps([{"index": s["pose_index"], "file": s["submitted_file_id"]} for s in shots]),
                         "now": datetime.now(UTC).replace(tzinfo=None)})
                if status == "ACTIVE":
                    for index in reshoot:
                        await conn.execute(text("DELETE FROM training_submissions WHERE assignment_id=:id AND pose_index=:pose"),
                                           {"id": row["id"], "pose": index})
                await conn.execute(text("""UPDATE training_assignments SET status=:status,review_source=:source,
                    ai_score=:score,ai_analysis=:analysis,completed_at=:completed WHERE id=:id"""),
                    {"id": row["id"], "status": status, "source": ("AI_PENDING" if result.get("nextRetryAt") else "AI_UNCERTAIN" if decision == "MANUAL_REVIEW" else "AI_FAILED") if status == "PENDING_REVIEW" else "AI",
                     "score": review.get("score"), "analysis": json.dumps(result, ensure_ascii=False),
                     "completed": datetime.now(UTC).replace(tzinfo=None) if status == "COMPLETED" else None})
            logger.info("Academy assignment %s review finished decision=%s state=%s code=%s attempts=%s",
                        row["id"], decision or "unavailable", status, result.get("errorCode", "none"), attempts + 1)
            outcomes[row["id"]] = result
        return outcomes

    async def worker(self, app):
        try:
            await self.recover_stalled_storage(app["yandex_disk"])
        except Exception:
            logger.exception("Academy stalled-storage recovery unavailable")
        while True:
            try:
                await self.process_pending(app["yandex_disk"])
            except Exception:
                logger.exception("Academy review queue unavailable")
            await asyncio.sleep(10)


def install_academy_practice(app, miniapp):
    service = AcademyPractice(miniapp)
    app["academy_practice"] = service
    prefix = "/api/miniapp/academy/practice"
    app.router.add_get(prefix, service.catalog)
    app.router.add_post(prefix + "/start", service.start)
    app.router.add_get(prefix + "/references/{category}/{pose}", service.reference)
    app.router.add_get(prefix + "/{id}", service.detail)
    app.router.add_get(prefix + "/{id}/photos/{pose}", service.photo)
    app.router.add_post(prefix + "/{id}/photos/{pose}", service.upload)
    app.router.add_post(prefix + "/{id}/review", service.review)
    app.router.add_post(prefix + "/{id}/retry", service.retry)
    app.router.add_post(prefix + "/{id}/retake", service.retake)
    return service


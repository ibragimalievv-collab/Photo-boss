"""Authenticated Academy assignments, uploads and reviews inside the Mini App."""
import asyncio
import contextlib
import hashlib
import io
import json
import logging
import secrets
from datetime import UTC, datetime

from aiogram.exceptions import TelegramAPIError
from aiohttp import web
from sqlalchemy import text

from .config import config
from .miniapp_security import AccessError, require_owner
from .services.training import CATEGORY_BY_SLUG, TRAINING_CATEGORIES, training_day
from .services.training_ai import MAX_IMAGE_BYTES, analyze_training_set
from .work_chat import positive_id
from .yandex_disk import ROOT, YandexDiskError

logger = logging.getLogger(__name__)
DISK_PREFIX = "academy-disk:"


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
        if not rows or (rows[0]["user_id"] != actor["id"] and "OWNER" not in actor["roles"]):
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
        return {"id": aid, "category": category.slug, "title": category.title,
                "userName": user[0]["name"], "own": row["user_id"] == actor["id"],
                "canReview": "OWNER" in actor["roles"] and row["status"] == "PENDING_REVIEW",
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
            allowed = set(block["practiceCategories"]) if block else set(CATEGORY_BY_SLUG)
            if not allowed:
                allowed = set(CATEGORY_BY_SLUG)
            queue = []
            if "OWNER" in actor["roles"]:
                queue = await self.api.rows(conn, """SELECT a.id,u.name,a.category_slug
                    FROM training_assignments a JOIN users u ON u.id=a.user_id
                    WHERE a.status='PENDING_REVIEW' ORDER BY a.id LIMIT 100""")
            result = {"assignment": await self.detail_data(conn, actor, active["id"]) if active else None,
                "ready": ready and not today_done, "todayDone": today_done,
                "categories": [{"slug": c.slug, "title": c.title} for c in TRAINING_CATEGORIES
                               if c.slug in allowed and (not latest or c.slug != latest["category_slug"])],
                "history": [{"id": r["id"], "title": CATEGORY_BY_SLUG[r["category_slug"]].title,
                             "status": r["status"]} for r in assignments[:30]],
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
        actor = request["miniapp_actor"]
        require_owner(actor["roles"])
        aid = positive_id(request.match_info["id"])
        body = await self.api.body(request)
        if (set(body) != {"decision", "indexes", "comment"} or body["decision"] not in {"accept", "revision"}
                or not isinstance(body["indexes"], list) or len(body["indexes"]) > 5
                or any(type(i) is not int or i not in range(1, 6) for i in body["indexes"])
                or not isinstance(body["comment"], str) or len(body["comment"]) > 2000
                or (body["decision"] == "revision" and (not body["indexes"] or not body["comment"].strip()))):
            raise AccessError("Для пересъёмки выберите кадры и напишите, что исправить.", 400)
        async with self.lock, self.engine.begin() as conn:
            row = await self.assignment(conn, actor, aid, lock=True)
            if row["status"] != "PENDING_REVIEW":
                raise AccessError("Задание уже обработано.", 409)
            accepted = body["decision"] == "accept"
            if not accepted:
                for index in set(body["indexes"]):
                    await conn.execute(text("DELETE FROM training_submissions WHERE assignment_id=:id AND pose_index=:pose"),
                                       {"id": aid, "pose": index})
            await conn.execute(text("""UPDATE training_assignments SET status=:status,review_source='OWNER',
                completed_at=:completed,ai_analysis=:analysis WHERE id=:id"""),
                {"id": aid, "status": "COMPLETED" if accepted else "ACTIVE",
                 "completed": datetime.now(UTC).replace(tzinfo=None) if accepted else None,
                 "analysis": json.dumps({"status": "owner", **body}, ensure_ascii=False)})
            await self.api.audit_write(conn, actor, "training_approved" if accepted else "training_rejected",
                                       "training_assignment", aid, body["comment"])
            data = await self.detail_data(conn, actor, aid)
        return web.json_response(data)

    async def process_pending(self, storage):
        async with self.engine.connect() as conn:
            rows = await self.api.rows(conn, """SELECT * FROM training_assignments
                WHERE status='PENDING_REVIEW' AND review_source='AI_PENDING' ORDER BY id LIMIT 2""")
        for row in rows:
            result = {"status": "unavailable"}
            try:
                async with self.engine.connect() as conn:
                    shots = await self.api.rows(conn, """SELECT submitted_file_id FROM training_submissions
                        WHERE assignment_id=:id ORDER BY pose_index""", id=row["id"])
                category = CATEGORY_BY_SLUG[row["category_slug"]]
                if config.openai_api_key:
                    uploaded = [await photo_bytes(self.api.bot, storage, s["submitted_file_id"]) for s in shots]
                    result = await analyze_training_set(category, [p.read_bytes() for p in category.image_paths], uploaded)
            except (OSError, ValueError, TelegramAPIError, YandexDiskError):
                logger.warning("Academy analysis unavailable for assignment %s", row["id"])
            review = result.get("review", {}) if result.get("status") == "completed" else {}
            decision = review.get("decision")
            reshoot = review.get("reshoot_indexes", [])
            async with self.lock, self.engine.begin() as conn:
                current = await self.api.rows(conn, "SELECT status,review_source FROM training_assignments WHERE id=:id FOR UPDATE", id=row["id"])
                if not current or current[0]["status"] != "PENDING_REVIEW" or current[0]["review_source"] != "AI_PENDING":
                    continue
                status = "COMPLETED" if decision == "ACCEPT" else "ACTIVE" if decision == "REVISION" and reshoot else "PENDING_REVIEW"
                if status == "ACTIVE":
                    for index in reshoot:
                        await conn.execute(text("DELETE FROM training_submissions WHERE assignment_id=:id AND pose_index=:pose"),
                                           {"id": row["id"], "pose": index})
                await conn.execute(text("""UPDATE training_assignments SET status=:status,review_source=:source,
                    ai_score=:score,ai_analysis=:analysis,completed_at=:completed WHERE id=:id"""),
                    {"id": row["id"], "status": status, "source": "OWNER" if status == "PENDING_REVIEW" else "AI",
                     "score": review.get("score"), "analysis": json.dumps(result, ensure_ascii=False),
                     "completed": datetime.now(UTC).replace(tzinfo=None) if status == "COMPLETED" else None})

    async def worker(self, app):
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
    return service

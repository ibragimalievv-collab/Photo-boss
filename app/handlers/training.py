import io
import json
import logging

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile
from sqlalchemy import select

from ..academy_practice import DISK_PREFIX, AcademyPractice, photo_bytes
from ..access import StaffFilter
from ..db import Session, engine
from ..keyboards import inline
from ..models import (
    AcademyCoachingPlan,
    TrainingAssignment,
    TrainingSubmission,
    User,
)
from ..services.academy_growth import final_exam_unlocked
from ..services.core import ROLES, audit, get_user
from ..services.training import (
    CATEGORY_BY_SLUG,
    FINAL_EXAM_REQUIREMENTS,
    FINAL_EXAM_SLUG,
    category_rows,
    shot_instruction,
    training_day,
)
from ..services.training_ai import MAX_IMAGE_BYTES, review_text
from ..yandex_disk import YandexDisk, configured_from_env

r = Router()
r.message.filter(StaffFilter(*ROLES))
r.callback_query.filter(StaffFilter(*ROLES))
logger = logging.getLogger(__name__)


async def latest_assignment(session, user_id, *, unfinished_only=False, lock=False):
    query = select(TrainingAssignment).where(TrainingAssignment.user_id == user_id)
    if unfinished_only:
        query = query.where(TrainingAssignment.status.in_({"ACTIVE", "PENDING_REVIEW"}))
    query = query.order_by(
        TrainingAssignment.assigned_date.desc(), TrainingAssignment.id.desc()
    )
    if lock:
        query = query.with_for_update()
    return (await session.execute(query.limit(1))).scalars().first()


async def submission_indexes(session, assignment_id):
    return set(
        (
            await session.scalars(
                select(TrainingSubmission.pose_index).where(
                    TrainingSubmission.assignment_id == assignment_id
                )
            )
        ).all()
    )


def next_pose_index(indexes):
    return next((index for index in range(1, 6) if index not in indexes), None)


async def send_pose(message, assignment, pose_index):
    category = CATEGORY_BY_SLUG.get(assignment.category_slug)
    if category is None or pose_index not in range(1, 6):
        return await message.answer(
            "Эталон для задания не найден. Обратитесь к владельцу."
        )
    path = category.image_paths[pose_index - 1]
    if not path.is_file():
        return await message.answer(
            "Фотография временно недоступна. Обратитесь к владельцу."
        )
    await message.answer_photo(
        FSInputFile(path),
        caption=(
            f"{category.title}\n\n"
            f"Поза {pose_index}/5\n{shot_instruction(category, pose_index)}\n\n"
            "Повторите принцип кадра и пришлите свою фотографию сюда. "
            "Следующий кадр обязан отличаться ракурсом."
        ),
    )


async def send_reference_set(message, assignment):
    category = CATEGORY_BY_SLUG.get(assignment.category_slug)
    if category is None or any(not path.is_file() for path in category.image_paths):
        return await message.answer(
            "Фотографии временно недоступны. Обратитесь к владельцу."
        )
    is_exam = category.slug == FINAL_EXAM_SLUG
    if is_exam:
        await message.answer(
            "🎓 Выпускной экзамен\n\n" + "\n".join(FINAL_EXAM_REQUIREMENTS)
            + "\n\nРеференсы показывают отдельные принципы. Снимите собственную цельную серию; копировать людей и место не нужно."
        )
    for pose_index, path in enumerate(category.image_paths, start=1):
        await message.answer_photo(
            FSInputFile(path),
            caption=(f"{category.title}\n\nЭталон {pose_index}/5\n"
                     f"{shot_instruction(category, pose_index)} {pose_index}/5"),
        )
    await message.answer(
        "Референсы показаны. Пришлите пять своих экзаменационных кадров по порядку заданий. "
         "Проходной балл — 85; при спорной оценке потребуется повторная проверка или пересъёмка."
         if is_exam else
         "Все 5 эталонов показаны. Теперь пришлите сюда 5 своих повторов "
        "по порядку — от первого кадра к пятому. Одинаковый ракурс дважды "
        "не засчитывается: меняйте точку, высоту, план или действие."
    )


class TrainingBuffer(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_IMAGE_BYTES:
            raise ValueError("Слишком большое фото")
        return super().write(data)


async def download_training_photo(bot, file_id):
    if file_id.startswith(DISK_PREFIX):
        return await photo_bytes(bot, YandexDisk(*configured_from_env()), file_id)
    buffer = TrainingBuffer()
    await bot.download(file_id, destination=buffer, timeout=25)
    content = buffer.getvalue()
    if not content.startswith(b"\xff\xd8\xff"):
        raise ValueError("Ожидалось JPEG-фото")
    return content


async def ai_review_assignment(bot, assignment_id):
    """Use the same claimed, retryable queue as the web app, never a second AI path."""
    from ..miniapp_api import MiniApp

    service = AcademyPractice(MiniApp(engine, bot, []))
    outcomes = await service.process_pending(YandexDisk(*configured_from_env()), assignment_id=assignment_id)
    result = outcomes.get(assignment_id)
    # Another worker already owns the lease; do not overwrite it with AI_FAILED.
    if result is None:
        return True
    async with Session() as session:
        assignment = await session.get(TrainingAssignment, assignment_id)
        trainee = await session.get(User, assignment.user_id) if assignment else None
    if assignment is None or trainee is None:
        return True
    category = CATEGORY_BY_SLUG.get(assignment.category_slug)
    if category is None:
        return True
    if result.get("status") == "storage_unavailable":
        await bot.send_message(trainee.tg_id, "Нужно повторно загрузить кадры: "
                               + ", ".join(map(str, result["unavailable_indexes"]))
                               + ". Остальные фотографии сохранены. Откройте практику в приложении.")
        return True
    if result.get("status") != "completed":
        message = result.get("message") or "AI-проверка временно недоступна. Фото сохранены."
        message = message.replace("Ваш текст сохранён", "Фотографии сохранены")
        message += (" Повторная попытка запланирована автоматически." if result.get("nextRetryAt")
                    else " Подробности и повторная проверка доступны в Академии приложения.")
        await bot.send_message(trainee.tg_id, message)
        return True
    review = result["review"]
    await bot.send_message(trainee.tg_id, review_text(result))
    if review["decision"] == "ACCEPT":
        await bot.send_message(
            trainee.tg_id,
            "✅ ИИ принял практику по единому стандарту Photo Boss. Новый набор откроется завтра.",
        )
    elif review["decision"] == "REVISION":
        indexes = sorted(set(review["reshoot_indexes"]))
        await bot.send_message(
            trainee.tg_id,
            "🔁 Переснимите кадры: " + ", ".join(map(str, indexes)) + ". После загрузки ИИ проверит набор снова.",
        )
        if indexes:
            await bot.send_photo(
                trainee.tg_id,
                FSInputFile(category.image_paths[indexes[0] - 1]),
                caption=(f"Кадр {indexes[0]}/5\n"
                         f"{shot_instruction(category, indexes[0])}"),
            )
    else:
        await bot.send_message(trainee.tg_id, "ИИ не смог уверенно оценить кадры. "
                               "Откройте Академию приложения, чтобы повторить проверку или переснять спорные кадры.")
    return True


@r.message(F.text == "🎓 Обучение")
async def training_menu(message):
    async with Session() as session:
        user = await get_user(session, message.from_user.id)
        unfinished = await latest_assignment(session, user.id, unfinished_only=True)
        if unfinished is not None:
            indexes = await submission_indexes(session, unfinished.id)
            category = CATEGORY_BY_SLUG.get(unfinished.category_slug)
            if unfinished.status == "PENDING_REVIEW":
                return await message.answer(
                    "⏳ Ваши 5 фотографий отправлены AI-помощнику на проверку. "
                    "До решения другой набор выбрать нельзя."
                )
            await message.answer(
                ("⚠️ Просроченное обязательное задание\n\n"
                 if unfinished.assigned_date < training_day()
                 else "🎓 У вас уже есть обязательное задание\n\n")
                +
                f"Категория: {category.title if category else unfinished.category_slug}\n"
                f"Загружено: {len(indexes)}/5. Сменить категорию до завершения нельзя."
            )
            return await send_reference_set(message, unfinished)

        latest = await latest_assignment(session, user.id)
        today = training_day()
        if (
            latest is not None
            and latest.status == "COMPLETED"
            and latest.completed_at is not None
            and training_day(latest.completed_at) == today
        ):
            return await message.answer(
                "✅ Сегодняшнее обучение выполнено: 5/5. Новые позы откроются завтра."
            )
        excluded = latest.category_slug if latest is not None else None
        allowed = set(CATEGORY_BY_SLUG)
        if not await final_exam_unlocked(session, user.id):
            allowed.discard(FINAL_EXAM_SLUG)
        await message.answer(
            "🎓 Обучение\n\n"
            "Выберите одну категорию на сегодня. В ней 5 разных поз. После выбора "
            "категория закрепится за вами, пока вы не пришлёте все 5 повторов. "
            "На следующий день откроется другой набор.",
            reply_markup=inline(category_rows(excluded, allowed_slugs=allowed)),
        )


@r.callback_query(F.data.startswith("training:"))
async def training_reference(callback: CallbackQuery):
    slug = callback.data.partition(":")[2]
    category = CATEGORY_BY_SLUG.get(slug)
    if category is None:
        return await callback.answer("Категория не найдена.", show_alert=True)
    if callback.message is None or any(
        not path.is_file() for path in category.image_paths
    ):
        return await callback.answer("Фотографии временно недоступны.", show_alert=True)
    today = training_day()
    async with Session() as session:
        user = await get_user(session, callback.from_user.id)
        await session.get(User, user.id, with_for_update=True)
        if category.slug == FINAL_EXAM_SLUG and not await final_exam_unlocked(session, user.id):
            return await callback.answer(
                "Выпускной экзамен откроется после 28 уроков и принятых практик всех семи блоков.",
                show_alert=True,
            )
        plan = await session.scalar(select(AcademyCoachingPlan).where(AcademyCoachingPlan.user_id == user.id, AcademyCoachingPlan.track == "photographer").order_by(AcademyCoachingPlan.id.desc()).limit(1))
        if category.slug != FINAL_EXAM_SLUG and (
            not plan or json.loads(plan.data).get("category") != category.slug
        ):
            return await callback.answer("AI назначает категорию по вашим результатам. Откройте AI-наставника в Академии приложения.", show_alert=True)
        unfinished = await latest_assignment(
            session, user.id, unfinished_only=True, lock=True
        )
        if unfinished is not None:
            if unfinished.status == "PENDING_REVIEW":
                return await callback.answer(
                    "Задание уже отправлено AI-помощнику на проверку.", show_alert=True
                )
            await callback.answer(
                "Сначала завершите выбранную категорию.", show_alert=True
            )
            return await send_reference_set(callback.message, unfinished)
        latest = await latest_assignment(session, user.id)
        if (
            latest is not None
            and latest.status == "COMPLETED"
            and latest.completed_at is not None
            and training_day(latest.completed_at) == today
        ):
            return await callback.answer(
                "Сегодня 5/5 уже выполнено. Новый набор будет завтра.", show_alert=True
            )
        if latest is not None and latest.category_slug == category.slug:
            return await callback.answer(
                "Выберите другую категорию: вчерашний набор сегодня не повторяется.",
                show_alert=True,
            )
        assignment = TrainingAssignment(
            user_id=user.id,
            assigned_date=today,
            category_slug=category.slug,
        )
        session.add(assignment)
        await session.flush()
        await audit(
            session,
            user,
            "training_started",
            "training_assignment",
            assignment.id,
            category.slug,
        )
        await session.commit()
    await callback.message.answer(
        f"📁 Категория закреплена: {category.title}\n"
        "Нужно повторить все 5 кадров. Переключиться на другую категорию до завершения нельзя."
    )
    await send_reference_set(callback.message, assignment)
    await callback.answer()


@r.message(F.photo)
async def training_submission(message):
    async with Session() as session:
        user = await get_user(session, message.from_user.id)
        assignment = await latest_assignment(
            session, user.id, unfinished_only=True, lock=True
        )
        if assignment is None:
            return await message.answer(
                "Сначала откройте 🎓 Обучение и выберите категорию."
            )
        if assignment.status == "PENDING_REVIEW":
            return await message.answer(
                "⏳ Пять фотографий уже отправлены AI-помощнику на проверку."
            )
        indexes = await submission_indexes(session, assignment.id)
        pose_index = next_pose_index(indexes)
        if pose_index is None:
            assignment.status = "PENDING_REVIEW"
            await session.commit()
            return await message.answer("⏳ Задание ожидает AI-проверки.")
        category = CATEGORY_BY_SLUG.get(assignment.category_slug)
        if category is None:
            return await message.answer("Категория задания не найдена.")
        session.add(
            TrainingSubmission(
                assignment_id=assignment.id,
                pose_index=pose_index,
                reference_filename=category.image_paths[pose_index - 1].name,
                submitted_file_id=message.photo[-1].file_id,
            )
        )
        submitted_all = len(indexes) == 4
        if submitted_all:
            assignment.status = "PENDING_REVIEW"
            assignment.review_source = "AI_PENDING"
            await audit(
                session,
                user,
                "training_submitted",
                "training_assignment",
                assignment.id,
                category.slug,
            )
        await session.commit()
    if submitted_all:
        await message.answer(
            "📥 Все 5 повторов загружены. ИИ проверяет технику, свет, позу, "
            "эмоцию, композицию и разные ракурсы."
        )
        await ai_review_assignment(message.bot, assignment.id)
        return
    indexes.add(pose_index)
    await message.answer(
        f"✅ Повтор принят. Загружено: {len(indexes)}/5. Следующий кадр:"
    )
    await send_pose(message, assignment, next_pose_index(indexes))


def callback_id(data, prefix):
    try:
        value = int(data.removeprefix(prefix))
        if not 0 < value <= 2**31 - 1:
            raise ValueError
        return value
    except (AttributeError, TypeError, ValueError):
        return None


@r.callback_query(F.data.startswith("training_review:"))
async def training_review(callback: CallbackQuery, current_roles):
    if "OWNER" not in current_roles:
        return await callback.answer(
            "Проверять обучение может только владелец.", show_alert=True
        )
    assignment_id = callback_id(callback.data, "training_review:")
    if assignment_id is None or callback.message is None:
        return await callback.answer("Некорректная кнопка.", show_alert=True)
    async with Session() as session:
        assignment = await session.get(TrainingAssignment, assignment_id)
        if assignment is None:
            return await callback.answer("Задание не найдено.", show_alert=True)
        trainee = await session.get(User, assignment.user_id)
        submissions = (
            await session.scalars(
                select(TrainingSubmission)
                .where(TrainingSubmission.assignment_id == assignment.id)
                .order_by(TrainingSubmission.pose_index)
            )
        ).all()
    category = CATEGORY_BY_SLUG.get(assignment.category_slug)
    if category is None or len(submissions) != 5:
        return await callback.answer("Набор ещё не готов к проверке.", show_alert=True)
    await callback.answer()
    await callback.message.answer(
        f"👀 Проверка обучения\nСотрудник: {trainee.name}\nКатегория: {category.title}"
    )
    if assignment.ai_analysis:
        try:
            await callback.message.answer(review_text(json.loads(assignment.ai_analysis)))
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning("Invalid stored AI review for assignment %s", assignment.id)
    for submission in submissions:
        pose = submission.pose_index
        await callback.message.answer_photo(
            FSInputFile(category.image_paths[pose - 1]), caption=f"Эталон {pose}/5"
        )
        photo = submission.submitted_file_id
        if photo.startswith(DISK_PREFIX):
            photo = BufferedInputFile(await download_training_photo(callback.bot, photo), filename=f"practice-{pose}.jpg")
        await callback.message.answer_photo(photo, caption=f"Повтор сотрудника {pose}/5")
    await callback.message.answer("Оценку и пересъёмку назначает только AI-помощник. Повтор проверки доступен в Академии приложения.")


@r.callback_query(F.data.startswith("training_approve:"))
async def training_approve(callback: CallbackQuery, current_roles):
    await callback.answer("Практику принимает только AI-помощник. Ручная оценка отключена.", show_alert=True)


@r.callback_query(F.data.startswith("training_reject:"))
async def training_reject(callback: CallbackQuery, current_roles):
    await callback.answer("Пересъёмку назначает только AI-помощник. Откройте Академию приложения.", show_alert=True)

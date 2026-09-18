import json
import logging
from datetime import timedelta
from html import escape
from io import BytesIO

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery
from sqlalchemy import func, select

from ..access import StaffFilter
from ..db import Session
from ..keyboards import inline
from ..models import (
    AcademyAchievement,
    AcademyBestWork,
    AcademyError,
    AcademyLesson,
    AcademyLessonProgress,
    AcademyPracticeSubmission,
    AcademyPracticeTask,
    AcademyReview,
    Booking,
    Sale,
    User,
    UserRole,
)
from ..services.academy import CATEGORY_LABELS, analyze_photo, utc_now
from ..services.core import ROLES, get_user, roles_of

r = Router()
r.message.filter(StaffFilter(*ROLES))
r.callback_query.filter(StaffFilter(*ROLES))
logger = logging.getLogger(__name__)

LIBRARY_CATEGORIES = [
    ("couple", "💑 Пары"),
    ("family", "👨‍👩‍👧 Семьи"),
    ("children", "👶 Дети"),
    ("wedding", "💍 Свадьбы"),
    ("sea", "🌊 Море"),
    ("evening", "🌅 Вечерние кадры"),
]

MANUAL_ISSUES = {
    "light": (
        "Лицо в тени / свет требует правки",
        "Поверните лицо к главному источнику света на 20–40° или подвиньте человека ближе к свету.",
    ),
    "composition": (
        "Композиция требует правки",
        "Проверьте горизонт, края кадра и пустое пространство. Перед спуском быстро пройдитесь взглядом по четырём углам.",
    ),
    "pose": (
        "Слабая поза",
        "Дайте человеку простое действие вместо статичной позы: шаг, поворот, объятие или взаимодействие руками.",
    ),
    "emotion": (
        "Слабая эмоция",
        "Не просите просто улыбнуться. Дайте героям действие или повод посмотреть друг на друга и снимайте реакцию.",
    ),
}


class AcademyFlow(StatesGroup):
    upload = State()
    best_work = State()


def academy_menu(roles):
    rows = [
        [("👤 Мой профиль", "academy:profile"), ("🎓 Обучение", "academy:lessons")],
        [("📸 Практика", "academy:practice"), ("⚠️ Мои ошибки", "academy:errors")],
        [("🏆 Достижения", "academy:achievements"), ("🖼 Лучшие работы", "academy:library")],
        [("📈 Мой прогресс", "academy:progress"), ("🤖 Разобрать кадр", "academy:work")],
    ]
    if {"OWNER", "ADMIN"} & roles:
        rows += [
            [("📥 Проверка работ", "academy:review_queue")],
            [("➕ Добавить лучший кадр", "academy:add_best")],
        ]
    return inline(rows)


async def photographer_user(session, tg_id):
    user = await get_user(session, tg_id)
    roles = await roles_of(session, user)
    return user, roles


async def photo_bytes(bot, file_id):
    tg_file = await bot.get_file(file_id)
    buf = BytesIO()
    await bot.download_file(tg_file.file_path, destination=buf)
    return buf.getvalue()


async def save_analysis(session, review, data, reviewer_id=None):
    review.status = "DONE"
    review.composition_score = data["composition"]
    review.light_score = data["light"]
    review.emotion_score = data["emotion"]
    review.pose_score = data["pose"]
    review.overall_score = data["overall"]
    review.good_points_json = json.dumps(data.get("good", []), ensure_ascii=False)
    review.summary = data.get("summary", "")
    review.reviewed_by_user_id = reviewer_id
    review.reviewed_at = utc_now()
    await session.flush()
    for item in data.get("errors", []):
        session.add(
            AcademyError(
                photographer_id=review.photographer_id,
                review_id=review.id,
                category=item["category"],
                title=item["title"],
                recommendation=item["recommendation"],
            )
        )


def review_text(review, errors):
    good = []
    try:
        good = json.loads(review.good_points_json or "[]")
    except json.JSONDecodeError:
        pass
    lines = [
        "🤖 Разбор кадра",
        "",
        f"📐 Композиция: {review.composition_score or 0}/10",
        f"💡 Свет: {review.light_score or 0}/10",
        f"❤️ Эмоция: {review.emotion_score or 0}/10",
        f"🧍 Поза: {review.pose_score or 0}/10",
        f"⭐ Итог: {review.overall_score or 0}/10",
    ]
    if good:
        lines += ["", "✅ Хорошо:"] + [f"• {x}" for x in good[:4]]
    if errors:
        lines += ["", "⚠️ Исправить:"]
        for error in errors[:4]:
            lines.append(f"• {error.title}: {error.recommendation}")
    if review.summary:
        lines += ["", f"🎯 {review.summary}"]
    return "\n".join(lines)


async def notify_reviewers(bot, review_id, trainee_name):
    async with Session() as session:
        ids = (
            await session.scalars(
                select(User.tg_id)
                .join(UserRole, UserRole.user_id == User.id)
                .where(
                    User.active.is_(True),
                    UserRole.role.in_({"OWNER", "ADMIN"}),
                )
            )
        ).all()
    for tg_id in set(ids):
        try:
            await bot.send_message(
                tg_id,
                f"📥 Новый кадр Академии на проверку\nФотограф: {trainee_name}",
                reply_markup=inline(
                    [[("👀 Открыть", f"academy:manual:{review_id}")]]
                ),
            )
        except TelegramAPIError:
            logger.warning("Could not notify Academy reviewer %s", tg_id)


async def maybe_analyze(message, review_id, user_name):
    file_id = message.photo[-1].file_id
    try:
        data = await analyze_photo(await photo_bytes(message.bot, file_id))
    except Exception:
        logger.exception("Academy image analysis crashed")
        data = None
    if data is None:
        await notify_reviewers(message.bot, review_id, user_name)
        return None
    async with Session() as session:
        review = await session.get(AcademyReview, review_id)
        if review is None or review.status != "PENDING":
            return None
        await save_analysis(session, review, data)
        await session.commit()
        errors = (
            await session.scalars(
                select(AcademyError).where(AcademyError.review_id == review.id)
            )
        ).all()
    return review_text(review, errors)


@r.message(F.text == "📚 Академия фотографа")
async def open_academy(message, current_roles):
    await message.answer(
        "📚 Академия фотографа Photo Boss\n\n"
        "Обучение связано с реальной работой: качество → продажи → рост уровня.",
        reply_markup=academy_menu(current_roles),
    )


@r.callback_query(F.data == "academy:home")
async def academy_home(callback, current_roles):
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "📚 Академия фотографа Photo Boss",
            reply_markup=academy_menu(current_roles),
        )


@r.callback_query(F.data == "academy:profile")
async def profile(callback, current_roles):
    if "PHOTOGRAPHER" not in current_roles:
        return await callback.answer("Профиль роста доступен фотографам.", show_alert=True)
    async with Session() as session:
        user = await get_user(session, callback.from_user.id)
        shoots = await session.scalar(
            select(func.count(Booking.id)).where(Booking.photographer_id == user.id)
        ) or 0
        avg_sale = await session.scalar(
            select(func.avg(Sale.amount)).where(
                Sale.credited_user_id == user.id,
                Sale.commission_role == "PHOTOGRAPHER",
            )
        ) or 0
        errors_count = await session.scalar(
            select(func.count(AcademyError.id)).where(
                AcademyError.photographer_id == user.id,
                AcademyError.resolved.is_(False),
            )
        ) or 0
        quality = await session.scalar(
            select(func.avg(AcademyReview.overall_score)).where(
                AcademyReview.photographer_id == user.id,
                AcademyReview.status == "DONE",
            )
        ) or 0
        scores = (
            await session.execute(
                select(
                    func.avg(AcademyReview.composition_score),
                    func.avg(AcademyReview.light_score),
                    func.avg(AcademyReview.emotion_score),
                    func.avg(AcademyReview.pose_score),
                ).where(
                    AcademyReview.photographer_id == user.id,
                    AcademyReview.status == "DONE",
                )
            )
        ).one()
        lesson_count = await session.scalar(
            select(func.count(AcademyLessonProgress.id)).where(
                AcademyLessonProgress.user_id == user.id
            )
        ) or 0
        practice_count = await session.scalar(
            select(func.count(func.distinct(AcademyPracticeSubmission.task_id))).where(
                AcademyPracticeSubmission.photographer_id == user.id
            )
        ) or 0
    score_map = {
        "композиция": float(scores[0] or 0),
        "свет": float(scores[1] or 0),
        "эмоция": float(scores[2] or 0),
        "позирование": float(scores[3] or 0),
    }
    nonzero = {k: v for k, v in score_map.items() if v > 0}
    strong = max(nonzero, key=nonzero.get) if nonzero else "ещё собираем данные"
    weak = min(nonzero, key=nonzero.get) if nonzero else "ещё собираем данные"
    points = int(lesson_count) * 20 + int(practice_count) * 100
    if points >= 5000:
        level = "👑 Мастер Photo Boss"
    elif points >= 1500:
        level = "🥇 Профессионал"
    elif points >= 500:
        level = "🥈 Средний"
    else:
        level = "🥉 Новичок"
    await callback.answer()
    await callback.message.answer(
        f"👤 {user.name}\n\n"
        f"Уровень: {level}\n"
        f"Баллы: {points}\n"
        f"Съёмок: {shoots}\n"
        f"Средний чек: {float(avg_sale):.0f}\n"
        f"Рейтинг качества: {float(quality) * 10:.0f}%\n"
        f"Открытых ошибок: {errors_count}\n\n"
        f"💪 Сильная сторона: {strong}\n"
        f"🎯 Зона роста: {weak}",
        reply_markup=inline([[("⬅️ Академия", "academy:home")]]),
    )


@r.callback_query(F.data == "academy:lessons")
async def lessons(callback, current_roles):
    if "PHOTOGRAPHER" not in current_roles:
        return await callback.answer("Раздел доступен фотографам.", show_alert=True)
    async with Session() as session:
        rows = (
            await session.execute(
                select(AcademyLesson.category, func.count(AcademyLesson.id))
                .where(AcademyLesson.active.is_(True))
                .group_by(AcademyLesson.category)
            )
        ).all()
    buttons = [
        [(CATEGORY_LABELS.get(category, category), f"academy:lessoncat:{category}")]
        for category, _ in rows
    ]
    buttons.append([("⬅️ Академия", "academy:home")])
    await callback.answer()
    await callback.message.answer(
        "🎓 Короткие уроки по 2–5 минут. Выберите тему:",
        reply_markup=inline(buttons),
    )


@r.callback_query(F.data.startswith("academy:lessoncat:"))
async def lesson_category(callback):
    category = callback.data.split(":", 2)[2]
    async with Session() as session:
        lessons = (
            await session.scalars(
                select(AcademyLesson)
                .where(
                    AcademyLesson.category == category,
                    AcademyLesson.active.is_(True),
                )
                .order_by(AcademyLesson.sort_order, AcademyLesson.id)
            )
        ).all()
    if not lessons:
        return await callback.answer("Уроков пока нет.", show_alert=True)
    rows = [[(lesson.title, f"academy:lesson:{lesson.id}")] for lesson in lessons]
    rows.append([("⬅️ Темы", "academy:lessons")])
    await callback.answer()
    await callback.message.answer(
        CATEGORY_LABELS.get(category, category),
        reply_markup=inline(rows),
    )


@r.callback_query(F.data.startswith("academy:lesson:"))
async def lesson(callback):
    try:
        lesson_id = int(callback.data.split(":")[2])
    except (ValueError, IndexError):
        return await callback.answer("Некорректный урок.", show_alert=True)
    async with Session() as session:
        user = await get_user(session, callback.from_user.id)
        item = await session.get(AcademyLesson, lesson_id)
        if item is None or not item.active:
            return await callback.answer("Урок не найден.", show_alert=True)
        done = await session.scalar(
            select(AcademyLessonProgress.id).where(
                AcademyLessonProgress.user_id == user.id,
                AcademyLessonProgress.lesson_id == item.id,
            )
        )
        if done is None:
            session.add(AcademyLessonProgress(user_id=user.id, lesson_id=item.id))
            await session.commit()
    await callback.answer("Урок отмечен как пройден.")
    await callback.message.answer(
        f"🎓 {item.title}\n\n{item.body}\n\n✅ +20 баллов",
        reply_markup=inline(
            [[("⬅️ К урокам", f"academy:lessoncat:{item.category}")]]
        ),
    )


@r.callback_query(F.data == "academy:practice")
async def practice(callback, current_roles):
    if "PHOTOGRAPHER" not in current_roles:
        return await callback.answer("Раздел доступен фотографам.", show_alert=True)
    async with Session() as session:
        tasks = (
            await session.scalars(
                select(AcademyPracticeTask)
                .where(AcademyPracticeTask.active.is_(True))
                .order_by(AcademyPracticeTask.id)
            )
        ).all()
    rows = [[(task.title, f"academy:task:{task.id}")] for task in tasks]
    rows.append([("⬅️ Академия", "academy:home")])
    await callback.answer()
    await callback.message.answer(
        "📸 Практика\n\nВыберите задание. Для каждого нужно загрузить 5 кадров.",
        reply_markup=inline(rows),
    )


@r.callback_query(F.data.startswith("academy:task:"))
async def practice_task(callback, state, current_roles):
    if "PHOTOGRAPHER" not in current_roles:
        return await callback.answer("Раздел доступен фотографам.", show_alert=True)
    try:
        task_id = int(callback.data.split(":")[2])
    except (ValueError, IndexError):
        return await callback.answer("Некорректное задание.", show_alert=True)
    async with Session() as session:
        user = await get_user(session, callback.from_user.id)
        task = await session.get(AcademyPracticeTask, task_id)
        if task is None or not task.active:
            return await callback.answer("Задание не найдено.", show_alert=True)
        count = await session.scalar(
            select(func.count(AcademyPracticeSubmission.id)).where(
                AcademyPracticeSubmission.task_id == task.id,
                AcademyPracticeSubmission.photographer_id == user.id,
            )
        ) or 0
    if count >= 5:
        return await callback.answer("Это задание уже выполнено 5/5.", show_alert=True)
    await state.set_state(AcademyFlow.upload)
    await state.update_data(mode="practice", task_id=task.id)
    await callback.answer()
    await callback.message.answer(
        f"📸 {task.title}\n\n"
        f"{task.description}\n\n"
        f"✅ Сильный кадр: {task.strong_example_text}\n"
        f"⚠️ Типичная ошибка: {task.common_mistake_text}\n"
        f"🎯 Повторить: {task.repeat_tip}\n\n"
        f"Сейчас загружено: {count}/5. Пришлите следующий кадр.",
    )


@r.callback_query(F.data == "academy:work")
async def work_analysis(callback, state, current_roles):
    if "PHOTOGRAPHER" not in current_roles:
        return await callback.answer("Раздел доступен фотографам.", show_alert=True)
    await state.set_state(AcademyFlow.upload)
    await state.update_data(mode="work")
    await callback.answer()
    await callback.message.answer(
        "🤖 Пришлите один реальный рабочий кадр.\n"
        "Если AI подключён — разбор придёт сразу. Иначе кадр уйдёт владельцу на проверку."
    )


@r.message(AcademyFlow.upload, F.photo)
async def receive_academy_photo(message, state, current_roles):
    if "PHOTOGRAPHER" not in current_roles:
        await state.clear()
        return await message.answer("Раздел доступен фотографам.")
    data = await state.get_data()
    mode = data.get("mode")
    task_id = data.get("task_id")
    file_id = message.photo[-1].file_id
    async with Session() as session:
        user = await get_user(session, message.from_user.id)
        if mode == "practice":
            task = await session.get(AcademyPracticeTask, task_id)
            if task is None:
                await state.clear()
                return await message.answer("Задание больше недоступно.")
            count = await session.scalar(
                select(func.count(AcademyPracticeSubmission.id)).where(
                    AcademyPracticeSubmission.task_id == task.id,
                    AcademyPracticeSubmission.photographer_id == user.id,
                )
            ) or 0
            if count >= 5:
                await state.clear()
                return await message.answer("Задание уже выполнено 5/5.")
            pose_index = int(count) + 1
            review = AcademyReview(
                photographer_id=user.id,
                telegram_file_id=file_id,
                source_type="practice",
                source_id=task.id,
            )
            session.add(review)
            await session.flush()
            session.add(
                AcademyPracticeSubmission(
                    task_id=task.id,
                    photographer_id=user.id,
                    pose_index=pose_index,
                    telegram_file_id=file_id,
                    review_id=review.id,
                )
            )
        else:
            review = AcademyReview(
                photographer_id=user.id,
                telegram_file_id=file_id,
                source_type="work",
            )
            session.add(review)
            await session.flush()
            pose_index = None
        review_id = review.id
        user_name = user.name
        await session.commit()
    text = await maybe_analyze(message, review_id, user_name)
    if text:
        await message.answer(text)
    else:
        await message.answer(
            "📥 Кадр принят. AI-анализ сейчас недоступен, поэтому он отправлен владельцу на проверку."
        )
    if mode == "practice":
        if pose_index >= 5:
            await state.clear()
            await message.answer("🏆 Задание выполнено 5/5. +100 баллов.")
        else:
            await message.answer(f"✅ Загружено {pose_index}/5. Пришлите кадр {pose_index + 1}/5.")
    else:
        await state.clear()


@r.callback_query(F.data == "academy:errors")
async def errors(callback, current_roles):
    if "PHOTOGRAPHER" not in current_roles:
        return await callback.answer("Раздел доступен фотографам.", show_alert=True)
    async with Session() as session:
        user = await get_user(session, callback.from_user.id)
        items = (
            await session.scalars(
                select(AcademyError)
                .where(
                    AcademyError.photographer_id == user.id,
                    AcademyError.resolved.is_(False),
                )
                .order_by(AcademyError.created_at.desc())
                .limit(8)
            )
        ).all()
    await callback.answer()
    if not items:
        return await callback.message.answer(
            "⚠️ Открытых ошибок пока нет. Загрузите рабочий кадр для разбора.",
            reply_markup=inline([[("⬅️ Академия", "academy:home")]]),
        )
    lines = ["⚠️ Мои ошибки", ""]
    for item in items:
        lines.append(f"• {item.title}\n  → {item.recommendation}")
    await callback.message.answer(
        "\n\n".join(lines),
        reply_markup=inline([[("⬅️ Академия", "academy:home")]]),
    )


async def achievement_rows(session, user_id):
    lessons = await session.scalar(
        select(func.count(AcademyLessonProgress.id)).where(
            AcademyLessonProgress.user_id == user_id
        )
    ) or 0
    tasks = await session.scalar(
        select(func.count(func.distinct(AcademyPracticeSubmission.task_id))).where(
            AcademyPracticeSubmission.photographer_id == user_id
        )
    ) or 0
    emotion = await session.scalar(
        select(func.count(AcademyReview.id)).where(
            AcademyReview.photographer_id == user_id,
            AcademyReview.status == "DONE",
            AcademyReview.emotion_score >= 8,
        )
    ) or 0
    light = await session.scalar(
        select(func.count(AcademyReview.id)).where(
            AcademyReview.photographer_id == user_id,
            AcademyReview.status == "DONE",
            AcademyReview.light_score >= 8,
        )
    ) or 0
    catalog = [
        ("start", "🚀 Старт Академии", int(lessons), 1),
        ("practice3", "📸 Практик", int(tasks), 3),
        ("emotion100", "❤️ Мастер эмоций", int(emotion), 100),
        ("light500", "💡 Световой мастер", int(light), 500),
    ]
    existing = set(
        (
            await session.scalars(
                select(AcademyAchievement.code).where(
                    AcademyAchievement.photographer_id == user_id
                )
            )
        ).all()
    )
    changed = False
    for code, _, current, target in catalog:
        if current >= target and code not in existing:
            session.add(AcademyAchievement(photographer_id=user_id, code=code))
            existing.add(code)
            changed = True
    if changed:
        await session.commit()
    return [(title, current, target, code in existing) for code, title, current, target in catalog]


@r.callback_query(F.data == "academy:achievements")
async def achievements(callback, current_roles):
    if "PHOTOGRAPHER" not in current_roles:
        return await callback.answer("Раздел доступен фотографам.", show_alert=True)
    async with Session() as session:
        user = await get_user(session, callback.from_user.id)
        rows = await achievement_rows(session, user.id)
    lines = ["🏆 Мои достижения", ""]
    for title, current, target, unlocked in rows:
        lines.append(
            f"{'✅' if unlocked else '🔒'} {title}: {min(current, target)}/{target}"
        )
    await callback.answer()
    await callback.message.answer(
        "\n".join(lines),
        reply_markup=inline([[("⬅️ Академия", "academy:home")]]),
    )


@r.callback_query(F.data == "academy:library")
async def library(callback):
    rows = [[(label, f"academy:lib:{slug}")] for slug, label in LIBRARY_CATEGORIES]
    rows.append([("⬅️ Академия", "academy:home")])
    await callback.answer()
    await callback.message.answer(
        "🖼 Библиотека лучших работ Photo Boss\n\nВыберите категорию:",
        reply_markup=inline(rows),
    )


@r.callback_query(F.data.startswith("academy:lib:"))
async def library_category(callback):
    category = callback.data.split(":", 2)[2]
    async with Session() as session:
        items = (
            await session.scalars(
                select(AcademyBestWork)
                .where(AcademyBestWork.category == category)
                .order_by(AcademyBestWork.created_at.desc())
                .limit(10)
            )
        ).all()
    await callback.answer()
    if not items:
        return await callback.message.answer(
            "В этой категории пока нет кадров.",
            reply_markup=inline([[("⬅️ Библиотека", "academy:library")]]),
        )
    label = CATEGORY_LABELS.get(category, category)
    for index, item in enumerate(items, 1):
        await callback.message.answer_photo(
            item.telegram_file_id,
            caption=f"{label} · пример {index}" + (f"\n{item.note}" if item.note else ""),
        )


@r.callback_query(F.data == "academy:progress")
async def progress(callback, current_roles):
    if "PHOTOGRAPHER" not in current_roles:
        return await callback.answer("Раздел доступен фотографам.", show_alert=True)
    now = utc_now()
    current_start = now - timedelta(days=30)
    previous_start = now - timedelta(days=60)
    async with Session() as session:
        user = await get_user(session, callback.from_user.id)
        current_errors = await session.scalar(
            select(func.count(AcademyError.id)).where(
                AcademyError.photographer_id == user.id,
                AcademyError.created_at >= current_start,
            )
        ) or 0
        previous_errors = await session.scalar(
            select(func.count(AcademyError.id)).where(
                AcademyError.photographer_id == user.id,
                AcademyError.created_at >= previous_start,
                AcademyError.created_at < current_start,
            )
        ) or 0
        current_quality = await session.scalar(
            select(func.avg(AcademyReview.overall_score)).where(
                AcademyReview.photographer_id == user.id,
                AcademyReview.status == "DONE",
                AcademyReview.created_at >= current_start,
            )
        ) or 0
        previous_quality = await session.scalar(
            select(func.avg(AcademyReview.overall_score)).where(
                AcademyReview.photographer_id == user.id,
                AcademyReview.status == "DONE",
                AcademyReview.created_at >= previous_start,
                AcademyReview.created_at < current_start,
            )
        ) or 0
        current_sales = await session.scalar(
            select(func.coalesce(func.sum(Sale.amount), 0)).where(
                Sale.credited_user_id == user.id,
                Sale.created_at >= current_start,
            )
        ) or 0
        previous_sales = await session.scalar(
            select(func.coalesce(func.sum(Sale.amount), 0)).where(
                Sale.credited_user_id == user.id,
                Sale.created_at >= previous_start,
                Sale.created_at < current_start,
            )
        ) or 0
    sales_change = (
        ((float(current_sales) - float(previous_sales)) / float(previous_sales) * 100)
        if float(previous_sales) > 0
        else 0
    )
    await callback.answer()
    await callback.message.answer(
        "📈 Прогресс за 30 дней\n\n"
        f"Ошибки: {previous_errors} → {current_errors}\n"
        f"Качество: {float(previous_quality):.1f}/10 → {float(current_quality):.1f}/10\n"
        f"Продажи: {float(previous_sales):.0f} → {float(current_sales):.0f} "
        f"({sales_change:+.0f}%)\n\n"
        "Главная цель — меньше повторяющихся ошибок и стабильнее результат.",
        reply_markup=inline([[("⬅️ Академия", "academy:home")]]),
    )


@r.callback_query(F.data == "academy:review_queue")
async def review_queue(callback, current_roles):
    if not {"OWNER", "ADMIN"} & current_roles:
        return await callback.answer("Доступ только владельцу/администратору.", show_alert=True)
    async with Session() as session:
        rows = (
            await session.execute(
                select(AcademyReview, User.name)
                .join(User, User.id == AcademyReview.photographer_id)
                .where(AcademyReview.status == "PENDING")
                .order_by(AcademyReview.created_at)
                .limit(20)
            )
        ).all()
    buttons = [
        [(f"👀 {name} · #{review.id}", f"academy:manual:{review.id}")]
        for review, name in rows
    ]
    buttons.append([("⬅️ Академия", "academy:home")])
    await callback.answer()
    await callback.message.answer(
        f"📥 На проверке: {len(rows)}",
        reply_markup=inline(buttons),
    )


@r.callback_query(F.data.startswith("academy:manual:"))
async def manual_review(callback, current_roles):
    if not {"OWNER", "ADMIN"} & current_roles:
        return await callback.answer("Нет доступа.", show_alert=True)
    try:
        review_id = int(callback.data.split(":")[2])
    except (ValueError, IndexError):
        return await callback.answer("Некорректная кнопка.", show_alert=True)
    async with Session() as session:
        review = await session.get(AcademyReview, review_id)
        if review is None or review.status != "PENDING":
            return await callback.answer("Кадр уже обработан.", show_alert=True)
        trainee = await session.get(User, review.photographer_id)
        errors = (
            await session.scalars(
                select(AcademyError).where(AcademyError.review_id == review.id)
            )
        ).all()
    await callback.answer()
    caption = (
        f"👀 Проверка #{review.id}\nФотограф: {trainee.name}\n"
        f"Источник: {'Практика' if review.source_type == 'practice' else 'Рабочий кадр'}"
    )
    await callback.message.answer_photo(
        review.telegram_file_id,
        caption=caption,
        reply_markup=inline(
            [
                [("📐 Композиция", f"academy:issue:{review.id}:composition"), ("💡 Свет", f"academy:issue:{review.id}:light")],
                [("🧍 Поза", f"academy:issue:{review.id}:pose"), ("❤️ Эмоция", f"academy:issue:{review.id}:emotion")],
                [("✅ Завершить", f"academy:finish:{review.id}")],
            ]
        ),
    )
    if errors:
        await callback.message.answer(
            "Уже отмечено: " + ", ".join(error.title for error in errors)
        )


@r.callback_query(F.data.startswith("academy:issue:"))
async def manual_issue(callback, current_roles):
    if not {"OWNER", "ADMIN"} & current_roles:
        return await callback.answer("Нет доступа.", show_alert=True)
    try:
        _, _, raw_id, category = callback.data.split(":", 3)
        review_id = int(raw_id)
        title, recommendation = MANUAL_ISSUES[category]
    except (ValueError, KeyError):
        return await callback.answer("Некорректная кнопка.", show_alert=True)
    async with Session() as session:
        review = await session.get(AcademyReview, review_id)
        if review is None or review.status != "PENDING":
            return await callback.answer("Кадр уже обработан.", show_alert=True)
        exists = await session.scalar(
            select(AcademyError.id).where(
                AcademyError.review_id == review.id,
                AcademyError.category == category,
            )
        )
        if exists is None:
            session.add(
                AcademyError(
                    photographer_id=review.photographer_id,
                    review_id=review.id,
                    category=category,
                    title=title,
                    recommendation=recommendation,
                )
            )
            await session.commit()
    await callback.answer("Ошибка отмечена.")


@r.callback_query(F.data.startswith("academy:finish:"))
async def finish_manual(callback, current_roles):
    if not {"OWNER", "ADMIN"} & current_roles:
        return await callback.answer("Нет доступа.", show_alert=True)
    try:
        review_id = int(callback.data.split(":")[2])
    except (ValueError, IndexError):
        return await callback.answer("Некорректная кнопка.", show_alert=True)
    async with Session() as session:
        review = await session.get(AcademyReview, review_id, with_for_update=True)
        if review is None or review.status != "PENDING":
            return await callback.answer("Кадр уже обработан.", show_alert=True)
        errors = (
            await session.scalars(
                select(AcademyError).where(AcademyError.review_id == review.id)
            )
        ).all()
        bad = {error.category for error in errors}
        data = {
            "composition": 5 if "composition" in bad else 8,
            "light": 5 if "light" in bad else 8,
            "emotion": 5 if "emotion" in bad else 8,
            "pose": 5 if "pose" in bad else 8,
        }
        data["overall"] = round(sum(data.values()) / 4)
        data["good"] = ["Кадр проверен наставником Photo Boss"] if not bad else []
        data["errors"] = []
        data["summary"] = (
            "Сильный кадр. Продолжайте закреплять этот уровень."
            if not bad
            else "Сфокусируйтесь на отмеченных пунктах на следующей съёмке."
        )
        reviewer = await get_user(session, callback.from_user.id)
        review.status = "DONE"
        review.composition_score = data["composition"]
        review.light_score = data["light"]
        review.emotion_score = data["emotion"]
        review.pose_score = data["pose"]
        review.overall_score = data["overall"]
        review.good_points_json = json.dumps(data["good"], ensure_ascii=False)
        review.summary = data["summary"]
        review.reviewed_by_user_id = reviewer.id
        review.reviewed_at = utc_now()
        trainee = await session.get(User, review.photographer_id)
        await session.commit()
    await callback.answer("Проверка завершена.")
    try:
        await callback.bot.send_message(trainee.tg_id, review_text(review, errors))
    except TelegramAPIError:
        pass
    if callback.message:
        await callback.message.answer("✅ Кадр проверен и результат отправлен фотографу.")


@r.callback_query(F.data == "academy:add_best")
async def add_best(callback, state, current_roles):
    if not {"OWNER", "ADMIN"} & current_roles:
        return await callback.answer("Нет доступа.", show_alert=True)
    rows = [[(label, f"academy:addbestcat:{slug}")] for slug, label in LIBRARY_CATEGORIES]
    rows.append([("⬅️ Академия", "academy:home")])
    await callback.answer()
    await callback.message.answer(
        "➕ Выберите категорию лучшего кадра:",
        reply_markup=inline(rows),
    )


@r.callback_query(F.data.startswith("academy:addbestcat:"))
async def add_best_category(callback, state, current_roles):
    if not {"OWNER", "ADMIN"} & current_roles:
        return await callback.answer("Нет доступа.", show_alert=True)
    category = callback.data.split(":", 2)[2]
    if category not in {slug for slug, _ in LIBRARY_CATEGORIES}:
        return await callback.answer("Категория не найдена.", show_alert=True)
    await state.set_state(AcademyFlow.best_work)
    await state.update_data(category=category)
    await callback.answer()
    await callback.message.answer("Пришлите фотографию, которую нужно добавить в библиотеку.")


@r.message(AcademyFlow.best_work, F.photo)
async def save_best_work(message, state, current_roles):
    if not {"OWNER", "ADMIN"} & current_roles:
        await state.clear()
        return await message.answer("Нет доступа.")
    data = await state.get_data()
    category = data.get("category")
    async with Session() as session:
        user = await get_user(session, message.from_user.id)
        session.add(
            AcademyBestWork(
                category=category,
                telegram_file_id=message.photo[-1].file_id,
                added_by_user_id=user.id,
            )
        )
        await session.commit()
    await state.clear()
    await message.answer("✅ Кадр добавлен в библиотеку лучших работ.")

from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, FSInputFile
from sqlalchemy import case, func, select

from ..access import StaffFilter
from ..db import Session
from ..keyboards import inline
from ..models import (
    AcademyLessonProgress,
    AcademyReview,
    Booking,
    PayrollEntry,
    Photo,
    Sale,
    Shooting,
    TrainingAssignment,
    User,
)
from ..services.academy import (
    ACADEMY_LESSONS,
    BLOCK_BY_NUMBER,
    LESSON_BY_SLUG,
    REVIEW_TEMPLATES,
    block_lessons,
    block_practice_done,
    lesson_is_unlocked,
    level_for,
    next_level,
    strongest_and_weakest,
    unlocked_block,
)
from ..services.core import ROLES, audit, get_user
from ..services.training import CATEGORY_BY_SLUG, category_rows

r = Router()
r.message.filter(StaffFilter(*ROLES))
r.callback_query.filter(StaffFilter(*ROLES))


def academy_rows(roles):
    rows = [
        [("👤 Мой профиль", "academy:profile"), ("🎓 Обучение", "academy:theory")],
        [("📸 Практика", "academy:practice"), ("⚠️ Мои ошибки", "academy:errors")],
        [
            ("🏆 Мои достижения", "academy:achievements"),
            ("📈 Мой прогресс", "academy:progress"),
        ],
        [("🖼 Лучшие работы", "academy:library")],
    ]
    if {"OWNER", "ADMIN"} & set(roles):
        rows.append([("🧑‍🏫 Разбор реальных работ", "academy:review_queue")])
    return rows


async def academy_home(message, roles):
    await message.answer(
        "📚 АКАДЕМИЯ ФОТОГРАФА PHOTO BOSS\n\n"
        "Здесь обучение связано с реальной работой: теория → практика → "
        "разбор кадров → качество → продажи.",
        reply_markup=inline(academy_rows(roles)),
    )


async def review_averages(session, user_id, *, start=None, end=None):
    conditions = [AcademyReview.user_id == user_id]
    if start is not None:
        conditions.append(AcademyReview.created_at >= start)
    if end is not None:
        conditions.append(AcademyReview.created_at < end)
    row = (
        await session.execute(
            select(
                func.count(AcademyReview.id),
                func.avg(AcademyReview.quality_score),
                func.avg(AcademyReview.composition_score),
                func.avg(AcademyReview.light_score),
                func.avg(AcademyReview.pose_score),
                func.avg(AcademyReview.emotion_score),
                func.avg(AcademyReview.color_score),
                func.sum(case((AcademyReview.issues != "", 1), else_=0)),
            ).where(*conditions)
        )
    ).one()
    return {
        "count": int(row[0] or 0),
        "quality": float(row[1]) if row[1] is not None else None,
        "composition": float(row[2]) if row[2] is not None else None,
        "light": float(row[3]) if row[3] is not None else None,
        "pose": float(row[4]) if row[4] is not None else None,
        "emotion": float(row[5]) if row[5] is not None else None,
        "color": float(row[6]) if row[6] is not None else None,
        "errors": int(row[7] or 0),
    }


async def photographer_counts(session, user_id):
    shootings = int(
        await session.scalar(
            select(func.count(Booking.id)).where(
                Booking.photographer_id == user_id,
                Booking.status != "REJECTED",
            )
        )
        or 0
    )
    avg_check = float(
        await session.scalar(
            select(func.avg(Sale.amount)).where(Sale.credited_user_id == user_id)
        )
        or 0
    )
    lessons = int(
        await session.scalar(
            select(func.count(AcademyLessonProgress.id)).where(
                AcademyLessonProgress.user_id == user_id,
                AcademyLessonProgress.topic_slug.in_([lesson.slug for lesson in ACADEMY_LESSONS])
            )
        )
        or 0
    )
    practice = int(
        await session.scalar(
            select(func.count(TrainingAssignment.id)).where(
                TrainingAssignment.user_id == user_id,
                TrainingAssignment.status == "COMPLETED",
            )
        )
        or 0
    )
    strong_reviews = int(
        await session.scalar(
            select(func.count(AcademyReview.id)).where(
                AcademyReview.user_id == user_id,
                AcademyReview.quality_score >= 8,
            )
        )
        or 0
    )
    points = lessons * 10 + practice * 50 + strong_reviews * 20 + min(shootings, 500) * 2
    return shootings, avg_check, lessons, practice, strong_reviews, points


async def academy_program_state(session, user_id):
    completed = set((await session.scalars(
        select(AcademyLessonProgress.topic_slug).where(
            AcademyLessonProgress.user_id == user_id,
                AcademyLessonProgress.topic_slug.in_([lesson.slug for lesson in ACADEMY_LESSONS])
        )
    )).all())
    accepted = set((await session.scalars(
        select(TrainingAssignment.category_slug).where(
            TrainingAssignment.user_id == user_id,
            TrainingAssignment.status == "COMPLETED",
        )
    )).all())
    return completed, accepted, unlocked_block(completed, accepted)


@r.message(F.text == "📚 Академия")
async def academy_menu(message, current_roles):
    await academy_home(message, current_roles)


@r.callback_query(F.data == "academy:home")
async def academy_home_callback(callback: CallbackQuery, current_roles):
    await callback.answer()
    if callback.message:
        await academy_home(callback.message, current_roles)


@r.callback_query(F.data == "academy:profile")
async def academy_profile(callback: CallbackQuery):
    async with Session() as session:
        user = await get_user(session, callback.from_user.id)
        shootings, avg_check, lessons, practice, strong_reviews, points = (
            await photographer_counts(session, user.id)
        )
        metrics = await review_averages(session, user.id)
        now = datetime.now(UTC).replace(tzinfo=None)
        sales_30 = float(
            await session.scalar(
                select(func.coalesce(func.sum(Sale.amount), 0)).where(
                    Sale.credited_user_id == user.id,
                    Sale.created_at >= now - timedelta(days=30),
                )
            )
            or 0
        )
        payouts = float(
            await session.scalar(
                select(func.coalesce(func.sum(PayrollEntry.amount), 0)).where(
                    PayrollEntry.user_id == user.id
                )
            )
            or 0
        )
    skill_values = {
        key: metrics[key] for key in ("composition", "light", "pose", "emotion", "color")
    }
    strongest, weakest = strongest_and_weakest(skill_values)
    level = level_for(points)
    upcoming, left = next_level(points)
    quality = f"{metrics['quality'] * 10:.0f}%" if metrics["quality"] is not None else "—"
    next_text = (
        f"\nДо уровня «{upcoming}»: {left} баллов" if upcoming is not None else "\nМаксимальный уровень достигнут."
    )
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"👤 {user.name}\n\n"
            f"Уровень: {level}\n"
            f"Баллы: {points}{next_text}\n\n"
            f"📸 Съёмок: {shootings}\n"
            f"💰 Средний чек: {avg_check:.2f} ₽\n"
            f"⭐ Рейтинг качества: {quality}\n"
            f"⚠️ Ошибок в разборах: {metrics['errors']}\n\n"
            f"💪 Сильная сторона: {strongest}\n"
            f"🎯 Зона роста: {weakest}\n\n"
            f"📚 Уроков пройдено: {lessons}/{len(ACADEMY_LESSONS)}\n"
            f"📸 Практик принято: {practice}\n"
            f"✅ Сильных разборов: {strong_reviews}\n\n"
            f"💼 Связь с бизнесом\n"
            f"Продажи за 30 дней: {sales_30:.2f} ₽\n"
            f"Записи выплат/удержаний: {payouts:.2f} ₽",
            reply_markup=inline([[("⬅️ Академия", "academy:home")]]),
        )


@r.callback_query(F.data == "academy:theory")
async def academy_theory(callback: CallbackQuery):
    async with Session() as session:
        user = await get_user(session, callback.from_user.id)
        completed, accepted, available_block = await academy_program_state(
            session, user.id
        )
    rows = []
    for lesson in ACADEMY_LESSONS:
        if lesson.slug in completed:
            mark = "✅"
        elif lesson.block <= available_block:
            mark = "▫️"
        else:
            mark = "🔒"
        rows.append([(
            f"{mark} День {lesson.day}. {lesson.title}",
            f"academy:lesson:{lesson.slug}",
        )])
    rows.append([("⬅️ Академия", "academy:home")])
    current = BLOCK_BY_NUMBER[available_block]
    current_done = sum(
        lesson.slug in completed for lesson in block_lessons(available_block)
    )
    practice_line = (
        "Практика принята."
        if block_practice_done(available_block, accepted)
        else f"После 4 уроков: {current.practice_title}."
    )
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"🎓 Программа на 28 дней\n\n"
            f"Текущий блок {available_block}/7: {current.title}\n"
            f"В блоке: {current_done}/4 уроков. {practice_line}\n\n"
            f"Пройдено всего: {len(completed)}/{len(ACADEMY_LESSONS)}.\n"
            "Пропущенный урок не сгорает и остаётся текущим.",
            reply_markup=inline(rows),
        )


@r.callback_query(F.data.startswith("academy:lesson:"))
async def academy_lesson(callback: CallbackQuery):
    slug = callback.data.partition("academy:lesson:")[2]
    lesson = LESSON_BY_SLUG.get(slug)
    if lesson is None:
        return await callback.answer("Урок не найден.", show_alert=True)
    async with Session() as session:
        user = await get_user(session, callback.from_user.id)
        completed, accepted, _ = await academy_program_state(session, user.id)
    if not lesson_is_unlocked(lesson.slug, completed, accepted):
        return await callback.answer(
            "Сначала пройдите предыдущие четыре урока и сдайте практику.",
            show_alert=True,
        )
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"День {lesson.day} · Блок {lesson.block}\n{lesson.title}\n\n{lesson.body}",
            reply_markup=inline(
                [
                    [("✅ Урок изучен", f"academy:lesson_done:{slug}")],
                    [("⬅️ К урокам", "academy:theory")],
                ]
            ),
        )


@r.callback_query(F.data.startswith("academy:lesson_done:"))
async def academy_lesson_done(callback: CallbackQuery):
    slug = callback.data.partition("academy:lesson_done:")[2]
    lesson = LESSON_BY_SLUG.get(slug)
    if lesson is None:
        return await callback.answer("Урок не найден.", show_alert=True)
    created = False
    async with Session() as session:
        user = await get_user(session, callback.from_user.id)
        completed, accepted, _ = await academy_program_state(session, user.id)
        if not lesson_is_unlocked(slug, completed, accepted):
            return await callback.answer(
                "Предыдущий блок ещё не завершён.", show_alert=True
            )
        exists = await session.scalar(
            select(AcademyLessonProgress.id).where(
                AcademyLessonProgress.user_id == user.id,
                AcademyLessonProgress.topic_slug == slug,
            )
        )
        if exists is None:
            session.add(AcademyLessonProgress(user_id=user.id, topic_slug=slug))
            await audit(session, user, "academy_lesson_completed", "academy_lesson", details=slug)
            await session.commit()
            created = True
    await callback.answer("Урок засчитан. +10 баллов" if created else "Урок уже был засчитан.")
    if callback.message:
        await callback.message.answer(
            "✅ Урок отмечен как изученный." if created else "✅ Этот урок уже пройден.",
            reply_markup=inline([[("🎓 Следующий урок", "academy:theory")]]),
        )


@r.callback_query(F.data == "academy:practice")
async def academy_practice(callback: CallbackQuery):
    async with Session() as session:
        user = await get_user(session, callback.from_user.id)
        completed, _accepted, available_block = await academy_program_state(
            session, user.id
        )
    block = BLOCK_BY_NUMBER[available_block]
    lessons_done = all(
        lesson.slug in completed for lesson in block_lessons(available_block)
    )
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "📸 Практика\n\n"
            f"Текущий блок {available_block}: {block.title}\n"
            f"Обязательное задание: {block.practice_title}\n\n"
            + ("Сначала завершите четыре урока этого блока.\n\n" if not lessons_done else "")
            + "Бот покажет 5 эталонных кадров. Повторите принцип и загрузите "
            "свои 5 фотографий. ИИ проверит их по 100-балльному стандарту; "
            "проходной балл — 85.",
            reply_markup=inline([
                *(category_rows(allowed_slugs=block.practice_categories)
                  if lessons_done else []),
                [("⬅️ Академия", "academy:home")],
            ]),
        )


@r.callback_query(F.data == "academy:errors")
async def academy_errors(callback: CallbackQuery):
    async with Session() as session:
        user = await get_user(session, callback.from_user.id)
        rows = (
            await session.scalars(
                select(AcademyReview)
                .where(AcademyReview.user_id == user.id)
                .order_by(AcademyReview.created_at.desc())
                .limit(6)
            )
        ).all()
    await callback.answer()
    if not callback.message:
        return
    if not rows:
        return await callback.message.answer(
            "⚠️ Мои ошибки\n\nРазборов реальных работ пока нет. "
            "После проверки рабочих кадров здесь появятся конкретные рекомендации.",
            reply_markup=inline([[("⬅️ Академия", "academy:home")]]),
        )
    text = ["⚠️ Мои ошибки\n"]
    for item in rows:
        issue = item.issues or "Сильный кадр — критичных ошибок нет."
        text.append(
            f"• {item.created_at:%d.%m}: {issue}\n"
            f"  Оценка: {item.quality_score}/10\n"
            f"  → {item.recommendation}"
        )
    await callback.message.answer(
        "\n\n".join(text),
        reply_markup=inline([[("⬅️ Академия", "academy:home")]]),
    )


@r.callback_query(F.data == "academy:achievements")
async def academy_achievements(callback: CallbackQuery):
    async with Session() as session:
        user = await get_user(session, callback.from_user.id)
        shootings, _, _, practice, _, points = await photographer_counts(session, user.id)
        emotion = int(
            await session.scalar(
                select(func.count(AcademyReview.id)).where(
                    AcademyReview.user_id == user.id,
                    AcademyReview.emotion_score >= 9,
                )
            )
            or 0
        )
        light = int(
            await session.scalar(
                select(func.count(AcademyReview.id)).where(
                    AcademyReview.user_id == user.id,
                    AcademyReview.light_score >= 9,
                )
            )
            or 0
        )
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "🏆 Мои достижения\n\n"
            f"⭐ Баллы: {points}\n"
            f"Уровень: {level_for(points)}\n\n"
            f"{'🏅' if emotion >= 10 else '▫️'} Мастер эмоций — {min(emotion, 10)}/10 сильных эмоциональных разборов\n"
            f"{'🏅' if light >= 10 else '▫️'} Световой мастер — {min(light, 10)}/10 кадров с отличным светом\n"
            f"{'🏅' if practice >= 10 else '▫️'} Практик — {min(practice, 10)}/10 принятых заданий\n"
            f"{'🏅' if shootings >= 100 else '▫️'} 100 съёмок — {min(shootings, 100)}/100",
            reply_markup=inline([[("⬅️ Академия", "academy:home")]]),
        )


@r.callback_query(F.data == "academy:library")
async def academy_library(callback: CallbackQuery):
    preferred = ("family", "couple", "child", "family_portrait", "woman", "man", "adults")
    rows = [
        [(CATEGORY_BY_SLUG[slug].title, f"academy:library:{slug}")]
        for slug in preferred
        if slug in CATEGORY_BY_SLUG
    ]
    rows.append([("⬅️ Академия", "academy:home")])
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "🖼 Библиотека лучших работ\n\n"
            "Референсы Photo Boss: смотрите не только на позу, но и на свет, "
            "дистанцию между людьми и эмоцию.",
            reply_markup=inline(rows),
        )


@r.callback_query(F.data.startswith("academy:library:"))
async def academy_library_category(callback: CallbackQuery):
    slug = callback.data.partition("academy:library:")[2]
    category = CATEGORY_BY_SLUG.get(slug)
    if category is None:
        return await callback.answer("Категория не найдена.", show_alert=True)
    await callback.answer()
    if not callback.message:
        return
    available = [path for path in category.image_paths[:3] if path.is_file()]
    if not available:
        return await callback.message.answer("Референсы временно недоступны.")
    notes = (
        "Смотрите на три вещи: где источник света, как люди связаны внутри кадра "
        "и что создаёт живую эмоцию."
    )
    for index, path in enumerate(available, start=1):
        await callback.message.answer_photo(
            FSInputFile(path),
            caption=f"{category.title} · пример {index}\n\n{notes}",
        )
    await callback.message.answer(
        "Используйте референс как принцип, а не как шаблон позы.",
        reply_markup=inline([[("⬅️ К библиотеке", "academy:library")]]),
    )


async def sales_total(session, user_id, start, end):
    return float(
        await session.scalar(
            select(func.coalesce(func.sum(Sale.amount), 0)).where(
                Sale.credited_user_id == user_id,
                Sale.created_at >= start,
                Sale.created_at < end,
            )
        )
        or 0
    )


@r.callback_query(F.data == "academy:progress")
async def academy_progress(callback: CallbackQuery):
    now = datetime.now(UTC).replace(tzinfo=None)
    current_start = now - timedelta(days=30)
    previous_start = now - timedelta(days=60)
    async with Session() as session:
        user = await get_user(session, callback.from_user.id)
        current = await review_averages(session, user.id, start=current_start, end=now)
        previous = await review_averages(
            session, user.id, start=previous_start, end=current_start
        )
        current_sales = await sales_total(session, user.id, current_start, now)
        previous_sales = await sales_total(
            session, user.id, previous_start, current_start
        )
        practice_30 = int(
            await session.scalar(
                select(func.count(TrainingAssignment.id)).where(
                    TrainingAssignment.user_id == user.id,
                    TrainingAssignment.status == "COMPLETED",
                    TrainingAssignment.completed_at >= current_start,
                    TrainingAssignment.completed_at < now,
                )
            )
            or 0
        )
    skill_values = {
        key: current[key] for key in ("composition", "light", "pose", "emotion", "color")
    }
    _, weakest = strongest_and_weakest(skill_values)
    if previous_sales > 0:
        sales_change = (current_sales - previous_sales) / previous_sales * 100
        sales_line = f"{sales_change:+.0f}%"
    elif current_sales > 0:
        sales_line = "рост с 0"
    else:
        sales_line = "без изменений"
    current_quality = f"{current['quality'] * 10:.0f}%" if current["quality"] is not None else "—"
    previous_quality = f"{previous['quality'] * 10:.0f}%" if previous["quality"] is not None else "—"
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "📈 Прогресс за 30 дней\n\n"
            f"Разборов: {current['count']}\n"
            f"Ошибок: {previous['errors']} → {current['errors']}\n"
            f"Качество: {previous_quality} → {current_quality}\n"
            f"Продажи: {previous_sales:.2f} ₽ → {current_sales:.2f} ₽ ({sales_line})\n"
            f"Практик принято: {practice_30}\n\n"
            f"🎯 Главная зона развития: {weakest}",
            reply_markup=inline([[("⬅️ Академия", "academy:home")]]),
        )


def parse_positive_int(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if 0 < result <= 2**31 - 1 else None


@r.callback_query(F.data == "academy:review_queue")
async def academy_review_queue(callback: CallbackQuery, current_roles):
    if not {"OWNER", "ADMIN"} & set(current_roles):
        return await callback.answer("Раздел доступен владельцу и администратору.", show_alert=True)
    async with Session() as session:
        rows = (
            await session.execute(
                select(Photo.id, User.name, Photo.created_at)
                .join(Shooting, Shooting.id == Photo.shooting_id)
                .join(Booking, Booking.id == Shooting.booking_id)
                .join(User, User.id == Booking.photographer_id)
                .where(User.active.is_(True))
                .order_by(Photo.created_at.desc())
                .limit(10)
            )
        ).all()
    buttons = [
        [(f"📷 #{photo_id} · {name[:22]}", f"academy:review_photo:{photo_id}")]
        for photo_id, name, _ in rows
    ]
    buttons.append([("⬅️ Академия", "academy:home")])
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "🧑‍🏫 Разбор реальных работ\n\n"
            "Последние рабочие кадры. Откройте фотографию и выберите главный вывод для сотрудника.",
            reply_markup=inline(buttons),
        )


@r.callback_query(F.data.startswith("academy:review_photo:"))
async def academy_review_photo(callback: CallbackQuery, current_roles):
    if not {"OWNER", "ADMIN"} & set(current_roles):
        return await callback.answer("Недостаточно прав.", show_alert=True)
    photo_id = parse_positive_int(callback.data.partition("academy:review_photo:")[2])
    if photo_id is None:
        return await callback.answer("Некорректная фотография.", show_alert=True)
    async with Session() as session:
        row = (
            await session.execute(
                select(Photo, User)
                .join(Shooting, Shooting.id == Photo.shooting_id)
                .join(Booking, Booking.id == Shooting.booking_id)
                .join(User, User.id == Booking.photographer_id)
                .where(Photo.id == photo_id)
            )
        ).one_or_none()
    if row is None:
        return await callback.answer("Фотография не найдена.", show_alert=True)
    photo, photographer = row
    template_buttons = [
        [
            (REVIEW_TEMPLATES["strong"].title, f"academy:review_apply:{photo_id}:strong"),
            (REVIEW_TEMPLATES["light"].title, f"academy:review_apply:{photo_id}:light"),
        ],
        [
            (REVIEW_TEMPLATES["horizon"].title, f"academy:review_apply:{photo_id}:horizon"),
            (REVIEW_TEMPLATES["pose"].title, f"academy:review_apply:{photo_id}:pose"),
        ],
        [
            (REVIEW_TEMPLATES["emotion"].title, f"academy:review_apply:{photo_id}:emotion"),
            (REVIEW_TEMPLATES["angle"].title, f"academy:review_apply:{photo_id}:angle"),
        ],
        [(REVIEW_TEMPLATES["color"].title, f"academy:review_apply:{photo_id}:color")],
        [("⬅️ К очереди", "academy:review_queue")],
    ]
    await callback.answer()
    if callback.message:
        await callback.message.answer_photo(
            photo.file_id,
            caption=f"Рабочий кадр #{photo.id}\nФотограф: {photographer.name}\n\nВыберите главный вывод:",
            reply_markup=inline(template_buttons),
        )


@r.callback_query(F.data.startswith("academy:review_apply:"))
async def academy_review_apply(callback: CallbackQuery, current_roles):
    if not {"OWNER", "ADMIN"} & set(current_roles):
        return await callback.answer("Недостаточно прав.", show_alert=True)
    try:
        _, _, raw_photo_id, template_slug = callback.data.split(":", 3)
    except (AttributeError, ValueError):
        return await callback.answer("Некорректная кнопка.", show_alert=True)
    photo_id = parse_positive_int(raw_photo_id)
    template = REVIEW_TEMPLATES.get(template_slug)
    if photo_id is None or template is None:
        return await callback.answer("Некорректная оценка.", show_alert=True)
    async with Session() as session:
        row = (
            await session.execute(
                select(Photo, User)
                .join(Shooting, Shooting.id == Photo.shooting_id)
                .join(Booking, Booking.id == Shooting.booking_id)
                .join(User, User.id == Booking.photographer_id)
                .where(Photo.id == photo_id)
            )
        ).one_or_none()
        reviewer = await get_user(session, callback.from_user.id)
        if row is None or reviewer is None:
            return await callback.answer("Фотография не найдена.", show_alert=True)
        photo, photographer = row
        review = await session.scalar(
            select(AcademyReview).where(AcademyReview.photo_id == photo.id)
        )
        if review is None:
            review = AcademyReview(
                user_id=photographer.id,
                photo_id=photo.id,
                reviewer_user_id=reviewer.id,
                file_id=photo.file_id,
                composition_score=template.composition,
                light_score=template.light,
                pose_score=template.pose,
                emotion_score=template.emotion,
                color_score=template.color,
                quality_score=template.quality,
                strengths=template.strengths,
                issues=template.issues,
                recommendation=template.recommendation,
            )
            session.add(review)
            await session.flush()
        else:
            review.reviewer_user_id = reviewer.id
            review.composition_score = template.composition
            review.light_score = template.light
            review.pose_score = template.pose
            review.emotion_score = template.emotion
            review.color_score = template.color
            review.quality_score = template.quality
            review.strengths = template.strengths
            review.issues = template.issues
            review.recommendation = template.recommendation
            review.created_at = datetime.now(UTC).replace(tzinfo=None)
        await audit(
            session,
            reviewer,
            "academy_real_work_reviewed",
            "academy_review",
            review.id,
            f"photo={photo.id};template={template.slug}",
        )
        await session.commit()
        photographer_tg = photographer.tg_id
    notice = (
        f"📚 Новый разбор реальной работы\n\n"
        f"{template.title}\n"
        f"Оценка качества: {template.quality}/10\n\n"
        f"✅ Хорошо: {template.strengths}\n"
        f"{'⚠️ Исправить: ' + template.issues + chr(10) if template.issues else ''}"
        f"→ {template.recommendation}"
    )
    try:
        await callback.bot.send_message(photographer_tg, notice)
    except TelegramAPIError:
        pass
    await callback.answer("Разбор сохранён.")
    if callback.message:
        await callback.message.answer(
            "✅ Разбор сохранён и добавлен фотографу в «Мои ошибки».\n\n" + notice,
            reply_markup=inline(
                [
                    [("📷 Следующий кадр", "academy:review_queue")],
                    [("🏠 Академия", "academy:home")],
                ]
            ),
        )

import json
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery
from sqlalchemy import func, select

from ..access import StaffFilter
from ..db import Session
from ..keyboards import inline
from ..models import (
    AcademyAchievement,
    AcademyBestWork,
    AcademyLesson,
    AcademyPractice,
    AcademyProgress,
    AcademyReview,
    AcademyUserAchievement,
    Booking,
    Hotel,
    Photo,
    Shooting,
    User,
    UserRole,
)
from ..services.core import ROLES, audit, get_user, roles_of, has

r = Router()
r.message.filter(StaffFilter(*ROLES))
r.callback_query.filter(StaffFilter(*ROLES))

LESSONS = (
    ("posing-couples", "posing", "💑 Пары: живые эмоции", "Как поставить пару без зажатых поз.", "Не ставьте людей лицом в камеру как на паспорт. Дайте им идти, говорить и смотреть друг на друга.", "Начните с движения: три шага навстречу друг другу, затем остановка и взгляд.", "Начальный"),
    ("posing-family", "posing", "👨‍👩‍👧 Семья в кадре", "Собираем семью в живую, объёмную композицию.", "Не выстраивайте семью в одну линию.", "Создайте треугольник: взрослые ближе, дети между ними.", "Начальный"),
    ("posing-children", "posing", "🧒 Дети: эмоции без принуждения", "Как работать с детьми через игру.", "Не требуйте неподвижно смотреть в объектив.", "Дайте простое задание: найти ракушку, обнять родителя, рассмеяться.", "Начальный"),
    ("portrait", "posing", "📷 Индивидуальный портрет", "Поза, руки и линия плеч.", "Не оставляйте руки без действия.", "Попросите человека коснуться одежды, волос или предмета.", "Средний"),
    ("sun", "light", "☀️ Съёмка на солнце", "Управляем жёстким дневным светом.", "Не оставляйте жёсткую тень от носа на лице.", "Поверните клиента к открытому свету или найдите светлую отражающую поверхность.", "Начальный"),
    ("shade", "light", "🌤 Свет в тени", "Используем мягкий естественный свет.", "Не ставьте человека глубоко в тень с ярким фоном.", "Поставьте модель на край тени лицом к открытому небу.", "Начальный"),
    ("evening", "light", "🌅 Вечерний свет", "Снимаем тёплый закат и силуэт.", "Не допускайте тёмного лица без художественной задачи.", "Экспонируйте лицо, а контровой свет используйте как объём.", "Средний"),
    ("indoor", "light", "🏨 Свет в помещении", "Работа с окном и смешанным светом.", "Не смешивайте жёлтый потолочный свет и синий свет из окна.", "Выключите лишние источники или выровняйте баланс белого.", "Средний"),
    ("color", "editing", "🎨 Единый цвет Photo Boss", "Чистый и тёплый цвет без грязной кожи.", "Не уводите кожу в оранжевый или серый.", "Сначала выровняйте баланс белого, затем работайте с оттенками.", "Начальный"),
    ("skin", "editing", "✨ Натуральная кожа", "Ретушь без пластика.", "Не стирайте текстуру кожи полностью.", "Убирайте временные дефекты, сохраняя естественные детали.", "Средний"),
    ("trust", "client", "🗣 Как расслабить клиента", "Первые 60 секунд определяют весь результат.", "Не начинайте с команды «улыбнитесь».", "Назовите человека по имени и дайте простое действие вместо позы.", "Начальный"),
    ("emotion", "client", "😊 Как вызвать эмоцию", "Работа с искренней реакцией.", "Не просите играть эмоцию в пустоту.", "Задайте вопрос или игровое действие между людьми.", "Средний"),
    ("value", "sales", "💎 Показ ценности фотографий", "Как объяснить клиенту, за что он платит.", "Не начинайте разговор со скидки.", "Покажите лучший кадр и объясните, что именно делает его особенным.", "Начальный"),
    ("more-photos", "sales", "💰 Больше фотографий — честно", "Как предложить расширенный набор без давления.", "Не навязывайте пакет до просмотра результата.", "Сначала отберите любимые кадры клиента, потом предложите сохранить больше.", "Средний"),
)
CATEGORIES = {"posing": "📸 Позирование", "light": "💡 Свет", "editing": "🎨 Обработка", "client": "🗣 Клиент", "sales": "💰 Продажи"}
PRACTICE_TASKS = {
    "posing": "Пара в движении: общий кадр, эмоция, контакт и взаимодействие.",
    "light": "Портрет в естественном свете: ровное лицо, объём и чистый фон.",
    "editing": "Обработайте один кадр: чистая кожа, натуральный цвет, единый стиль.",
    "client": "Сделайте кадр, где клиент выглядит расслабленно и естественно.",
    "sales": "Выберите лучший кадр и подготовьте короткую историю его ценности для клиента.",
}


class AcademyPracticeFlow(StatesGroup):
    upload = State()


async def seed(session):
    existing = set((await session.scalars(select(AcademyLesson.slug))).all())
    for slug, category, title, description, bad, advice, level in LESSONS:
        if slug not in existing:
            session.add(AcademyLesson(slug=slug, category=category, title=title, description=description, bad_example=bad, good_example=advice, advice=advice, level=level))
    achievements = (("first-lesson", "📖 Первый шаг", "Пройти первый урок"),
                    ("practice-5", "📸 Практик", "Выполнить 5 практик"),
                    ("shoots-100", "🏆 Первые 100 съёмок", "Завершить 100 съёмок"),
                    ("portrait-master", "🏆 Мастер портрета", "Пройти уроки по позированию"),
                    ("sales-growth", "🏆 Рост продаж", "Пройти уроки по продажам"))
    existing_achievements = set((await session.scalars(select(AcademyAchievement.code))).all())
    for code, title, condition in achievements:
        if code not in existing_achievements:
            session.add(AcademyAchievement(code=code, title=title, condition=condition))
    await session.commit()


async def current_user(session, tg_id):
    user = await get_user(session, tg_id)
    await seed(session)
    return user


async def academy_stats(session, user_id):
    total = await session.scalar(select(func.count(AcademyLesson.id))) or 0
    completed = await session.scalar(select(func.count(AcademyProgress.id)).where(AcademyProgress.user_id == user_id, AcademyProgress.status == "COMPLETED")) or 0
    practices = await session.scalar(select(func.count(AcademyPractice.id)).where(AcademyPractice.user_id == user_id, AcademyPractice.status == "COMPLETED")) or 0
    quality = await session.scalar(select(func.avg(AcademyReview.score)).where(AcademyReview.photographer_id == user_id, AcademyReview.status == "REVIEWED"))
    return total, completed, practices, round(float(quality or 0))


def level(progress):
    return "⭐ Начальный" if progress < 35 else "⭐ Средний" if progress < 75 else "⭐ Продвинутый"


def back():
    return inline([[("🏠 Академия", "academy:home")]])


@r.message(F.text == "📚 Академия фотографа")
async def academy_home(message):
    async with Session() as session:
        user = await current_user(session, message.from_user.id)
        total, completed, practices, quality = await academy_stats(session, user.id)
    percent = round(completed / total * 100) if total else 0
    await message.answer(
        f"📚 Академия фотографа\n\nФотограф: {user.name}\n\nУровень: {level(percent)}\nПрогресс: {percent}%\nВсего уроков: {completed}/{total}\nПрактика: {practices} выполнено\nКачество работ: {quality or '—'}%\n\nВыберите раздел:",
        reply_markup=inline([
            [("📖 Обучение", "academy:learn"), ("📸 Практика", "academy:practice")],
            [("🔍 Разбор моих работ", "academy:reviews"), ("🏆 Достижения", "academy:achievements")],
            [("📊 Прогресс", "academy:progress"), ("🌟 Лучшие работы", "academy:best")],
        ]),
    )


@r.callback_query(F.data == "academy:home")
async def academy_home_callback(callback):
    await callback.answer()
    await academy_home(callback.message)


@r.callback_query(F.data == "academy:learn")
async def lessons_menu(callback):
    async with Session() as session:
        await current_user(session, callback.from_user.id)
        lessons = (await session.scalars(select(AcademyLesson).order_by(AcademyLesson.category, AcademyLesson.id))).all()
    rows = []
    for category, title in CATEGORIES.items():
        count = sum(lesson.category == category for lesson in lessons)
        rows.append([(f"{title} ({count})", f"academy:category:{category}")])
    await callback.answer()
    await callback.message.answer("📖 Обучение\n\nВыберите тему:", reply_markup=inline(rows + [[("🏠 Академия", "academy:home")]]))


@r.callback_query(F.data.startswith("academy:category:"))
async def category_menu(callback):
    category = callback.data.rsplit(":", 1)[1]
    if category not in CATEGORIES:
        return await callback.answer("Категория не найдена.", show_alert=True)
    async with Session() as session:
        user = await current_user(session, callback.from_user.id)
        lessons = (await session.scalars(select(AcademyLesson).where(AcademyLesson.category == category).order_by(AcademyLesson.id))).all()
        done = set((await session.scalars(select(AcademyProgress.lesson_id).where(AcademyProgress.user_id == user.id, AcademyProgress.status == "COMPLETED"))).all())
    rows = [[(f"{'✅ ' if lesson.id in done else ''}{lesson.title}", f"academy:lesson:{lesson.id}")] for lesson in lessons]
    await callback.answer()
    await callback.message.answer(CATEGORIES[category], reply_markup=inline(rows + [[("◀️ Темы", "academy:learn")], [("🏠 Академия", "academy:home")]]))


@r.callback_query(F.data.startswith("academy:lesson:"))
async def lesson_card(callback):
    try:
        lesson_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return await callback.answer("Некорректный урок.", show_alert=True)
    async with Session() as session:
        await current_user(session, callback.from_user.id)
        lesson = await session.get(AcademyLesson, lesson_id)
    if lesson is None:
        return await callback.answer("Урок не найден.", show_alert=True)
    await callback.answer()
    await callback.message.answer(
        f"📖 {lesson.title}\n\n{lesson.description}\n\n❌ Частая ошибка\n{lesson.bad_example}\n\n✅ Как правильно\n{lesson.good_example}\n\n💡 Совет\n{lesson.advice}",
        reply_markup=inline([[("🧠 Пройти тест", f"academy:quiz:{lesson.id}"), ("📸 Начать практику", f"academy:start-practice:{lesson.id}")], [("🏠 Академия", "academy:home")]]),
    )


@r.callback_query(F.data.startswith("academy:quiz:"))
async def quiz(callback):
    lesson_id = callback.data.rsplit(":", 1)[1]
    await callback.answer()
    await callback.message.answer("🧠 Мини-тест\n\nЧто важнее при работе с клиентом?", reply_markup=inline([[("Дать понятное действие", f"academy:quiz-answer:{lesson_id}:yes"), ("Попросить просто улыбаться", f"academy:quiz-answer:{lesson_id}:no")], [("🏠 Академия", "academy:home")]]))


@r.callback_query(F.data.startswith("academy:quiz-answer:"))
async def quiz_answer(callback):
    parts = callback.data.split(":")
    if len(parts) != 4:
        return await callback.answer("Некорректный ответ.", show_alert=True)
    try:
        lesson_id = int(parts[2])
    except ValueError:
        return await callback.answer("Некорректный урок.", show_alert=True)
    if parts[3] != "yes":
        return await callback.answer("Не совсем. Вернитесь к совету урока.", show_alert=True)
    async with Session() as session:
        user = await current_user(session, callback.from_user.id)
        progress = (await session.scalars(select(AcademyProgress).where(AcademyProgress.user_id == user.id, AcademyProgress.lesson_id == lesson_id))).one_or_none()
        if progress is None:
            session.add(AcademyProgress(user_id=user.id, lesson_id=lesson_id, status="COMPLETED", completed_at=datetime.now(UTC).replace(tzinfo=None)))
            await audit(session, user, "academy_lesson_completed", "academy_lesson", lesson_id)
            await session.commit()
    await callback.answer("Верно!")
    await callback.message.answer("✅ Урок засчитан. Теперь закрепите его на практике.", reply_markup=back())


@r.callback_query(F.data == "academy:practice")
async def practice_menu(callback):
    async with Session() as session:
        user = await current_user(session, callback.from_user.id)
        lessons = (await session.scalars(select(AcademyLesson).join(AcademyProgress, AcademyProgress.lesson_id == AcademyLesson.id).where(AcademyProgress.user_id == user.id, AcademyProgress.status == "COMPLETED").order_by(AcademyLesson.id.desc()))).all()
    rows = [[(f"Задание: {lesson.title}", f"academy:start-practice:{lesson.id}")] for lesson in lessons[:12]]
    if not rows:
        return await callback.answer("Сначала пройдите хотя бы один урок.", show_alert=True)
    await callback.answer()
    await callback.message.answer("📸 Практика\n\nВыберите освоенный урок:", reply_markup=inline(rows + [[("🏠 Академия", "academy:home")]]))


@r.callback_query(F.data.startswith("academy:start-practice:"))
async def start_practice(callback, state):
    try:
        lesson_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return await callback.answer("Некорректное задание.", show_alert=True)
    async with Session() as session:
        user = await current_user(session, callback.from_user.id)
        lesson = await session.get(AcademyLesson, lesson_id)
        if lesson is None:
            return await callback.answer("Урок не найден.", show_alert=True)
        practice = AcademyPractice(user_id=user.id, lesson_id=lesson.id, task_text=PRACTICE_TASKS[lesson.category], status="ACTIVE")
        session.add(practice)
        await session.commit()
    await state.set_state(AcademyPracticeFlow.upload)
    await state.set_data({"academy_practice_id": practice.id})
    await callback.answer()
    await callback.message.answer(f"📸 Практика\n\n{practice.task_text}\n\nПришлите один или несколько кадров. После первого кадра задание будет отправлено на проверку менеджеру.")


@r.message(StateFilter(AcademyPracticeFlow.upload), F.photo)
async def practice_upload(message, state):
    data = await state.get_data()
    practice_id = data.get("academy_practice_id")
    async with Session() as session:
        user = await current_user(session, message.from_user.id)
        practice = await session.get(AcademyPractice, practice_id, with_for_update=True)
        if practice is None or practice.user_id != user.id:
            await state.clear()
            return await message.answer("Задание не найдено. Откройте Академию заново.")
        practice.photo_file_id = message.photo[-1].file_id
        practice.status = "SUBMITTED"
        practice.submitted_at = datetime.now(UTC).replace(tzinfo=None)
        await audit(session, user, "academy_practice_submitted", "academy_practice", practice.id)
        await session.commit()
    await state.clear()
    await message.answer("✅ Практика принята и отправлена менеджеру на проверку.", reply_markup=back())


@r.message(StateFilter(AcademyPracticeFlow.upload))
async def practice_need_photo(message):
    await message.answer("Нужна фотография. Пришлите кадр сюда или нажмите «❌ Отменить».")


@r.callback_query(F.data == "academy:reviews")
async def reviews(callback):
    async with Session() as session:
        user = await current_user(session, callback.from_user.id)
        reviews = (await session.scalars(select(AcademyReview).where(AcademyReview.photographer_id == user.id).order_by(AcademyReview.created_at.desc()).limit(15))).all()
        shootings = (await session.scalars(select(Shooting).join(Booking, Booking.id == Shooting.booking_id).where(Booking.photographer_id == user.id, Shooting.status == "COMPLETED").order_by(Shooting.completed_at.desc()).limit(20))).all()
        reviewed = set(review.shooting_id for review in reviews if review.shooting_id)
    rows = [[(f"🎓 Отправить съёмку #{shooting.id}", f"academy:send-review:{shooting.id}")] for shooting in shootings if shooting.id not in reviewed]
    rows += [[(f"{'✅' if review.status == 'REVIEWED' else '⏳'} Разбор съёмки #{review.shooting_id}", f"academy:review-card:{review.id}")] for review in reviews]
    await callback.answer()
    await callback.message.answer("🔍 Разбор моих работ\n\nОтправьте завершённую съёмку в Академию или откройте готовый разбор.", reply_markup=inline(rows[:18] + [[("🏠 Академия", "academy:home")]]))


@r.callback_query(F.data.startswith("academy:send-review:"))
async def send_review(callback):
    try:
        shooting_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return await callback.answer("Некорректная съёмка.", show_alert=True)
    async with Session() as session:
        user = await current_user(session, callback.from_user.id)
        shooting = await session.get(Shooting, shooting_id)
        booking = await session.get(Booking, shooting.booking_id) if shooting else None
        if shooting is None or booking is None or booking.photographer_id != user.id or shooting.status != "COMPLETED":
            return await callback.answer("Эта съёмка недоступна.", show_alert=True)
        existing = (await session.scalars(select(AcademyReview).where(AcademyReview.shooting_id == shooting.id))).one_or_none()
        if existing:
            return await callback.answer("Эта работа уже в Академии.", show_alert=True)
        review = AcademyReview(photographer_id=user.id, shooting_id=shooting.id, status="PENDING")
        session.add(review)
        await audit(session, user, "academy_review_requested", "shooting", shooting.id)
        await session.commit()
    await callback.answer("Отправлено!")
    await callback.message.answer("🎓 Работа отправлена в Академию. Менеджер подготовит разбор: сильные стороны, ошибки и рекомендованный урок.", reply_markup=back())


@r.callback_query(F.data.startswith("academy:review-card:"))
async def review_card(callback):
    try:
        review_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return await callback.answer("Некорректный разбор.", show_alert=True)
    async with Session() as session:
        user = await current_user(session, callback.from_user.id)
        review = await session.get(AcademyReview, review_id)
        if review is None or review.photographer_id != user.id:
            return await callback.answer("Разбор не найден.", show_alert=True)
        recommendation = await session.get(AcademyLesson, review.recommended_lesson_id) if review.recommended_lesson_id else None
    if review.status != "REVIEWED":
        text = "⏳ Разбор ожидает менеджера."
    else:
        text = f"🔍 Разбор работы\n\nСильные стороны:\n{review.strengths or '—'}\n\nНужно улучшить:\n{review.issues or '—'}\n\nОценка качества: {review.score}%\n\nРекомендованный урок:\n{recommendation.title if recommendation else '—'}"
    await callback.answer()
    await callback.message.answer(text, reply_markup=back())


@r.callback_query(F.data == "academy:progress")
async def progress(callback):
    async with Session() as session:
        user = await current_user(session, callback.from_user.id)
        total, completed, practices, quality = await academy_stats(session, user.id)
        by_category = []
        for category, title in CATEGORIES.items():
            count = await session.scalar(select(func.count(AcademyProgress.id)).join(AcademyLesson, AcademyLesson.id == AcademyProgress.lesson_id).where(AcademyProgress.user_id == user.id, AcademyProgress.status == "COMPLETED", AcademyLesson.category == category)) or 0
            by_category.append(f"{title}: {count}")
    percent = round(completed / total * 100) if total else 0
    await callback.answer()
    await callback.message.answer(f"📊 Мой прогресс\n\n{level(percent)}\nУроки: {completed}/{total} ({percent}%)\nПрактика: {practices}\nКачество: {quality or '—'}%\n\n" + "\n".join(by_category), reply_markup=back())


@r.callback_query(F.data == "academy:achievements")
async def achievements(callback):
    async with Session() as session:
        user = await current_user(session, callback.from_user.id)
        _total, lessons, practices, _quality = await academy_stats(session, user.id)
        achievements = (await session.scalars(select(AcademyAchievement).order_by(AcademyAchievement.id))).all()
        shoots = await session.scalar(select(func.count(Shooting.id)).join(Booking, Booking.id == Shooting.booking_id).where(Booking.photographer_id == user.id, Shooting.status == "COMPLETED")) or 0
        unlocked = {"first-lesson": lessons >= 1, "practice-5": practices >= 5, "shoots-100": shoots >= 100, "portrait-master": lessons >= 4, "sales-growth": lessons >= 2}
        owned = set((await session.scalars(select(AcademyUserAchievement.achievement_id).where(AcademyUserAchievement.user_id == user.id))).all())
        for achievement in achievements:
            if unlocked.get(achievement.code) and achievement.id not in owned:
                session.add(AcademyUserAchievement(user_id=user.id, achievement_id=achievement.id))
        await session.commit()
        owned = set((await session.scalars(select(AcademyUserAchievement.achievement_id).where(AcademyUserAchievement.user_id == user.id))).all())
    lines = [f"{'🏆' if achievement.id in owned else '🔒'} {achievement.title}\n{achievement.condition}" for achievement in achievements]
    await callback.answer()
    await callback.message.answer("🏆 Достижения\n\n" + "\n\n".join(lines), reply_markup=back())


@r.callback_query(F.data == "academy:best")
async def best_works(callback):
    async with Session() as session:
        await current_user(session, callback.from_user.id)
        works = (await session.scalars(select(AcademyBestWork).where(AcademyBestWork.active.is_(True)).order_by(AcademyBestWork.category, AcademyBestWork.id).limit(20))).all()
    text = "🌟 Лучшие работы компании\n\nКатегории: семья • пара • дети • море • вечер • премиум."
    if works:
        text += "\n\n" + "\n".join(f"• {work.category}: {work.title}" for work in works)
    else:
        text += "\n\nЛучшие кадры появятся здесь после отбора менеджером."
    await callback.answer()
    await callback.message.answer(text, reply_markup=back())


@r.message(F.text == "👨‍💼 Контроль обучения")
async def manager_control(message, current_roles):
    if not ({"MANAGER", "ADMIN", "OWNER"} & current_roles):
        return await message.answer("Доступно менеджеру, администратору или владельцу.")
    async with Session() as session:
        await seed(session)
        photographers = (await session.scalars(select(User).join(UserRole, UserRole.user_id == User.id).where(User.active.is_(True), UserRole.role == "PHOTOGRAPHER").order_by(User.name))).all()
    await message.answer("👨‍💼 Контроль обучения\n\nВыберите фотографа:", reply_markup=inline([[(user.name, f"academy:manager-user:{user.id}")] for user in photographers] + [[("🏠 Академия", "academy:home")]]))


@r.callback_query(F.data.startswith("academy:manager-user:"))
async def manager_user(callback, current_roles):
    if not ({"MANAGER", "ADMIN", "OWNER"} & current_roles):
        return await callback.answer("Нет доступа.", show_alert=True)
    try:
        user_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return await callback.answer("Некорректный сотрудник.", show_alert=True)
    async with Session() as session:
        await seed(session)
        user = await session.get(User, user_id)
        total, completed, practices, quality = await academy_stats(session, user_id)
        reviews = (await session.scalars(select(AcademyReview).where(AcademyReview.photographer_id == user_id, AcademyReview.status == "PENDING").order_by(AcademyReview.created_at))).all()
    percent = round(completed / total * 100) if total else 0
    rows = [[(f"🔍 Разобрать съёмку #{review.shooting_id}", f"academy:manager-review:{review.id}")] for review in reviews]
    await callback.answer()
    await callback.message.answer(f"👨‍💼 {user.name}\n\nКачество: {quality or '—'}%\nПрогресс: {percent}%\nПрактика: {practices}\nОжидают разбор: {len(reviews)}", reply_markup=inline(rows + [[("🏠 Академия", "academy:home")]]))


@r.callback_query(F.data.startswith("academy:manager-review:"))
async def manager_review(callback, current_roles):
    if not ({"MANAGER", "ADMIN", "OWNER"} & current_roles):
        return await callback.answer("Нет доступа.", show_alert=True)
    try:
        review_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return await callback.answer("Некорректный разбор.", show_alert=True)
    async with Session() as session:
        review = await session.get(AcademyReview, review_id)
        shooting = await session.get(Shooting, review.shooting_id) if review else None
        photos = (await session.scalars(select(Photo).where(Photo.shooting_id == shooting.id).limit(10))).all() if shooting else []
    if review is None or shooting is None:
        return await callback.answer("Разбор не найден.", show_alert=True)
    await callback.answer()
    await callback.message.answer(f"🔍 Разбор съёмки #{shooting.id}\n\nОткройте фото ниже и выберите итог оценки.")
    for photo in photos:
        await callback.message.answer_photo(photo.file_id)
    await callback.message.answer("Оценка и главный фокус:", reply_markup=inline([
        [("90% — Отлично", f"academy:grade:{review.id}:90:excellent"), ("80% — Хорошо", f"academy:grade:{review.id}:80:light")],
        [("70% — Улучшить композицию", f"academy:grade:{review.id}:70:composition"), ("60% — Улучшить позы", f"academy:grade:{review.id}:60:posing")],
    ]))


@r.callback_query(F.data.startswith("academy:grade:"))
async def grade_review(callback, current_roles):
    if not ({"MANAGER", "ADMIN", "OWNER"} & current_roles):
        return await callback.answer("Нет доступа.", show_alert=True)
    try:
        _, _, raw_id, raw_score, issue = callback.data.split(":", 4)
        review_id, score = int(raw_id), int(raw_score)
    except ValueError:
        return await callback.answer("Некорректная оценка.", show_alert=True)
    async with Session() as session:
        reviewer = await current_user(session, callback.from_user.id)
        review = await session.get(AcademyReview, review_id, with_for_update=True)
        if review is None or review.status == "REVIEWED":
            return await callback.answer("Разбор уже готов.", show_alert=True)
        photographer = await session.get(User, review.photographer_id)
        lesson = (await session.scalars(select(AcademyLesson).where(AcademyLesson.category == ("light" if issue == "light" else "posing" if issue == "posing" else "editing")).order_by(AcademyLesson.id))).first()
        review.status = "REVIEWED"
        review.score = score
        review.strengths = "✅ Контакт с клиентом\n✅ Работа доведена до результата" if score >= 80 else "✅ Есть удачные кадры и потенциал"
        issue_text = {"excellent": "✅ Критичных ошибок не найдено", "light": "⚠ Свет лица: следите за направлением и мягкостью света", "composition": "⚠ Композиция: проверьте края кадра и баланс", "posing": "⚠ Позирование: дайте больше движения и естественного контакта"}[issue]
        review.issues = issue_text
        review.recommended_lesson_id = lesson.id if lesson else None
        review.reviewed_by_id = reviewer.id
        review.reviewed_at = datetime.now(UTC).replace(tzinfo=None)
        await audit(session, reviewer, "academy_review_completed", "academy_review", review.id, f"score={score};issue={issue}")
        await session.commit()
    await callback.bot.send_message(photographer.tg_id, f"🎓 Академия подготовила разбор вашей съёмки.\nОценка: {score}%\n\n{issue_text}\n\nОткройте «📚 Академия фотографа → Разбор моих работ».")
    await callback.answer("Разбор готов.")
    await callback.message.answer("✅ Разбор сохранён и отправлен фотографу.", reply_markup=back())

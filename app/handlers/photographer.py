from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ReplyKeyboardRemove
from sqlalchemy import func, select

from ..access import StaffFilter
from ..db import Session
from ..keyboards import inline, reply, request_location
from ..models import (
    Booking,
    PayrollEntry,
    Photo,
    PhotoStorage,
    Sale,
    ShiftCheckIn,
    ShiftCheckOut,
    Shooting,
)
from ..services.bookings import booking_card, notify_manager_ready_for_sale
from ..services.core import ROLES, audit, get_user, has, menu, roles_of
from ..services.photo_storage import (
    MAX_TELEGRAM_IMAGE_BYTES,
    ensure_photo_storage,
    storage_summary,
)
from ..services.sale_workflow import finalize_photographer_commissions
from ..services.shifts import LATE_FINE, is_late, shift_now

r = Router()
r.message.filter(StaffFilter("PHOTOGRAPHER"))
r.callback_query.filter(StaffFilter("PHOTOGRAPHER"))


class ShiftFlow(StatesGroup):
    location = State()
    photo = State()
    end_location = State()
    workplace_photo = State()


class PhotoUploadFlow(StatesGroup):
    uploading = State()


async def today_check_in(session, user_id, *, lock=False):
    query = select(ShiftCheckIn).where(
        ShiftCheckIn.user_id == user_id,
        ShiftCheckIn.shift_date == shift_now().date(),
    )
    if lock:
        query = query.with_for_update()
    return (await session.scalars(query)).one_or_none()


async def today_check_out(session, user_id, *, lock=False):
    query = select(ShiftCheckOut).where(
        ShiftCheckOut.user_id == user_id,
        ShiftCheckOut.shift_date == shift_now().date(),
    )
    if lock:
        query = query.with_for_update()
    return (await session.scalars(query)).one_or_none()


async def ask_for_location(message, state):
    await state.set_state(ShiftFlow.location)
    await message.answer(
        "📍 Для начала смены отправьте текущее местоположение кнопкой ниже.",
        reply_markup=request_location(),
    )


async def ask_for_full_body_photo(message, state):
    await state.set_state(ShiftFlow.photo)
    await message.answer(
        "📷 Сфотографируйтесь сейчас в полный рост. Нажмите значок камеры/скрепки "
        "в Telegram, выберите «Камера» и отправьте фотографию сюда.",
        reply_markup=ReplyKeyboardRemove(),
    )


@r.message(F.text == "🔄 Моя смена")
async def my_shift(m, state, current_roles):
    if "PHOTOGRAPHER" not in current_roles:
        return await m.answer("Смена доступна сотруднику с ролью фотографа.")
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        check_in = await today_check_in(s, u.id)
        check_out = await today_check_out(s, u.id)
    if check_in is None:
        await state.clear()
        return await m.answer(
            "🔄 Моя смена\n\nНачало рабочего дня — 09:00. "
            "После 09:00 автоматически начисляется штраф 500 ₽.",
            reply_markup=inline([[("▶️ Начать смену", "shift:begin")]]),
        )
    if check_in.status == "STARTED":
        await state.clear()
        started = shift_now(check_in.started_at)
        result = f"✅ Смена начата в {started:%H:%M}. Геолокация и фото сохранены."
        if check_in.late:
            result += f"\n⚠️ Опоздание: штраф {check_in.fine_amount:.0f} ₽."
        if check_out and check_out.status == "FINISHED":
            ended = shift_now(check_out.ended_at)
            return await m.answer(
                result + f"\n🏁 Смена завершена в {ended:%H:%M}.",
                reply_markup=reply(menu(current_roles)),
            )
        if check_out and check_out.status == "AWAITING_PHOTO":
            return await ask_for_workplace_photo(m, state)
        if check_out:
            return await ask_for_end_location(m, state)
        return await m.answer(
            result,
            reply_markup=inline([[("🏁 Закончить смену", "shift:end")]]),
        )
    if check_in.status == "AWAITING_PHOTO":
        return await ask_for_full_body_photo(m, state)
    return await ask_for_location(m, state)


@r.callback_query(F.data == "shift:begin")
async def begin_shift(c: CallbackQuery, state, current_roles):
    if c.message is None:
        return await c.answer("Некорректная кнопка.", show_alert=True)
    if "PHOTOGRAPHER" not in current_roles:
        return await c.answer("Нужна роль фотографа.", show_alert=True)
    async with Session() as s:
        u = await get_user(s, c.from_user.id)
        check_in = await today_check_in(s, u.id, lock=True)
        if check_in is None:
            check_in = ShiftCheckIn(
                user_id=u.id,
                shift_date=shift_now().date(),
            )
            s.add(check_in)
            await s.flush()
            await audit(s, u, "shift_check_in_started", "shift_check_in", check_in.id)
            await s.commit()
    await c.answer()
    if check_in.status == "STARTED":
        await state.clear()
        return await c.message.answer("✅ Сегодняшняя смена уже начата.")
    if check_in.status == "AWAITING_PHOTO":
        return await ask_for_full_body_photo(c.message, state)
    await ask_for_location(c.message, state)


@r.message(ShiftFlow.location, F.location)
async def save_shift_location(m, state):
    now = datetime.now(UTC).replace(tzinfo=None)
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        check_in = await today_check_in(s, u.id, lock=True)
        if check_in is None:
            await state.clear()
            return await m.answer("Начните заново через «🔄 Моя смена».")
        if check_in.status == "STARTED":
            await state.clear()
            return await m.answer("✅ Сегодняшняя смена уже начата.")
        check_in.latitude = m.location.latitude
        check_in.longitude = m.location.longitude
        check_in.location_received_at = now
        check_in.status = "AWAITING_PHOTO"
        await audit(s, u, "shift_location_received", "shift_check_in", check_in.id)
        await s.commit()
    await m.answer("✅ Геолокация сохранена.", reply_markup=ReplyKeyboardRemove())
    await ask_for_full_body_photo(m, state)


@r.message(ShiftFlow.location)
async def require_shift_location(m):
    await m.answer(
        "Нужно отправить геолокацию именно кнопкой «📍 Поделиться местоположением».",
        reply_markup=request_location(),
    )


@r.message(ShiftFlow.photo, F.photo)
async def save_shift_photo(m, state, current_roles):
    now_local = shift_now()
    now_utc = now_local.astimezone(UTC).replace(tzinfo=None)
    late = is_late(now_local)
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        check_in = await today_check_in(s, u.id, lock=True)
        if check_in is None:
            await state.clear()
            return await m.answer("Начните заново через «🔄 Моя смена».")
        if check_in.status == "STARTED":
            await state.clear()
            return await m.answer("✅ Сегодняшняя смена уже начата.")
        if check_in.status != "AWAITING_PHOTO" or check_in.latitude is None:
            return await ask_for_location(m, state)
        check_in.full_body_file_id = m.photo[-1].file_id
        check_in.started_at = now_utc
        check_in.status = "STARTED"
        check_in.late = late
        check_in.fine_amount = LATE_FINE if late else 0
        if late:
            s.add(
                PayrollEntry(
                    user_id=u.id,
                    kind="Штраф за опоздание",
                    amount=-LATE_FINE,
                    period=check_in.shift_date.isoformat(),
                    note=f"Начало смены после 09:00; shift_check_in={check_in.id}",
                )
            )
        await audit(
            s,
            u,
            "shift_started",
            "shift_check_in",
            check_in.id,
            f"late={late};fine={check_in.fine_amount:.0f}",
        )
        await s.commit()
    await state.clear()
    text = f"✅ Смена начата в {now_local:%H:%M}. Геолокация и фото сохранены."
    if late:
        text += f"\n⚠️ Опоздание после 09:00: начислен штраф {LATE_FINE:.0f} ₽."
    else:
        text += "\nВы отметились вовремя."
    await m.answer(text, reply_markup=reply(menu(current_roles)))


@r.message(ShiftFlow.photo)
async def require_shift_photo(m):
    await m.answer(
        "Нужна фотография в полный рост. Откройте камеру через значок камеры/скрепки "
        "и отправьте снимок сюда.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def ask_for_end_location(message, state):
    await state.set_state(ShiftFlow.end_location)
    await message.answer(
        "📍 Для завершения смены отправьте текущее местоположение кнопкой ниже.",
        reply_markup=request_location(),
    )


async def ask_for_workplace_photo(message, state):
    await state.set_state(ShiftFlow.workplace_photo)
    await message.answer(
        "📷 Сфотографируйте рабочее место перед уходом и отправьте фотографию сюда.",
        reply_markup=ReplyKeyboardRemove(),
    )


@r.callback_query(F.data == "shift:end")
async def end_shift(c: CallbackQuery, state, current_roles):
    if c.message is None:
        return await c.answer("Некорректная кнопка.", show_alert=True)
    async with Session() as s:
        u = await get_user(s, c.from_user.id)
        check_in = await today_check_in(s, u.id)
        if check_in is None or check_in.status != "STARTED":
            return await c.answer("Сначала начните смену.", show_alert=True)
        check_out = await today_check_out(s, u.id, lock=True)
        if check_out is None:
            check_out = ShiftCheckOut(user_id=u.id, shift_date=shift_now().date())
            s.add(check_out)
            await s.flush()
            await audit(s, u, "shift_check_out_started", "shift_check_out", check_out.id)
            await s.commit()
    await c.answer()
    if check_out.status == "FINISHED":
        await state.clear()
        return await c.message.answer("✅ Сегодняшняя смена уже завершена.")
    if check_out.status == "AWAITING_PHOTO":
        return await ask_for_workplace_photo(c.message, state)
    await ask_for_end_location(c.message, state)


@r.message(ShiftFlow.end_location, F.location)
async def save_end_location(m, state):
    now = datetime.now(UTC).replace(tzinfo=None)
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        check_out = await today_check_out(s, u.id, lock=True)
        if check_out is None:
            await state.clear()
            return await m.answer("Начните завершение заново через «🔄 Моя смена».")
        check_out.latitude = m.location.latitude
        check_out.longitude = m.location.longitude
        check_out.location_received_at = now
        check_out.status = "AWAITING_PHOTO"
        await audit(s, u, "shift_end_location_received", "shift_check_out", check_out.id)
        await s.commit()
    await m.answer("✅ Геолокация сохранена.", reply_markup=ReplyKeyboardRemove())
    await ask_for_workplace_photo(m, state)


@r.message(ShiftFlow.end_location)
async def require_end_location(m):
    await m.answer(
        "Нужно отправить геолокацию кнопкой «📍 Поделиться местоположением».",
        reply_markup=request_location(),
    )


@r.message(ShiftFlow.workplace_photo, F.photo)
async def save_workplace_photo(m, state, current_roles):
    now_local = shift_now()
    now_utc = now_local.astimezone(UTC).replace(tzinfo=None)
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        check_out = await today_check_out(s, u.id, lock=True)
        if check_out is None:
            await state.clear()
            return await m.answer("Начните завершение заново через «🔄 Моя смена».")
        if check_out.status == "FINISHED":
            await state.clear()
            return await m.answer("✅ Сегодняшняя смена уже завершена.")
        if check_out.status != "AWAITING_PHOTO" or check_out.latitude is None:
            return await ask_for_end_location(m, state)
        check_out.workplace_file_id = m.photo[-1].file_id
        check_out.ended_at = now_utc
        check_out.status = "FINISHED"
        await audit(s, u, "shift_finished", "shift_check_out", check_out.id)
        await s.commit()
    await state.clear()
    await m.answer(
        f"🏁 Смена завершена в {now_local:%H:%M}. Геолокация и фото рабочего места сохранены.",
        reply_markup=reply(menu(current_roles)),
    )


@r.message(ShiftFlow.workplace_photo)
async def require_workplace_photo(m):
    await m.answer(
        "Нужна фотография рабочего места. Откройте камеру и отправьте снимок сюда.",
        reply_markup=ReplyKeyboardRemove(),
    )


@r.message(F.text == "📸 Мои съёмки")
async def shoots(m):
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        rs = await roles_of(s, u)
        if not has(rs, "PHOTOGRAPHER"):
            return
        q = await s.execute(
            select(Booking, Shooting)
            .join(Shooting, Shooting.booking_id == Booking.id)
            .where(
                Booking.photographer_id == u.id,
                Booking.status.notin_(("PENDING_CONFIRMATION", "REJECTED")),
            )
            .order_by(Booking.shoot_date, Booking.shoot_time)
        )
        rows = q.all()
        if not rows:
            return await m.answer("Съёмок нет.")
        for b, sh in rows:
            buttons = []
            if sh.status == "ASSIGNED":
                buttons = [[("📥 Забрать съёмку", f"photo:pickup:{sh.id}", "primary")]]
            elif sh.status == "PICKED_UP":
                buttons = [[("▶️ Начать фотосессию", f"photo:start:{sh.id}", "primary")]]
            elif sh.status == "SHOOTING":
                buttons = [[("⏹ Окончить фотосессию", f"photo:finish:{sh.id}", "primary")]]
            elif sh.status == "SHOT":
                buttons = [[("✅ Готово к продаже", f"photo:ready:{sh.id}", "success")]]
            elif sh.status == "READY_FOR_SALE":
                buttons = [[("💰 Оформить продажу", f"sale:start:{b.id}", "success")]]
                if sh.full_upload_completed_at is None:
                    buttons.append(
                        [("☁️ Загрузить всю съёмку", f"photo:full_upload:{sh.id}", "primary")]
                    )
            text = await booking_card(s, b)
            if sh.status == "READY_FOR_SALE":
                text += (
                    "\n\n☁️ Полная съёмка: загружена."
                    if sh.full_upload_completed_at
                    else "\n\n☁️ Полная съёмка: можно загрузить позже. "
                         "Ставка фотографа будет определена только после полной загрузки."
                )
            await m.answer(text, reply_markup=inline(buttons) if buttons else None)


@r.callback_query(
    F.data.startswith(
        ("photo:pickup:", "photo:start:", "photo:finish:", "photo:ready:")
    )
)
async def action(c: CallbackQuery, state):
    try:
        _, act, raw_id = c.data.split(":", 2)
        sid = int(raw_id)
        if not 0 < sid <= 2**31 - 1:
            raise ValueError
    except (TypeError, ValueError):
        return await c.answer("Некорректная кнопка.")
    transitions = {
        "pickup": ("ASSIGNED", "PICKED_UP", "accepted_at"),
        "start": ("PICKED_UP", "SHOOTING", "started_at"),
        "finish": ("SHOOTING", "SHOT", "completed_at"),
        "ready": ("SHOT", "READY_FOR_SALE", "ready_for_sale_at"),
    }
    async with Session() as s:
        u = await get_user(s, c.from_user.id)
        sh = (
            await s.execute(
                select(Shooting).where(Shooting.id == sid).with_for_update()
            )
        ).scalar_one_or_none()
        if sh is None:
            return await c.answer("Съёмка не найдена.")
        b = await s.get(Booking, sh.booking_id)
        if u is None or not u.active or b is None or b.photographer_id != u.id:
            return await c.answer("Это не ваша съёмка.")
        expected, target, timestamp = transitions[act]
        if sh.status != expected:
            return await c.answer(
                "Этот шаг уже выполнен или предыдущий ещё не завершён."
            )
        now = datetime.now(UTC).replace(tzinfo=None)
        sh.status = target
        setattr(sh, timestamp, now)
        b.status = target
        await audit(s, u, f"shooting_{act}", "shooting", sid)
        await s.commit()
        if act == "ready":
            await notify_manager_ready_for_sale(c.bot, s, b)
    await state.clear()
    await c.answer()
    labels = {
        "pickup": (
            "📥 Съёмка принята. Когда будете готовы начать, нажмите кнопку.",
            [[("▶️ Начать фотосессию", f"photo:start:{sid}", "primary")]],
        ),
        "start": (
            "▶️ Фотосессия начата.",
            [[("⏹ Окончить фотосессию", f"photo:finish:{sid}", "primary")]],
        ),
        "finish": (
            (
                "⏹ Фотосессия окончена. Обработайте выбранные кадры. "
                "Когда материалы готовы для клиента, нажмите «Готово к продаже»."
            ),
            [[("✅ Готово к продаже", f"photo:ready:{sid}", "success")]],
        ),
        "ready": (
            (
                "💰 Съёмка готова к продаже. Менеджеру отправлено уведомление.\n\n"
                "Для продажи достаточно загрузить выбранные фотографии. "
                "Всю съёмку можно загрузить позже. До полной загрузки процент "
                "фотографа не фиксируется."
            ),
            [
                [("💰 Оформить продажу", f"sale:start:{b.id}", "success")],
                [("☁️ Загрузить всю съёмку", f"photo:full_upload:{sid}", "primary")],
            ],
        ),
    }
    text, buttons = labels[act]
    await c.message.answer(text, reply_markup=inline(buttons))


@r.callback_query(F.data.startswith("photo:full_upload:"))
async def start_full_upload(c: CallbackQuery, state):
    if c.message is None:
        return await c.answer("Некорректная кнопка.", show_alert=True)
    try:
        sid = int(c.data.rsplit(":", 1)[1])
        if not 0 < sid <= 2**31 - 1:
            raise ValueError
    except (TypeError, ValueError):
        return await c.answer("Некорректная кнопка.", show_alert=True)
    async with Session() as s:
        u = await get_user(s, c.from_user.id)
        sh = await s.get(Shooting, sid)
        booking = await s.get(Booking, sh.booking_id) if sh else None
        if (
            sh is None
            or booking is None
            or booking.photographer_id != u.id
            or sh.status != "READY_FOR_SALE"
        ):
            return await c.answer("Съёмка недоступна для загрузки.", show_alert=True)
        if sh.full_upload_completed_at is not None:
            return await c.answer("Вся съёмка уже отмечена как загруженная.", show_alert=True)
    await state.set_state(PhotoUploadFlow.uploading)
    await state.set_data({"shooting_id": sid})
    await c.answer()
    await c.message.answer(
        "☁️ Загружайте все кадры этой съёмки. Можно отправлять по одному или альбомом. "
        "JPEG, PNG и WebP до 20 МБ на файл.\n\n"
        "Процент фотографа будет рассчитан только после кнопки "
        "«Вся съёмка загружена».",
        reply_markup=inline(
            [[("✅ Вся съёмка загружена", "photo:upload_done", "success")]]
        ),
    )


@r.message(PhotoUploadFlow.uploading, F.photo | F.document)
async def upload_sale_photo(m, state):
    data = await state.get_data()
    shooting_id = data.get("shooting_id")
    attachment = m.photo[-1] if m.photo else m.document
    source_kind = "PHOTO" if m.photo else "DOCUMENT"
    file_id = attachment.file_id
    file_unique_id = attachment.file_unique_id

    if source_kind == "DOCUMENT":
        if attachment.file_size and attachment.file_size > MAX_TELEGRAM_IMAGE_BYTES:
            return await m.answer(
                "Файл больше 20 МБ. Уменьшите его или отправьте как обычную фотографию."
            )
        if attachment.mime_type not in {"image/jpeg", "image/png", "image/webp"}:
            return await m.answer("Как файл можно отправлять только JPEG, PNG или WebP.")
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        shooting = await s.get(Shooting, shooting_id)
        booking = await s.get(Booking, shooting.booking_id) if shooting else None
        if (
            booking is None
            or booking.photographer_id != u.id
            or shooting.status != "READY_FOR_SALE"
            or shooting.full_upload_completed_at is not None
        ):
            await state.clear()
            return await m.answer("Эта полная загрузка уже закрыта.")

        existing_storage = await s.scalar(
            select(PhotoStorage).where(
                PhotoStorage.shooting_id == shooting.id,
                PhotoStorage.telegram_unique_id == file_unique_id,
            )
        )
        created_photo = False
        if existing_storage is not None:
            photo = await s.get(Photo, existing_storage.photo_id)
        else:
            photo = await s.scalar(
                select(Photo).where(
                    Photo.shooting_id == shooting.id,
                    Photo.file_id == file_id,
                )
            )
            if photo is None:
                photo = Photo(shooting_id=shooting.id, file_id=file_id)
                s.add(photo)
                await s.flush()
                created_photo = True
            storage, storage_created = await ensure_photo_storage(
                s,
                photo_id=photo.id,
                shooting_id=shooting.id,
                file_id=file_id,
                file_unique_id=file_unique_id,
                source_kind=source_kind,
            )
            if (
                created_photo
                and not storage_created
                and storage is not None
                and storage.photo_id != photo.id
            ):
                await s.delete(photo)
                photo = await s.get(Photo, storage.photo_id)
        await s.commit()
        count = await s.scalar(
            select(func.count(Photo.id)).where(Photo.shooting_id == shooting.id)
        )
        sync = await storage_summary(s, shooting.id)
    await m.answer(
        f"✅ В полной съёмке сейчас: {count} кадров. "
        f"Яндекс.Диск: {sync['stored']} сохранено, {sync['pending']} в очереди"
        + (f", {sync['failed']} требуют повтора." if sync["failed"] else "."),
        reply_markup=inline(
            [[("✅ Вся съёмка загружена", "photo:upload_done", "success")]]
        ),
    )


@r.message(PhotoUploadFlow.uploading)
async def require_sale_photo(m):
    await m.answer(
        "Отправьте JPEG, PNG или WebP как фотографию или файл до 20 МБ, "
        "либо нажмите «Вся съёмка загружена»."
    )


@r.callback_query(PhotoUploadFlow.uploading, F.data == "photo:upload_done")
async def finish_photo_upload(c: CallbackQuery, state):
    if c.message is None:
        return await c.answer("Некорректная кнопка.", show_alert=True)
    data = await state.get_data()
    shooting_id = data.get("shooting_id")
    async with Session() as s:
        u = await get_user(s, c.from_user.id)
        shooting = await s.get(Shooting, shooting_id, with_for_update=True)
        booking = await s.get(Booking, shooting.booking_id) if shooting else None
        if (
            booking is None
            or booking.photographer_id != u.id
            or shooting.status != "READY_FOR_SALE"
            or shooting.full_upload_completed_at is not None
        ):
            await state.clear()
            return await c.answer("Загрузка уже закрыта.", show_alert=True)
        count = await s.scalar(
            select(func.count(Photo.id)).where(Photo.shooting_id == shooting.id)
        )
        if not count:
            return await c.answer("Сначала загрузите всю съёмку.", show_alert=True)
        declared = await s.scalar(
            select(func.max(Sale.declared_photo_count)).where(
                Sale.booking_id == booking.id,
                Sale.declared_photo_count.is_not(None),
            )
        )
        if declared and count < declared:
            return await c.answer(
                f"По продаже указано {declared} кадров, а загружено {count}. "
                "Загрузите оставшиеся кадры перед завершением.",
                show_alert=True,
            )
        sync = await storage_summary(s, shooting.id)
        shooting.full_upload_completed_at = datetime.now(UTC).replace(tzinfo=None)
        commission_state = await finalize_photographer_commissions(s, booking)
        await audit(
            s,
            u,
            "full_shoot_upload_completed",
            "shooting",
            shooting.id,
            (
                f"photos={count};percent={commission_state['percent']};"
                f"sales_updated={commission_state['sales']};"
                f"disk_pending={sync['pending']};disk_failed={sync['failed']}"
            ),
        )
        await s.commit()
    await state.clear()
    await c.answer()
    percent = commission_state["percent"]
    await c.message.answer(
        f"✅ Полная съёмка отмечена загруженной: {count} кадров.\n"
        f"📈 Ставка фотографа за эту съёмку зафиксирована: {percent:g}%.\n"
        f"Обновлено продаж: {commission_state['sales']}.\n"
        f"☁️ Яндекс.Диск: {sync['stored']} сохранено, {sync['pending']} ещё синхронизируются"
        + (f", ошибок: {sync['failed']}." if sync["failed"] else ".")
    )


@r.message(F.text == "📊 Моя статистика")
async def stats(m):
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        n = (
            await s.execute(
                select(func.count(Photo.id))
                .join(Shooting, Shooting.id == Photo.shooting_id)
                .join(Booking, Booking.id == Shooting.booking_id)
                .where(Booking.photographer_id == u.id)
            )
        ).scalar() or 0
        rev = (
            await s.execute(
                select(func.coalesce(func.sum(Sale.amount), 0)).where(
                    Sale.credited_user_id == u.id
                )
            )
        ).scalar() or 0
        await m.answer(f"📊 Статистика\nФото: {n}\nПродажи засчитаны: {rev:.2f} ₽")


@r.message(F.text == "💰 Мои выплаты")
async def payouts(m):
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        rows = (
            (
                await s.execute(
                    select(PayrollEntry)
                    .where(PayrollEntry.user_id == u.id)
                    .order_by(PayrollEntry.created_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        await m.answer(
            "\n".join(f"{x.period}: {x.kind} {x.amount:.2f} ₽" for x in rows)
            or "Выплат пока нет."
        )


@r.message(F.text == "👤 Профиль")
async def profile(m):
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        rs = await roles_of(s, u)
        await m.answer(
            f"👤 {u.name}\nРоли: {', '.join(ROLES[x] for x in rs)}"
        )

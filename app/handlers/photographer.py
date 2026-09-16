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
    GuestSelectedPhoto,
    PayrollEntry,
    Photo,
    Sale,
    ShiftCheckIn,
    ShiftCheckOut,
    Shooting,
)
from ..services.bookings import booking_card
from ..services.core import ROLES, audit, get_user, has, menu, roles_of
from ..services.shifts import LATE_FINE, is_late, shift_now

r = Router()
r.message.filter(StaffFilter("PHOTOGRAPHER", "MANAGER"))
r.callback_query.filter(StaffFilter("PHOTOGRAPHER", "MANAGER"))


class ShiftFlow(StatesGroup):
    location = State()
    photo = State()
    end_location = State()
    workplace_photo = State()


class PhotoUploadFlow(StatesGroup):
    uploading = State()
    selecting = State()


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
    if not ({"PHOTOGRAPHER", "MANAGER"} & current_roles):
        return await m.answer("Смена доступна фотографу или менеджеру.")
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
            reply_markup=inline([[('🏁 Закончить смену', 'shift:end')]]),
        )
    if check_in.status == "AWAITING_PHOTO":
        return await ask_for_full_body_photo(m, state)
    return await ask_for_location(m, state)


@r.callback_query(F.data == "shift:begin")
async def begin_shift(c: CallbackQuery, state, current_roles):
    if c.message is None:
        return await c.answer("Некорректная кнопка.", show_alert=True)
    if not ({"PHOTOGRAPHER", "MANAGER"} & current_roles):
        return await c.answer("Нужна роль фотографа или менеджера.", show_alert=True)
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
            await m.answer(
                await booking_card(s, b),
                reply_markup=inline(
                    [
                        [
                            ("📥 Забрал съёмку", f"photo:pickup:{sh.id}", "primary"),
                        ],
                        [
                            ("📸 Отснял съёмку", f"photo:shot:{sh.id}", "primary"),
                        ],
                        [
                            ("💰 Готово к продаже", f"photo:ready:{sh.id}", "success"),
                        ],
                    ]
                ),
            )


@r.callback_query(
    F.data.startswith(
        ("photo:pickup:", "photo:shot:", "photo:ready:")
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
        "shot": ("PICKED_UP", "SHOT", "completed_at"),
        "ready": ("SHOT", "UPLOADING", "completed_at"),
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
        sh.status = target
        setattr(sh, timestamp, datetime.now(UTC).replace(tzinfo=None))
        b.status = target
        await audit(s, u, f"shooting_{act}", "shooting", sid)
        await s.commit()
        if act == "ready":
            await state.set_state(PhotoUploadFlow.uploading)
            await state.set_data({"shooting_id": sid})
            await c.message.answer(
                "📤 Загрузите сюда все готовые фотографии этой съёмки. "
                "Можно отправлять по одной или альбомом. Когда закончите, нажмите кнопку ниже.",
                reply_markup=inline(
                    [[("✅ Завершить загрузку", "photo:upload_done", "success")]]
                ),
            )
        else:
            labels = {
                "pickup": "📥 Съёмка забрана.",
                "shot": "📸 Съёмка закончена.",
            }
            await c.message.answer(labels[act])
        await c.answer()


@r.message(PhotoUploadFlow.uploading, F.photo | F.document)
async def upload_sale_photo(m, state):
    data = await state.get_data()
    shooting_id = data.get("shooting_id")
    file_id = m.photo[-1].file_id if m.photo else m.document.file_id
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        shooting = await s.get(Shooting, shooting_id)
        booking = await s.get(Booking, shooting.booking_id) if shooting else None
        if booking is None or booking.photographer_id != u.id or shooting.status != "UPLOADING":
            await state.clear()
            return await m.answer("Эта загрузка уже закрыта. Откройте «📸 Мои съёмки».")
        exists = await s.scalar(
            select(Photo.id).where(Photo.shooting_id == shooting.id, Photo.file_id == file_id)
        )
        if exists is None:
            s.add(Photo(shooting_id=shooting.id, file_id=file_id))
            await s.commit()
        count = await s.scalar(
            select(func.count(Photo.id)).where(Photo.shooting_id == shooting.id)
        )
    await m.answer(
        f"✅ Загружено фотографий: {count}",
        reply_markup=inline(
            [[("✅ Завершить загрузку", "photo:upload_done", "success")]]
        ),
    )


@r.message(PhotoUploadFlow.uploading)
async def require_sale_photo(m):
    await m.answer("Отправьте фотографию или файл с фотографией.")


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
        if booking is None or booking.photographer_id != u.id or shooting.status != "UPLOADING":
            await state.clear()
            return await c.answer("Загрузка уже закрыта.", show_alert=True)
        count = await s.scalar(
            select(func.count(Photo.id)).where(Photo.shooting_id == shooting.id)
        )
        if not count:
            return await c.answer("Сначала загрузите фотографии.", show_alert=True)
        await audit(
            s, u, "shooting_all_photos_uploaded", "shooting", shooting.id, f"photos={count}"
        )
        await s.commit()
    await state.set_state(PhotoUploadFlow.selecting)
    await state.update_data({"shooting_id": shooting_id})
    await c.answer()
    await c.message.answer(
        f"✅ Все фотографии сохранены: {count}.\n\n"
        "Теперь пришлите сюда фотографии, которые выбрал гость. "
        "Можно отправить несколько. Когда закончите — нажмите кнопку.",
        reply_markup=inline(
            [[("✅ Выбор гостя завершён", "photo:selection_done", "success")]]
        ),
    )



@r.message(PhotoUploadFlow.selecting, F.photo | F.document)
async def save_guest_selected_photo(m, state):
    data = await state.get_data()
    shooting_id = data.get("shooting_id")
    file_id = m.photo[-1].file_id if m.photo else m.document.file_id
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        shooting = await s.get(Shooting, shooting_id)
        booking = await s.get(Booking, shooting.booking_id) if shooting else None
        if booking is None or booking.photographer_id != u.id or shooting.status != "UPLOADING":
            await state.clear()
            return await m.answer("Эта загрузка уже закрыта. Откройте «📸 Мои съёмки».")
        exists = await s.scalar(
            select(GuestSelectedPhoto.id).where(
                GuestSelectedPhoto.shooting_id == shooting.id,
                GuestSelectedPhoto.file_id == file_id,
            )
        )
        if exists is None:
            s.add(
                GuestSelectedPhoto(
                    shooting_id=shooting.id, file_id=file_id, selected_by_id=u.id
                )
            )
            await s.commit()
        count = await s.scalar(
            select(func.count(GuestSelectedPhoto.id)).where(
                GuestSelectedPhoto.shooting_id == shooting.id
            )
        )
    await m.answer(
        f"💛 Выбрано гостем: {count}",
        reply_markup=inline(
            [[("✅ Выбор гостя завершён", "photo:selection_done", "success")]]
        ),
    )


@r.message(PhotoUploadFlow.selecting)
async def require_guest_selected_photo(m):
    await m.answer("Пришлите выбранную гостем фотографию или нажмите «Выбор гостя завершён».")


@r.callback_query(PhotoUploadFlow.selecting, F.data == "photo:selection_done")
async def finish_guest_selection(c: CallbackQuery, state):
    if c.message is None:
        return await c.answer("Некорректная кнопка.", show_alert=True)
    data = await state.get_data()
    shooting_id = data.get("shooting_id")
    async with Session() as s:
        u = await get_user(s, c.from_user.id)
        shooting = await s.get(Shooting, shooting_id, with_for_update=True)
        booking = await s.get(Booking, shooting.booking_id) if shooting else None
        if booking is None or booking.photographer_id != u.id or shooting.status != "UPLOADING":
            await state.clear()
            return await c.answer("Загрузка уже закрыта.", show_alert=True)
        selected_count = await s.scalar(
            select(func.count(GuestSelectedPhoto.id)).where(
                GuestSelectedPhoto.shooting_id == shooting.id
            )
        ) or 0
        shooting.status = "READY_FOR_SALE"
        booking.status = "READY_FOR_SALE"
        await audit(
            s,
            u,
            "guest_selection_completed",
            "shooting",
            shooting.id,
            f"selected_by_guest={selected_count}",
        )
        await s.commit()
    await state.clear()
    await c.answer()
    await c.message.answer(
        f"💰 Готово к продаже. Выбрано гостем: {selected_count}.\n\n"
        "Работу можно отправить в Академию: выбранные гостем кадры будут отмечены отдельно.",
        reply_markup=inline(
            [[("🎓 Отправить работу в Академию", f"academy:send-review:{shooting_id}", "primary")]]
        ),
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

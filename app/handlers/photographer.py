from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery
from sqlalchemy import func, select

from ..access import StaffFilter
from ..db import Session
from ..keyboards import inline
from ..models import (
    Booking,
    PayrollEntry,
    Photo,
    PhotoStorage,
    Sale,
    ShiftCheckIn,
    ShiftCheckOut,
    Shooting,
    User,
)
from ..services.bookings import booking_card, notify_manager_ready_for_sale
from ..services.core import ROLES, get_user, has, roles_of
from ..services.photo_storage import (
    MAX_TELEGRAM_IMAGE_BYTES,
    receive_photo,
    storage_summary,
    unique_photo_count,
)
from ..services.photo_storage import (
    _download as download_photo_bytes,
)
from ..services.sale_workflow import (
    complete_full_upload,
)
from ..services.shifts import shift_now

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


async def today_check_in(session, user_id, *, lock=False, shift_id=None):
    query = select(ShiftCheckIn).where(
        ShiftCheckIn.user_id == user_id,
        ShiftCheckIn.shift_date == shift_now().date(),
        ShiftCheckIn.scheduled_shift_id == shift_id if shift_id is not None else ShiftCheckIn.scheduled_shift_id.is_(None),
    )
    if lock:
        query = query.with_for_update()
    return (await session.scalars(query)).one_or_none()


async def today_check_out(session, user_id, *, lock=False, shift_id=None):
    query = select(ShiftCheckOut).where(
        ShiftCheckOut.user_id == user_id,
        ShiftCheckOut.shift_date == shift_now().date(),
        ShiftCheckOut.scheduled_shift_id == shift_id if shift_id is not None else ShiftCheckOut.scheduled_shift_id.is_(None),
    )
    if lock:
        query = query.with_for_update()
    return (await session.scalars(query)).one_or_none()


async def open_shift_camera(message, state):
    # Telegram does not disclose whether an uploaded photo is from its camera or
    # gallery. All attendance writes therefore use one live-camera API workflow.
    from ..launch_policy import app_markup
    await state.clear()
    await message.answer(
        'Отметка смены выполняется в Photo Boss: откройте «Моя смена», выберите отель, '
        'отправьте текущую геолокацию и сделайте снимок живой камерой. '
        'При начале смены ИИ проверит полный рост. Результат общий для бота и приложения.',
        reply_markup=app_markup('home', message.chat.id),
    )


@r.message(F.text == "🔄 Моя смена")
async def my_shift(m, state, current_roles):
    if 'PHOTOGRAPHER' not in current_roles:
        return await m.answer('Смена доступна сотруднику с ролью фотографа.')
    async with Session() as session:
        user = await get_user(session, m.from_user.id)
        starts = (await session.scalars(select(ShiftCheckIn).where(
            ShiftCheckIn.user_id == user.id, ShiftCheckIn.shift_date == shift_now().date(),
        ).order_by(ShiftCheckIn.id))).all()
        ends = (await session.scalars(select(ShiftCheckOut).where(
            ShiftCheckOut.user_id == user.id, ShiftCheckOut.shift_date == shift_now().date(),
        ))).all()
        closed = {row.scheduled_shift_id for row in ends if row.status == 'FINISHED'}
        active = [row for row in starts if row.status == 'STARTED' and row.ai_status in ('ACCEPTED', 'MANUAL_APPROVED') and row.scheduled_shift_id not in closed]
        pending = any(row.status == 'PENDING_REVIEW' for row in [*starts, *ends])
    if active:
        await m.answer('✅ Подтверждённых открытых смен: ' + str(len(active)) + '. Последнее начало: ' + shift_now(active[-1].started_at).strftime('%H:%M') + '.')
    elif pending:
        await m.answer('Офлайн-отметка сохранена и ожидает решения руководителя. Начало смены автоматически не подтверждено.')
    elif starts and all(row.scheduled_shift_id in closed for row in starts):
        await m.answer('🏁 Подтверждённые смены завершены. Следующую смену выберите в графике.')
    else:
        await m.answer('🔄 Моя смена. Начало и штраф за опоздание учитываются по времени выбранной смены в графике. Если график не задан, начало — 09:00, штраф — 500 ₽.')
    return await open_shift_camera(m, state)


@r.callback_query(F.data == "shift:begin")
async def begin_shift(c: CallbackQuery, state, current_roles):
    if c.message is None or 'PHOTOGRAPHER' not in current_roles:
        return await c.answer('Нужна роль фотографа.', show_alert=True)
    await c.answer()
    return await open_shift_camera(c.message, state)


@r.callback_query(F.data == "shift:end")
async def end_shift(c: CallbackQuery, state, current_roles):
    if c.message is None or 'PHOTOGRAPHER' not in current_roles:
        return await c.answer('Нужна роль фотографа.', show_alert=True)
    await c.answer()
    return await open_shift_camera(c.message, state)


@r.message(ShiftFlow.location, F.location)
async def save_shift_location(m, state):
    return await open_shift_camera(m, state)


@r.message(ShiftFlow.location)
async def require_shift_location(m, state):
    return await open_shift_camera(m, state)


@r.message(ShiftFlow.photo, F.photo)
async def save_shift_photo(m, state, current_roles):
    return await open_shift_camera(m, state)


@r.message(ShiftFlow.photo)
async def require_shift_photo(m, state):
    return await open_shift_camera(m, state)


@r.message(ShiftFlow.end_location, F.location)
async def save_end_location(m, state):
    return await open_shift_camera(m, state)


@r.message(ShiftFlow.end_location)
async def require_end_location(m, state):
    return await open_shift_camera(m, state)


@r.message(ShiftFlow.workplace_photo, F.photo)
async def save_workplace_photo(m, state, current_roles):
    return await open_shift_camera(m, state)


@r.message(ShiftFlow.workplace_photo)
async def require_workplace_photo(m, state):
    return await open_shift_camera(m, state)


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
                buttons = [[("🎨 На обработку", f"photo:processing:{sh.id}", "primary")],
                           [("🕐 Назначить просмотр", f"photo:viewing:{sh.id}")]]
            elif sh.status == "PROCESSING":
                buttons = [[("✅ Готово к продаже", f"photo:ready:{sh.id}", "success")],
                           [("🕐 Изменить время просмотра", f"photo:viewing:{sh.id}")]]
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


class ViewingFlow(StatesGroup):
    appointment = State()


@r.callback_query(F.data.startswith("photo:viewing:"))
async def begin_viewing(c: CallbackQuery, state):
    from ..services.shifts import require_active_shift
    from ..services.shooting_workflow import photographer_shooting, viewing_allowed
    if c.message is None:
        return await c.answer("Некорректная кнопка.", show_alert=True)
    try:
        sid = int(c.data.rsplit(':', 1)[1])
        async with Session() as session:
            actor = await get_user(session, c.from_user.id)
            shooting, booking = await photographer_shooting(session, actor, sid)
            viewing_allowed(shooting.status, booking.status)
            await require_active_shift(session, actor.id, booking.hotel_id)
    except (ValueError, TypeError) as exc:
        return await c.answer(str(exc), show_alert=True)
    await state.set_state(ViewingFlow.appointment)
    await state.set_data({'shooting_id': sid})
    await c.answer()
    await c.message.answer(
        "🕐 Согласуйте с гостем время просмотра. Отправьте дату и время: "
        "ДД.ММ.ГГГГ ЧЧ:ММ. Если просмотр в другой день, добавьте после | причину переноса.\n"
        "Например: 16.10.2026 11:30 | Гость уезжает на экскурсию."
    )


@r.message(ViewingFlow.appointment)
async def save_viewing(m, state):
    from ..services.shooting_workflow import schedule_viewing
    data = await state.get_data()
    try:
        date_text, _, comment = (m.text or '').partition('|')
        at = datetime.strptime(date_text.strip(), '%d.%m.%Y %H:%M').replace(tzinfo=shift_now().tzinfo).isoformat(timespec='minutes')
        async with Session() as session:
            actor = await get_user(session, m.from_user.id)
            shooting, _booking = await schedule_viewing(session, actor, data.get('shooting_id'), at, comment.strip())
            stage = shooting.status
            await session.commit()
    except (ValueError, TypeError) as exc:
        return await m.answer(str(exc) if str(exc) and 'does not match format' not in str(exc) else
                              'Укажите время в формате ДД.ММ.ГГГГ ЧЧ:ММ; для другого дня добавьте | причину.')
    await state.clear()
    action, label = ('ready', '✅ Готово к продаже') if stage == 'PROCESSING' else ('processing', '🎨 На обработку')
    buttons = [[(label, f"photo:{action}:{data['shooting_id']}", 'primary')]] if stage != 'READY_FOR_SALE' else []
    await m.answer('✅ Время просмотра сохранено.', reply_markup=inline(buttons) if buttons else None)


@r.callback_query(F.data.startswith(("photo:pickup:", "photo:start:", "photo:finish:", "photo:processing:", "photo:ready:")))
async def action(c: CallbackQuery, state):
    from ..services.shooting_workflow import advance_shooting
    if c.message is None:
        return await c.answer("Некорректная кнопка.", show_alert=True)
    try:
        _, act, raw_id = c.data.split(":", 2)
        sid = int(raw_id)
        if not 0 < sid <= 2**31 - 1:
            raise ValueError('Некорректная кнопка.')
        async with Session() as session:
            actor = await get_user(session, c.from_user.id)
            _shooting, booking = await advance_shooting(session, actor, sid, act)
            await session.commit()
            if act == 'ready':
                await notify_manager_ready_for_sale(c.bot, session, booking)
            bid = booking.id
    except (TypeError, ValueError) as exc:
        return await c.answer(str(exc), show_alert=True)
    await state.clear()
    await c.answer()
    labels = {
        'pickup': ('📥 Съёмка принята. Начните подтверждённую смену перед съёмкой.',
                   [[('▶️ Начать фотосессию', f'photo:start:{sid}', 'primary')]]),
        'start': ('▶️ Фотосессия начата.', [[('⏹ Окончить фотосессию', f'photo:finish:{sid}', 'primary')]]),
        'finish': ('⏹ Фотосессия окончена. Согласуйте с гостем время просмотра и начните обработку.',
                   [[('🕐 Назначить просмотр', f'photo:viewing:{sid}', 'primary')],
                    [('🎨 На обработку', f'photo:processing:{sid}')]]),
        'processing': ('🎨 Съёмка на обработке. Когда кадры готовы, подтвердите готовность к продаже.',
                       [[('✅ Готово к продаже', f'photo:ready:{sid}', 'success')],
                        [('🕐 Назначить просмотр', f'photo:viewing:{sid}')]]),
        'ready': (('💰 Съёмка готова к продаже. Для продажи загрузите выбранные фотографии. '
                   'Ставка фотографа фиксируется после полной загрузки.'),
                  [[('💰 Оформить продажу', f'sale:start:{bid}', 'success')],
                   [('☁️ Загрузить всю съёмку', f'photo:full_upload:{sid}', 'primary')]]),
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
        from ..services.shifts import require_active_shift
        try:
            await require_active_shift(s, u.id, booking.hotel_id)
        except ValueError as exc:
            return await c.answer(str(exc), show_alert=True)
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
        supplied_type = (attachment.mime_type or '').lower()
        file_name = (attachment.file_name or '').lower()
        supported_extension = file_name.endswith(('.jpg', '.jpeg', '.png', '.webp'))
        if supplied_type not in {"image/jpeg", "image/png", "image/webp"} and not (
            supplied_type in {'', 'application/octet-stream'} and supported_extension
        ):
            return await m.answer("Как файл можно отправлять только JPEG, PNG или WebP.")
    async with Session() as s:
        u = await get_user(s, m.from_user.id)
        await s.get(User, u.id, with_for_update=True)
        shooting = await s.get(Shooting, shooting_id, with_for_update=True)
        booking = await s.get(Booking, shooting.booking_id) if shooting else None
        if (
            booking is None
            or booking.photographer_id != u.id
            or shooting.status != "READY_FOR_SALE"
            or shooting.full_upload_completed_at is not None
        ):
            await state.clear()
            return await m.answer("Эта полная загрузка уже закрыта.")

        from ..services.shifts import require_active_shift
        try:
            await require_active_shift(s, u.id, booking.hotel_id)
            raw = await download_photo_bytes(m.bot, file_id)
            _storage, created = await receive_photo(s, shooting_id=shooting.id,
                file_id=file_id, file_unique_id=file_unique_id, source_kind=source_kind, raw=raw)
        except ValueError as exc:
            return await m.answer(str(exc))
        except (TelegramAPIError, OSError, TimeoutError):
            return await m.answer('Не удалось получить файл из Telegram. Кадр не добавлен; отправьте его повторно.')
        await s.commit()
        count = await unique_photo_count(s, shooting.id)
        sync = await storage_summary(s, shooting.id)
    await m.answer(
        ('Этот кадр уже есть. ' if not created else '')
        + f"✅ В полной съёмке сейчас: {count} кадров. "
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
        try:
            completed = await complete_full_upload(s, u, shooting_id)
        except ValueError as exc:
            return await c.answer(str(exc), show_alert=True)
        count, sync, commission_state = completed.count, completed.sync, completed.commissions
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
        photo_rows = (
            await s.execute(
                select(Photo.shooting_id, Photo.id, PhotoStorage.sha256)
                .outerjoin(PhotoStorage, PhotoStorage.photo_id == Photo.id)
                .join(Shooting, Shooting.id == Photo.shooting_id)
                .join(Booking, Booking.id == Shooting.booking_id)
                .where(Booking.photographer_id == u.id)
            )
        ).all()
        n = len({(sid, digest or f'photo:{pid}') for sid, pid, digest in photo_rows})
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

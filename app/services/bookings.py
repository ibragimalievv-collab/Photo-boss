from aiogram.exceptions import TelegramAPIError
from sqlalchemy import func, select

from ..keyboards import inline
from ..models import Booking, Client, Hotel, Package, Photo, Sale, Shooting, User
from .commissions import photographer_percent
from .receipts import payment_totals

STATUS_NAMES = {
    "NEW": "Новая",
    "PENDING_CONFIRMATION": "Ожидает подтверждения",
    "CONFIRMED": "Подтверждена",
    "ASSIGNED": "Назначена фотографу",
    "RESCHEDULED": "Перенесена",
    "REJECTED": "Отказ",
    "CANCELLED": "Отменена",
    "PICKED_UP": "Фотограф забрал съёмку",
    "SHOT": "Съёмка отснята",
    "PROCESSING": "На обработке",
    "UPLOADING": "Загрузка готовых фотографий",
    "READY_FOR_SALE": "Готово к продаже",
    "ACCEPTED": "Съёмка забрана фотографом",
    "ARRIVED": "Фотограф на месте",
    "SHOOTING": "Идёт съёмка",
    "READY_FOR_MANAGER": "Съёмка завершена",
    "AWAITING_LOCATION": "Ожидается геопозиция",
    "AWAITING_PHOTO": "Ожидается фотография",
    "STARTED": "Смена начата",
    "FINISHED": "Смена завершена",
}


async def create_booking_record(session, actor, data, photographer_id=None):
    """One booking operation for the bot and queued Mini App requests."""
    from datetime import date, time
    from decimal import Decimal, InvalidOperation

    from ..models import UserRole
    from .core import audit, roles_of

    roles = await roles_of(session, actor)
    if not roles & {'OWNER', 'ADMIN', 'MANAGER'}:
        raise ValueError('Нет доступа к созданию записи.')
    if photographer_id is not None:
        if not roles & {'OWNER', 'ADMIN'}:
            raise ValueError('Назначать фотографа может только администратор или владелец.')
        photographer = await session.get(User, photographer_id)
        photo_roles = set(await session.scalars(select(UserRole.role).where(UserRole.user_id == photographer_id)))
        if photographer is None or not photographer.active or 'PHOTOGRAPHER' not in photo_roles:
            raise ValueError('Фотограф недоступен.')
    try:
        name = data['client_name'].strip()
        phone = data['client_phone']
        room = data['room'].strip()
        count = data['guest_count']
        deposit = Decimal(str(data['deposit']))
        day = date.fromisoformat(data['shoot_date'])
        at = time.fromisoformat(data['shoot_time'])
        if (not 2 <= len(name) <= 200 or not 1 <= len(room) <= 100
                or (phone is not None and (not isinstance(phone, str) or not 5 <= len(phone) <= 80))
                or type(count) is not int or not 1 <= count <= 100
                or not deposit.is_finite() or not 0 <= deposit <= 10_000_000 or at.tzinfo is not None):
            raise ValueError
        if type(data['hotel_id']) is not int or type(data['package_id']) is not int:
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation) as exc:
        raise ValueError('Проверьте данные гостя, дату, количество гостей и сумму брони.') from exc
    hotel = await session.get(Hotel, data['hotel_id'])
    package = await session.get(Package, data['package_id'])
    if hotel is None or not hotel.active or package is None or not package.active:
        raise ValueError('Отель или пакет больше не доступны. Обновите данные.')
    client = Client(name=name, phone=phone)
    session.add(client)
    await session.flush()
    booking = Booking(hotel_id=hotel.id, client_id=client.id, room=room, guest_count=count,
                      deposit=float(deposit.quantize(Decimal('0.01'))), shoot_date=day, shoot_time=at,
                      package_id=package.id, manager_id=actor.id, photographer_id=photographer_id,
                      status='PENDING_CONFIRMATION')
    session.add(booking)
    await session.flush()
    session.add(Shooting(booking_id=booking.id, status='PENDING_CONFIRMATION'))
    await audit(session, actor, 'booking_created', 'booking', booking.id)
    await session.flush()
    return booking


async def booking_card(session, booking: Booking):
    hotel = await session.get(Hotel, booking.hotel_id)
    client = await session.get(Client, booking.client_id)
    package = await session.get(Package, booking.package_id)
    manager = await session.get(User, booking.manager_id)
    photographer = (
        await session.get(User, booking.photographer_id)
        if booking.photographer_id
        else None
    )
    sale_total = await session.scalar(
        select(func.coalesce(func.sum(Sale.amount), 0)).where(
            Sale.booking_id == booking.id
        )
    )
    sold_photos = await session.scalar(
        select(func.coalesce(func.sum(Sale.sold_photos), 0)).where(
            Sale.booking_id == booking.id
        )
    )
    uploaded_photos = await session.scalar(
        select(func.count(Photo.id))
        .join(Shooting, Shooting.id == Photo.shooting_id)
        .where(Shooting.booking_id == booking.id)
    )
    _, paid, outstanding = await payment_totals(session, booking.id)
    return (
        f"📋 Запись #{booking.id}\n"
        f"Статус: {STATUS_NAMES.get(booking.status, 'Статус обновляется')}\n"
        f"🏨 Отель: {hotel.name if hotel else 'не найден'}\n"
        f"🚪 Комната: {booking.room}\n"
        f"👤 Клиент: {client.name if client else 'не найден'}\n"
        f"📞 Телефон: {(client.phone if client else None) or 'не указан'}\n"
        f"👥 Количество гостей: {booking.guest_count}\n"
        f"📅 Дата: {booking.shoot_date:%d.%m.%Y}\n"
        f"🕐 Время: {booking.shoot_time:%H:%M}\n"
        f"📦 Пакет: {package.name if package else 'не найден'}\n"
        f"💳 Бронь: {booking.deposit:.2f} ₽ (заявлено)\n"
        f"💰 Продажа: {sale_total or 0:.2f} ₽\n"
        f"✅ Поступление подтверждено владельцем: {paid:.2f} ₽\n"
        f"Осталось оплатить по продажам: {outstanding:.2f} ₽\n"
        f"🖼 Кадры: сфотографировано {uploaded_photos or 0}, куплено {sold_photos or 0}\n"
        f"📈 Ставка фотографа: {photographer_percent(uploaded_photos or 0):g}% "
        "от продаж этой съёмки\n"
        f"📋 Менеджер: {manager.name if manager else 'не найден'}\n"
        f"📸 Фотограф: {photographer.name if photographer else 'не назначен'}"
        + (
            f"\n❌ Причина отмены: {booking.cancellation_reason}"
            if booking.status == "CANCELLED" and booking.cancellation_reason
            else ""
        )
    )


async def notify_photographer_assignment(bot, session, booking: Booking):
    if not booking.photographer_id:
        return False
    photographer = await session.get(User, booking.photographer_id)
    shooting = await session.scalar(
        select(Shooting).where(Shooting.booking_id == booking.id)
    )
    hotel = await session.get(Hotel, booking.hotel_id)
    if photographer is None or not photographer.active or shooting is None:
        return False
    try:
        await bot.send_message(
            photographer.tg_id,
            f"📸 Вам назначена фотосессия #{booking.id}\n"
            f"🏨 {hotel.name if hotel else 'Отель'}\n"
            f"📅 {booking.shoot_date:%d.%m.%Y} · {booking.shoot_time:%H:%M}\n"
            f"👥 Гостей: {booking.guest_count}\n\n"
            "Когда примете съёмку, нажмите «Забрать съёмку».",
            reply_markup=inline(
                [[("📥 Забрать съёмку", f"photo:pickup:{shooting.id}", "primary")]]
            ),
            disable_notification=False,
        )
        return True
    except TelegramAPIError:
        return False


async def notify_manager_ready_for_sale(bot, session, booking: Booking):
    manager = await session.get(User, booking.manager_id)
    if manager is None or not manager.active:
        return False
    try:
        await bot.send_message(
            manager.tg_id,
            f"💰 Съёмка #{booking.id} готова к продаже.\n"
            "Сначала загрузите чек продажи, затем укажите количество кадров "
            "и количество проданных кадров, после чего загрузите выбранные фотографии.",
            reply_markup=inline(
                [[("💰 Оформить продажу", f"sale:start:{booking.id}", "success")]]
            ),
            disable_notification=False,
        )
        return True
    except TelegramAPIError:
        return False


async def notify_photographer_rescheduled(bot, session, booking: Booking):
    if not booking.photographer_id:
        return False
    photographer = await session.get(User, booking.photographer_id)
    if photographer is None or not photographer.active:
        return False
    try:
        await bot.send_message(
            photographer.tg_id,
            f"📅 Фотосессия #{booking.id} перенесена.\n"
            f"Новая дата и время: {booking.shoot_date:%d.%m.%Y} · {booking.shoot_time:%H:%M}.",
            disable_notification=False,
        )
        return True
    except TelegramAPIError:
        return False


async def notify_photographer_cancelled(bot, session, booking: Booking):
    if not booking.photographer_id:
        return False
    photographer = await session.get(User, booking.photographer_id)
    if photographer is None or not photographer.active:
        return False
    try:
        await bot.send_message(
            photographer.tg_id,
            f"❌ Фотосессия #{booking.id} отменена.\n"
            f"Причина: {booking.cancellation_reason or 'не указана'}",
            disable_notification=False,
        )
        return True
    except TelegramAPIError:
        return False

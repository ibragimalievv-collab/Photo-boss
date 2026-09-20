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
    "PICKED_UP": "Фотограф забрал съёмку",
    "SHOT": "Съёмка отснята",
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

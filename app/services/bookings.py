from ..models import Booking, Client, Hotel, Package, User

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
    return (
        f"📋 Запись #{booking.id}\n"
        f"Статус: {STATUS_NAMES.get(booking.status, booking.status)}\n"
        f"🏨 Отель: {hotel.name if hotel else 'не найден'}\n"
        f"🚪 Комната: {booking.room}\n"
        f"👤 Клиент: {client.name if client else 'не найден'}\n"
        f"📞 Телефон: {(client.phone if client else None) or 'не указан'}\n"
        f"📅 Дата: {booking.shoot_date:%d.%m.%Y}\n"
        f"🕐 Время: {booking.shoot_time:%H:%M}\n"
        f"📦 Пакет: {package.name if package else 'не найден'}\n"
        f"📋 Менеджер: {manager.name if manager else 'не найден'}\n"
        f"📸 Фотограф: {photographer.name if photographer else 'не назначен'}"
    )

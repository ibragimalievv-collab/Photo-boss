import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from sqlalchemy import func, select

from ..config import config
from ..models import (
    AcademyCertificate,
    AcademyReminder,
    AcademyReview,
    Booking,
    BookingReminder,
    Client,
    HotelEmployee,
    Receipt,
    RepeatSaleLead,
    Sale,
    Setting,
    Shooting,
    User,
    UserRole,
)
from .academy import ACADEMY_LESSONS
from .academy_growth import academy_counts, ensure_certificate

ACTIVE_BOOKING_STATUSES = {
    "PENDING_CONFIRMATION", "CONFIRMED", "ASSIGNED", "PICKED_UP",
    "ACCEPTED", "ARRIVED", "SHOOTING", "RESCHEDULED",
}


def booking_moment(booking):
    return datetime.combine(booking.shoot_date, booking.shoot_time).replace(
        tzinfo=ZoneInfo(config.training_timezone)
    )


def reminder_kind(booking, now):
    now = now if now.tzinfo else now.replace(tzinfo=UTC)
    hours = (booking_moment(booking) - now).total_seconds() / 3600
    if 1.5 <= hours <= 2.5:
        return "2H"
    if 23.5 <= hours <= 24.5:
        return "24H"
    return None


async def run_operations_once(session, bot, now=None):
    now = now or datetime.now(UTC)
    bookings = (
        await session.scalars(
            select(Booking).where(Booking.status.in_(ACTIVE_BOOKING_STATUSES))
        )
    ).all()
    sent = 0
    for booking in bookings:
        kind = reminder_kind(booking, now)
        if kind is None:
            continue
        client = await session.get(Client, booking.client_id)
        recipients = {booking.manager_id, booking.photographer_id} - {None}
        for user_id in recipients:
            exists = await session.scalar(
                select(BookingReminder.id).where(
                    BookingReminder.booking_id == booking.id,
                    BookingReminder.user_id == user_id,
                    BookingReminder.reminder_kind == kind,
                )
            )
            if exists:
                continue
            user = await session.get(User, user_id)
            if user is None or not user.active:
                continue
            hours = "24 часа" if kind == "24H" else "2 часа"
            text = (
                f"⏰ Через {hours} съёмка #{booking.id}\n"
                f"Дата: {booking.shoot_date:%d.%m.%Y} {booking.shoot_time:%H:%M}\n"
                f"Комната: {booking.room}\n"
                f"Клиент: {client.name if client else 'не найден'}\n"
                f"Телефон: {(client.phone if client else None) or 'не указан'}"
            )
            try:
                await bot.send_message(user.tg_id, text)
            except TelegramAPIError:
                continue
            session.add(
                BookingReminder(
                    booking_id=booking.id, user_id=user.id, reminder_kind=kind
                )
            )
            sent += 1
    leads = await create_repeat_sale_leads(session, now)
    academy_reminders = await send_academy_reminders(session, bot, now)
    certificates = await issue_academy_certificates(session)
    await session.commit()
    return {"reminders": sent, "repeat_sale_leads": leads,
            "academy_reminders": academy_reminders,
            "academy_certificates": certificates}


async def send_academy_reminders(session, bot, now=None):
    now = now or datetime.now(UTC)
    local_now = now.astimezone(ZoneInfo(config.training_timezone))
    if local_now.hour != 10:
        return 0
    users = (await session.scalars(
        select(User).join(UserRole, UserRole.user_id == User.id).where(
            User.active.is_(True), UserRole.role == "PHOTOGRAPHER"
        ).order_by(User.id)
    )).unique().all()
    sent = 0
    for user in users:
        lessons, practices, _score = await academy_counts(session, user.id)
        if lessons >= len(ACADEMY_LESSONS) and practices >= 7:
            continue
        exists = await session.scalar(select(AcademyReminder.id).where(
            AcademyReminder.user_id == user.id,
            AcademyReminder.reminder_date == local_now.date(),
            AcademyReminder.kind == "DAILY_LESSON",
        ))
        if exists:
            continue
        try:
            await bot.send_message(
                user.tg_id,
                "📚 Photo Boss Academy\n\n"
                f"Ваш прогресс: {lessons}/28 уроков и {practices}/7 практик. "
                "Незавершённый урок не сгорает — продолжите с текущего шага.",
            )
        except TelegramAPIError:
            continue
        session.add(AcademyReminder(
            user_id=user.id, reminder_date=local_now.date()
        ))
        sent += 1
    return sent


async def issue_academy_certificates(session):
    user_ids = (await session.scalars(
        select(User.id).join(UserRole, UserRole.user_id == User.id).where(
            User.active.is_(True), UserRole.role == "PHOTOGRAPHER"
        )
    )).all()
    issued = 0
    for user_id in set(user_ids):
        exists = await session.scalar(select(AcademyCertificate.id).where(
            AcademyCertificate.user_id == user_id
        ))
        if exists is None and await ensure_certificate(session, user_id) is not None:
            issued += 1
    return issued


def photographer_score(*, same_hotel, bookings_today, quality, distance_minutes=0):
    """Higher is better; workload dominates so assignments remain fair."""
    return (
        (30 if same_hotel else 0)
        + min(max(quality, 0), 10) * 3
        - bookings_today * 25
        - min(max(distance_minutes, 0), 180) / 6
    )


async def ranked_photographers(session, booking):
    photographers = (
        await session.scalars(
            select(User)
            .join(UserRole, UserRole.user_id == User.id)
            .where(User.active.is_(True), UserRole.role == "PHOTOGRAPHER")
            .order_by(User.name)
        )
    ).all()
    result = []
    for user in photographers:
        day_load = await session.scalar(
            select(func.count(Booking.id)).where(
                Booking.photographer_id == user.id,
                Booking.shoot_date == booking.shoot_date,
                Booking.status.notin_(["REJECTED"]),
            )
        )
        same_hotel = bool(
            await session.scalar(
                select(HotelEmployee.id).where(
                    HotelEmployee.user_id == user.id,
                    HotelEmployee.hotel_id == booking.hotel_id,
                ).limit(1)
            )
        )
        quality = await session.scalar(
            select(func.avg(AcademyReview.quality_score)).where(
                AcademyReview.user_id == user.id
            )
        )
        score = photographer_score(
            same_hotel=same_hotel,
            bookings_today=day_load or 0,
            quality=float(quality or 5),
        )
        result.append((user, score, day_load or 0, float(quality or 5)))
    return sorted(result, key=lambda row: (-row[1], row[0].name))


async def owner_kpis(session, start, end):
    bookings = await session.scalar(
        select(func.count(Booking.id)).where(Booking.shoot_date.between(start, end))
    ) or 0
    rejected = await session.scalar(
        select(func.count(Booking.id)).where(
            Booking.shoot_date.between(start, end), Booking.status == "REJECTED"
        )
    ) or 0
    sales_total, sales_count = (
        await session.execute(
            select(func.coalesce(func.sum(Sale.amount), 0), func.count(Sale.id)).where(
                func.date(Sale.created_at).between(start, end)
            )
        )
    ).one()
    paid = await session.scalar(
        select(func.coalesce(func.sum(Receipt.verified_amount), 0)).where(
            Receipt.status == "APPROVED",
            func.date(Receipt.reviewed_at).between(start, end),
        )
    ) or 0
    conversion = ((bookings - rejected) / bookings * 100) if bookings else 0
    average_check = float(sales_total or 0) / sales_count if sales_count else 0
    return {
        "bookings": bookings,
        "rejected": rejected,
        "conversion": conversion,
        "sales_total": float(sales_total or 0),
        "sales_count": sales_count,
        "average_check": average_check,
        "paid": float(paid),
    }


async def create_repeat_sale_leads(session, now=None):
    now = now or datetime.now(UTC)
    cutoff = now.date() - timedelta(days=7)
    rows = (
        await session.scalars(
            select(Booking)
            .join(Shooting, Shooting.booking_id == Booking.id)
            .where(
                Booking.shoot_date <= cutoff,
                Shooting.status.in_(["READY_FOR_SALE", "SHOT"]),
                ~Booking.id.in_(select(RepeatSaleLead.booking_id)),
            )
        )
    ).all()
    for booking in rows:
        client = await session.get(Client, booking.client_id)
        session.add(
            RepeatSaleLead(
                booking_id=booking.id,
                manager_id=booking.manager_id,
                offer_text=(
                    f"Здравствуйте, {client.name if client else 'гость'}! "
                    "Для вашей съёмки доступны дополнительные фотографии, "
                    "печать и подарочные фотопакеты."
                ),
            )
        )
    return len(rows)


async def backup_payload(session):
    """Portable business backup; secrets and receipt images are intentionally excluded."""
    data = {"created_at": datetime.now(UTC).isoformat(), "version": 1}
    for name, model, fields in [
        ("users", User, ("id", "tg_id", "name", "username", "active")),
        ("clients", Client, ("id", "name", "phone", "notes")),
        ("bookings", Booking, ("id", "hotel_id", "client_id", "room", "guest_count", "deposit", "shoot_date", "shoot_time", "manager_id", "photographer_id", "status")),
        ("sales", Sale, ("id", "booking_id", "credited_user_id", "amount", "percent", "commission", "payment_status", "created_at")),
    ]:
        rows = (await session.scalars(select(model).order_by(model.id))).all()
        data[name] = [
            {field: str(getattr(row, field)) if isinstance(getattr(row, field), (date, datetime, time, Decimal)) else getattr(row, field) for field in fields}
            for row in rows
        ]
    return json.dumps(data, ensure_ascii=False, indent=2).encode()


async def maybe_send_daily_backup(session, bot, now=None):
    now = now or datetime.now(UTC)
    local_now = now.astimezone(ZoneInfo(config.training_timezone))
    if local_now.hour != 3:
        return 0
    key = "LAST_AUTOMATIC_BACKUP_DATE"
    saved = await session.get(Setting, key)
    today = local_now.date().isoformat()
    if saved and saved.value == today:
        return 0
    payload = await backup_payload(session)
    delivered = 0
    for tg_id in config.admin_ids:
        try:
            await bot.send_document(
                tg_id,
                BufferedInputFile(payload, filename=f"photo-boss-backup-{today}.json"),
                caption="💾 Автоматическая ежедневная резервная копия Photo Boss.",
            )
            delivered += 1
        except TelegramAPIError:
            continue
    if delivered:
        if saved is None:
            session.add(Setting(key=key, value=today))
        else:
            saved.value = today
    return delivered


def reconciliation_status(receipt_amount, bank_amount):
    return "MATCHED" if Decimal(receipt_amount) == Decimal(bank_amount) else "MISMATCH"

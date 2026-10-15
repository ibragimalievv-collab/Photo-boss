"""One photographer production workflow shared by Telegram and the Mini App."""
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from ..config import config
from ..models import Booking, Shooting, User
from .core import audit, roles_of
from .shifts import require_active_shift

TRANSITIONS = {
    'pickup': ('ASSIGNED', 'PICKED_UP', 'accepted_at'),
    'start': ('PICKED_UP', 'SHOOTING', 'started_at'),
    'finish': ('SHOOTING', 'SHOT', 'completed_at'),
    'processing': ('SHOT', 'PROCESSING', 'processing_started_at'),
    'ready': ('PROCESSING', 'READY_FOR_SALE', 'ready_for_sale_at'),
}


def validate_transition(status, booking_status, action, *, viewing_at=None):
    if action not in TRANSITIONS:
        raise ValueError('Неизвестное действие съёмки.')
    if booking_status in {'PENDING_CONFIRMATION', 'REJECTED', 'CANCELLED'}:
        raise ValueError('Съёмка ещё не подтверждена или отменена.')
    expected, target, timestamp = TRANSITIONS[action]
    if status != expected:
        raise ValueError('Этот шаг уже выполнен или предыдущий ещё не завершён. Обновите карточку.')
    if action == 'ready' and viewing_at is None:
        raise ValueError('Сначала согласуйте с гостем и сохраните время просмотра фотографий.')
    return target, timestamp


def viewing_values(value, comment, shoot_date, *, now=None, tz=None):
    """Validate a viewing appointment; store naive UTC, expose local ISO time."""
    tz = tz or ZoneInfo(config.training_timezone)
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    try:
        if not isinstance(value, str) or not isinstance(comment, str):
            raise TypeError
        viewing = datetime.fromisoformat(value)
        if viewing.tzinfo is None:
            viewing = viewing.replace(tzinfo=tz)
        viewing = viewing.astimezone(tz)
        if viewing < now.astimezone(tz) - timedelta(minutes=1):
            raise ValueError
        if isinstance(shoot_date, str):
            from datetime import date
            shoot_date = date.fromisoformat(shoot_date)
        comment = comment.strip()
        if len(comment) > 2000:
            raise ValueError
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError('Укажите предстоящее время просмотра и комментарий до 2000 символов.') from exc
    if viewing.date() > shoot_date and not comment:
        raise ValueError('При переносе продажи на другой день обязателен комментарий с причиной.')
    return viewing.astimezone(UTC).replace(tzinfo=None), comment or None


def viewing_allowed(status, booking_status):
    if status not in {'SHOT', 'PROCESSING', 'READY_FOR_SALE'} or booking_status in {'CANCELLED', 'REJECTED'}:
        raise ValueError('Назначить просмотр можно после завершения съёмки.')


def viewing_iso(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(ZoneInfo(config.training_timezone)).isoformat(timespec='minutes')


async def photographer_shooting(session, actor, shooting_id):
    """Resolve and lock only a shoot currently assigned to this active photographer."""
    from sqlalchemy import select
    if actor is not None:
        actor = await session.scalar(select(User).where(User.id == actor.id).with_for_update().execution_options(populate_existing=True))
    roles = await roles_of(session, actor) if actor else set()
    preview = await session.get(Shooting, shooting_id) if type(shooting_id) is int else None
    booking = await session.get(Booking, preview.booking_id, with_for_update=True, populate_existing=True) if preview else None
    shooting = await session.scalar(select(Shooting).where(Shooting.id == shooting_id).with_for_update().execution_options(populate_existing=True)) if preview else None
    if (actor is None or not actor.active or 'PHOTOGRAPHER' not in roles
            or booking is None or shooting is None or booking.photographer_id != actor.id):
        raise ValueError('Это не ваша съёмка.')
    return shooting, booking


async def advance_shooting(session, actor, shooting_id, action):
    shooting, booking = await photographer_shooting(session, actor, shooting_id)
    target, timestamp = validate_transition(shooting.status, booking.status, action, viewing_at=shooting.viewing_at)
    if action != 'pickup':
        await require_active_shift(session, actor.id, booking.hotel_id)
    shooting.status = booking.status = target
    setattr(shooting, timestamp, datetime.now(UTC).replace(tzinfo=None))
    await audit(session, actor, f'shooting_{action}', 'shooting', shooting.id)
    return shooting, booking


async def schedule_viewing(session, actor, shooting_id, value, comment=''):
    shooting, booking = await photographer_shooting(session, actor, shooting_id)
    viewing_allowed(shooting.status, booking.status)
    await require_active_shift(session, actor.id, booking.hotel_id)
    shooting.viewing_at, shooting.viewing_comment = viewing_values(value, comment, booking.shoot_date)
    await audit(session, actor, 'shooting_viewing_scheduled', 'shooting', shooting.id,
                f'viewing_at={shooting.viewing_at.isoformat()};comment={shooting.viewing_comment or ""}')
    return shooting, booking

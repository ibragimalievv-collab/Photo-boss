"""Shared shooting transitions; callers commit together with their replay record."""
from datetime import UTC, datetime

from sqlalchemy import select

from ..models import Booking, Shooting
from .core import audit, roles_of

TRANSITIONS = {
    'pickup': ({'ASSIGNED'}, 'PICKED_UP', 'accepted_at'),
    'start': ({'ASSIGNED', 'PICKED_UP'}, 'SHOOTING', 'started_at'),
    'finish': ({'SHOOTING'}, 'SHOT', 'completed_at'),
    'processing': ({'SHOT'}, 'PROCESSING', None),
    'ready': ({'SHOT', 'PROCESSING'}, 'READY_FOR_SALE', 'ready_for_sale_at'),
}


async def transition_shooting(session, actor, shooting_id, action, reason=''):
    preview = await session.get(Shooting, shooting_id)
    booking = await session.get(Booking, preview.booking_id, with_for_update=True) if preview else None
    roles = await roles_of(session, actor) if actor else set()
    if (not actor or not actor.active or booking is None
            or not (roles & {'OWNER', 'ADMIN'} or
                    ('PHOTOGRAPHER' in roles and booking.photographer_id == actor.id))):
        raise ValueError('Нет доступа к съёмке.')
    shooting = await session.scalar(select(Shooting).where(Shooting.id == shooting_id)
                                    .with_for_update().execution_options(populate_existing=True))
    if not isinstance(action, str) or action not in TRANSITIONS:
        raise ValueError('Неизвестное действие съёмки.')
    expected, target, timestamp = TRANSITIONS[action]
    if shooting.status not in expected or booking.status in {'CANCELLED', 'REJECTED', 'SOLD'}:
        raise ValueError('Этот шаг уже выполнен или предыдущий ещё не завершён.')
    if not isinstance(reason, str) or len(reason.strip()) > 1000:
        raise ValueError('Пояснение должно содержать не больше 1000 символов.')
    reason = reason.strip()
    if action == 'ready' and len(reason) < 3:
        raise ValueError('Объясните причину переноса в продажу (от 3 до 1000 символов).')
    shooting.status = booking.status = target
    if timestamp:
        setattr(shooting, timestamp, datetime.now(UTC).replace(tzinfo=None))
    await audit(session, actor, f'shooting_{action}', 'shooting', shooting.id, reason or None)
    return shooting

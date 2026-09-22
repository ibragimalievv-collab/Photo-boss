"""Shared shooting transitions; callers commit together with their replay record."""
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..models import AuditLog, Booking, Sale, Shooting
from .core import audit, roles_of

TRANSITIONS = {
    'pickup': ({'ASSIGNED'}, 'PICKED_UP', 'accepted_at'),
    'start': ({'ASSIGNED', 'PICKED_UP'}, 'SHOOTING', 'started_at'),
    'finish': ({'SHOOTING'}, 'SHOT', 'completed_at'),
    'processing': ({'SHOT'}, 'PROCESSING', None),
    'ready': ({'SHOT', 'PROCESSING'}, 'READY_FOR_SALE', 'ready_for_sale_at'),
}


async def sale_schedule(session, shooting_id):
    details = await session.scalar(select(AuditLog.details).where(
        AuditLog.entity == 'shooting', AuditLog.entity_id == shooting_id,
        AuditLog.action == 'sale_postponed').order_by(AuditLog.id.desc()).limit(1))
    return json.loads(details) if details else None


async def transition_shooting(session, actor, shooting_id, action, reason='', *, viewing_at=None, expected_viewing_at=None, timezone='Europe/Moscow'):
    preview = await session.get(Shooting, shooting_id)
    booking = await session.get(Booking, preview.booking_id, with_for_update=True) if preview else None
    roles = await roles_of(session, actor) if actor else set()
    if (not actor or not actor.active or booking is None
            or not (roles & {'OWNER', 'ADMIN'} or
                    ('PHOTOGRAPHER' in roles and booking.photographer_id == actor.id))):
        raise ValueError('Нет доступа к съёмке.')
    shooting = await session.scalar(select(Shooting).where(Shooting.id == shooting_id)
                                    .with_for_update().execution_options(populate_existing=True))
    if not isinstance(action, str):
        raise ValueError('Неизвестное действие съёмки.')  # noqa: TRY004 - API validation uses ValueError
    if action in {'schedule_viewing', 'postpone_sale'}:
        if shooting.status not in {'SHOT', 'PROCESSING', 'READY_FOR_SALE'} or booking.status in {'CANCELLED', 'REJECTED', 'SOLD'}:
            raise ValueError('Назначить просмотр можно после завершения съёмки и до продажи.')
        if await session.scalar(select(Sale.id).where(Sale.booking_id == booking.id).limit(1)):
            raise ValueError('Продажа уже завершена; перенос недоступен.')
        current = shooting.viewing_at
        current_key = current.isoformat() + 'Z' if current else None
        if expected_viewing_at != current_key:
            raise ValueError('Время просмотра изменилось. Обновите экран.')
        tz = ZoneInfo(str(timezone))
        try:
            proposed = datetime.fromisoformat(viewing_at)
            if proposed.tzinfo is not None:
                raise ValueError
            proposed = proposed.replace(tzinfo=tz)
        except (ValueError, TypeError):
            raise ValueError('Укажите дату и время просмотра.') from None
        if proposed <= datetime.now(UTC):
            raise ValueError('Время просмотра должно быть в будущем. Согласуйте новое время с гостем.')
        previous_day = current.replace(tzinfo=UTC).astimezone(tz).date() if current else datetime.now(tz).date()
        if action == 'postpone_sale' and proposed.date() != max(previous_day, datetime.now(tz).date()) + timedelta(days=1):
            raise ValueError('Для переноса на следующий день выберите дату следующего дня.')
        if not isinstance(reason, str) or len(reason.strip()) > 1000:
            raise ValueError('Пояснение должно содержать не больше 1000 символов.')
        if (action == 'postpone_sale' or (current and proposed.date() > previous_day)) and len(reason.strip()) < 3:
            raise ValueError('Укажите причину переноса продажи (от 3 до 1000 символов).')
        shooting.viewing_at = proposed.astimezone(UTC).replace(tzinfo=None)
        await audit(session, actor, 'sale_postponed' if current or action == 'postpone_sale' else 'viewing_scheduled',
                    'shooting', shooting.id, json.dumps({'date': proposed.date().isoformat(),
                        'viewingAt': shooting.viewing_at.isoformat() + 'Z', 'previousViewingAt': current_key,
                        'reason': reason.strip()}, ensure_ascii=False))
        return shooting
    if not isinstance(action, str) or action not in TRANSITIONS:
        raise ValueError('Неизвестное действие съёмки.')
    expected, target, timestamp = TRANSITIONS[action]
    if shooting.status not in expected or booking.status in {'CANCELLED', 'REJECTED', 'SOLD'}:
        raise ValueError('Этот шаг уже выполнен или предыдущий ещё не завершён.')
    if not isinstance(reason, str) or len(reason.strip()) > 1000:
        raise ValueError('Пояснение должно содержать не больше 1000 символов.')
    reason = reason.strip()
    shooting.status = booking.status = target
    if timestamp:
        setattr(shooting, timestamp, datetime.now(UTC).replace(tzinfo=None))
    await audit(session, actor, f'shooting_{action}', 'shooting', shooting.id, reason or None)
    return shooting

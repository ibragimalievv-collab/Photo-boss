"""Immediate, hotel-scoped review notices; never a proof of incoming money."""
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Booking, Hotel, SaleDraft, User, UserRole, utc_now


async def queue_sale_alert(session, booking, actor_id, *, event_key, reasons):
    from .push_notifications import queue_notice, scoped_supervisors
    reasons = list(dict.fromkeys(reason[:300] for reason in reasons if isinstance(reason, str) and reason))[:10]
    if not reasons:
        return
    actor = await session.get(User, actor_id)
    hotel = await session.get(Hotel, booking.hotel_id)
    description = (f'Нужна проверка продажи. Запись №{booking.id}.\n'
                   f'Отель: {hotel.name if hotel else "не указан"}.\n'
                   f'Сотрудник: {actor.name if actor else "не указан"}.\n'
                   + '\n'.join(reasons)
                   + '\nАвтоматическая проверка не подтверждает поступление денег.')
    for uid in sorted(await scoped_supervisors(session, booking.hotel_id)):
        recipient = await session.get(User, uid)
        if recipient and recipient.active:
            await queue_notice(session, uid, kind='operational_alert', event_key=event_key,
                url='/app/#finance', message=description,
                payload={'bookingId': booking.id, 'hotelId': booking.hotel_id,
                         'reasons': reasons, 'description': description})


async def queue_receipt_review(session, booking, actor_id, *, analysis, expected,
                               event_key):
    from .services.receipts import warnings_for
    if not isinstance(analysis, dict) or analysis.get('status') != 'extracted':
        reasons = ['Автоматический разбор чека недоступен. Нужна ручная проверка.']
    else:
        fields = analysis.get('fields')
        if not isinstance(fields, dict):
            reasons = ['Данные чека не удалось проверить. Нужна ручная проверка.']
        else:
            # The amount is known after counts are accepted. Do not compare
            # an early draft against zero and manufacture a mismatch.
            reasons = warnings_for(fields, expected if expected is not None else fields.get('amount'), utc_now().date())
            concerns = fields.get('concerns', [])
            if isinstance(concerns, list):
                reasons += ['Замечание распознавания: ' + item[:240] for item in concerns if isinstance(item, str)]
    await queue_sale_alert(session, booking, actor_id, event_key=event_key, reasons=reasons)


async def alert_rejected_operation(api, actor_data, body, error):
    """A rejected transaction rolls back; persist its alert in a new one.

    Only exact known duplicate-evidence failures qualify. Re-read identity,
    ownership and hotel scope rather than trusting failed request metadata.
    Alerting is best effort at the caller and must retain the original error.
    """
    messages = {
        'Этот чек уже использовался или похож на ранее загруженный.': 'Повторно использован чек или его копия. Продажа с этим чеком не подтверждена.',
        'Этот кадр уже куплен в предыдущей продаже. Выберите новый кадр для допродажи.': 'Повторно выбран ранее проданный кадр. Проверьте допродажу.',
    }
    reason = messages.get(str(error))
    if (not reason or not isinstance(body, dict) or body.get('kind') not in {'sale_receipt', 'sale_selected', 'sale_disk_selected'}
            or not isinstance(body.get('key'), str) or not 16 <= len(body['key']) <= 80
            or body.get('actorId') != actor_data.get('id') or not isinstance(body.get('data'), dict)):
        return
    from .miniapp_security import AccessError
    from .operations_guard import require_generation
    from .services.booking_access import can_access_booking
    from .workflow import target
    async with AsyncSession(api.engine, expire_on_commit=False) as session:
        actor = await session.get(User, actor_data['id'], with_for_update={'key_share': True})
        if not actor or not actor.active:
            return
        roles = set(await session.scalars(select(UserRole.role).where(UserRole.user_id == actor.id)))
        conn = await session.connection()
        await require_generation(conn, body.get('generation', ''))
        from .models import Setting
        generation = await session.get(Setting, 'sales:generation')
        if generation and generation.value and body.get('salesGeneration', '') != generation.value:
            return
        try:
            draft_id = await target(session, actor, body['data'].get('draft'), 'draftId')
        except (AccessError, TypeError, ValueError):
            return
        draft = await session.get(SaleDraft, draft_id)
        booking = await session.get(Booking, draft.booking_id) if draft else None
        if (not draft or draft.created_by_id != actor.id or not booking
                or not await can_access_booking(session, actor.id, roles, booking)):
            return
        identity = json.dumps([actor.id, body['key'], body['kind'], reason], ensure_ascii=False)
        event = 'operation-alert:' + hashlib.sha256(identity.encode()).hexdigest()
        await queue_sale_alert(session, booking, actor.id, event_key=event, reasons=[reason])
        await session.commit()

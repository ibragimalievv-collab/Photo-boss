import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import func, select
from test_workflow_services import booking_data, fixture

from app.miniapp_security import AccessError
from app.models import (
    Booking,
    Hotel,
    HotelEmployee,
    Notification,
    SaleDraft,
    User,
    UserRole,
)
from app.operational_alerts import alert_rejected_operation, queue_receipt_review
from app.push_notifications import eligible, payload_for
from app.services.bookings import create_booking_record


async def setup():
    engine, factory = await fixture()
    async with factory() as session:
        session.add_all([User(id=3, tg_id=3, name='Owner'), UserRole(user_id=3, role='OWNER'),
            User(id=4, tg_id=4, name='Scoped admin'), UserRole(user_id=4, role='ADMIN'),
            HotelEmployee(user_id=4, hotel_id=1),
            User(id=5, tg_id=5, name='Other admin'), UserRole(user_id=5, role='ADMIN'),
            Hotel(id=2, name='Other hotel'), HotelEmployee(user_id=5, hotel_id=2)])
        booking = await create_booking_record(session, await session.get(User, 1), booking_data())
        booking.photographer_id = 2
        booking.status = 'READY_FOR_SALE'
        draft = SaleDraft(booking_id=booking.id, created_by_id=1, status='AWAITING_RECEIPT')
        session.add(draft)
        await session.flush()
        bid, did = booking.id, draft.id
        await session.commit()
    return engine, factory, bid, did


def extraction(amount='400'):
    return {'status': 'extracted', 'fields': {'is_receipt': True, 'amount': amount,
        'currency': 'RUB', 'date': str(datetime.now(UTC).date()), 'recipient': 'Test recipient',
        'operation_id': 'test-operation', 'concerns': []}}


def test_mismatched_receipt_immediately_notifies_owner_and_scoped_admin_once():
    async def run():
        engine, factory, bid, _ = await setup()
        try:
            async with factory() as session:
                booking = await session.get(Booking, bid)
                for _ in range(2):
                    await queue_receipt_review(session, booking, 1, analysis=extraction('399'),
                        expected=400, event_key='receipt-review:test')
                await session.commit()
            async with factory() as session:
                notices = list(await session.scalars(select(Notification).where(Notification.kind == 'operational_alert')))
                assert {n.user_id for n in notices} == {3, 4} and len(notices) == 2
                notice = next(n for n in notices if n.user_id == 4)
                assert 'Сумма в чеке отличается' in notice.text
                assert 'не подтверждает поступление денег' in notice.text
                assert payload_for(notice)['title'] == 'Photo Boss · Проверьте продажу'
                assert payload_for(notice)['url'] == '/app/#finance'
                sub = SimpleNamespace(active=True, user_id=4)
                assert await eligible(session, notice, sub)
                membership = await session.scalar(select(HotelEmployee).where(HotelEmployee.user_id == 4))
                await session.delete(membership)
                await session.commit()
                assert not await eligible(session, notice, sub)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_valid_receipt_and_early_draft_do_not_manufacture_amount_warning():
    async def run():
        engine, factory, bid, _ = await setup()
        try:
            async with factory() as session:
                booking = await session.get(Booking, bid)
                await queue_receipt_review(session, booking, 1, analysis=extraction(), expected=400, event_key='valid')
                await queue_receipt_review(session, booking, 1, analysis=extraction(), expected=None, event_key='draft')
                assert await session.scalar(select(func.count(Notification.id))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_failed_save_rolls_back_its_receipt_notice():
    async def run():
        engine, factory, bid, _ = await setup()
        try:
            async with factory() as session:
                await queue_receipt_review(session, await session.get(Booking, bid), 1,
                    analysis={'status': 'unavailable'}, expected=400, event_key='rolled-back')
                await session.rollback()
            async with factory() as session:
                assert await session.scalar(select(func.count(Notification.id))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_rejected_duplicate_alert_survives_rejection_and_deduplicates_retries():
    async def run():
        engine, factory, _, did = await setup()
        try:
            body = {'key': 'duplicate-receipt-operation', 'kind': 'sale_receipt', 'actorId': 1,
                    'data': {'draft': {'id': did}}}
            error = AccessError('Этот чек уже использовался или похож на ранее загруженный.', 409)
            for _ in range(2):
                await alert_rejected_operation(SimpleNamespace(engine=engine), {'id': 1}, body, error)
            async with factory() as session:
                notices = list(await session.scalars(select(Notification)))
                assert len(notices) == 2 and {n.user_id for n in notices} == {3, 4}
                assert all('Повторно использован чек' in n.text for n in notices)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_rejected_alert_rechecks_ownership_active_account_and_known_reason():
    async def run():
        engine, factory, _, did = await setup()
        try:
            body = {'key': 'duplicate-receipt-operation', 'kind': 'sale_receipt', 'actorId': 2,
                    'data': {'draft': {'id': did}}}
            error = AccessError('Этот чек уже использовался или похож на ранее загруженный.', 409)
            api = SimpleNamespace(engine=engine)
            await alert_rejected_operation(api, {'id': 2}, body, error)
            body['actorId'] = 1
            await alert_rejected_operation(api, {'id': 1}, body, AccessError('Проверьте данные.', 409))
            async with factory() as session:
                (await session.get(User, 1)).active = False
                await session.commit()
            await alert_rejected_operation(api, {'id': 1}, body, error)
            async with factory() as session:
                assert await session.scalar(select(func.count(Notification.id))) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())

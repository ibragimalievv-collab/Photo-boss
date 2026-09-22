import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_workflow_replay import setup
from test_workflow_services import booking_data, fixture

from app.miniapp_security import AccessError
from app.models import (
    AuditLog,
    Photo,
    Sale,
    SaleDraftPhoto,
    Shooting,
    User,
    UserRole,
)
from app.services.bookings import create_booking_record
from app.services.sale_workflow import (
    capture_draft_receipt,
    complete_full_upload,
    complete_sale,
    full_upload_deadline,
    set_sale_counts,
    start_sale_draft,
)
from app.services.shooting_workflow import transition_shooting


def test_photographer_lifecycle_sale_then_upload_and_salary():
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                photographer = await session.get(User, 2)
                manager = await session.get(User, 1)
                booking = await create_booking_record(session, manager, booking_data(), None)
                booking.photographer_id = photographer.id
                shooting = await session.scalar(select(Shooting))
                shooting.status = booking.status = 'ASSIGNED'
                await session.flush()
                for actor in [manager, User(id=99, active=True)]:
                    with pytest.raises(ValueError, match='Нет доступа'):
                        await transition_shooting(session, actor, shooting.id, 'start')
                with pytest.raises(ValueError, match='предыдущий'):
                    await transition_shooting(session, photographer, shooting.id, 'ready', 'Материалы готовы')
                for action in ['start', 'finish', 'processing']:
                    await transition_shooting(session, photographer, shooting.id, action)
                await transition_shooting(session, photographer, shooting.id, 'ready')
                assert await session.scalar(select(AuditLog.details).where(AuditLog.action == 'shooting_ready')) is None
                assert await full_upload_deadline(session, booking.id) is None
                with pytest.raises(ValueError, match='завершите продажу'):
                    await complete_full_upload(session, photographer, shooting.id)
                draft = await start_sale_draft(session, photographer, {'PHOTOGRAPHER'}, booking.id)
                await capture_draft_receipt(session, photographer, draft, 'test-receipt', 'unique', 'digest', {})
                await set_sale_counts(session, photographer, draft, 150, 1)
                with pytest.raises(ValueError, match='выбранные'):
                    await complete_sale(session, photographer, draft.id)
                session.add(SaleDraftPhoto(draft_id=draft.id, telegram_file_id='selected',
                    telegram_unique_id='selected', storage_path='app:/test.jpg', sha256='selected', byte_size=100))
                await session.flush()
                sale = (await complete_sale(session, photographer, draft.id)).sale
                await session.commit()
                assert sale.commission == 0 and sale.commission_finalized_at is None
                due = await full_upload_deadline(session, booking.id)
                assert due == sale.created_at + timedelta(hours=48)
                # Reopen from persisted state; the deadline must survive a new session.
                bid, sid, did = booking.id, shooting.id, draft.id
            async with factory() as session:
                photographer = await session.get(User, 2)
                assert await full_upload_deadline(session, bid) == due
                with pytest.raises(ValueError, match='завершена'):
                    await complete_sale(session, photographer, did)
                session.add_all(Photo(shooting_id=sid, file_id=f'photo-{i}') for i in range(149))
                await session.flush()
                with pytest.raises(ValueError, match='оставшиеся'):
                    await complete_full_upload(session, photographer, sid)
                session.add(Photo(shooting_id=sid, file_id='photo-150'))
                await session.flush()
                result = await complete_full_upload(session, photographer, sid)
                assert result.commissions['percent'] == 15
                assert (await session.scalar(select(Sale))).commission == 60
                with pytest.raises(ValueError, match='закрыта'):
                    await complete_full_upload(session, photographer, sid)
                assert await session.scalar(select(func.count(Sale.id))) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_transitions_lost_response_replay_and_revoked_role():
    async def run():
        engine, factory, work = await setup()
        try:
            async with factory() as session:
                booking = await create_booking_record(session, await session.get(User, 1), booking_data())
                booking.photographer_id = 2
                shooting = await session.scalar(select(Shooting))
                booking.status = shooting.status = 'ASSIGNED'
                await session.commit()
                sid = shooting.id
            actor = {'id': 2, 'roles': ['PHOTOGRAPHER']}
            def packet(key, action):
                return {'actorId': 2, 'key': 'shooting-request-' + key, 'kind': 'shoot_transition',
                        'date': '2026-09-22', 'data': {'shooting': {'id': sid}, 'action': action, 'reason': ''}}
            body = packet('start', 'start')
            first = json.loads((await work.execute(actor, body)).text)
            assert json.loads((await work.execute(actor, body)).text) == first
            with pytest.raises(AccessError):
                await work.execute(actor, packet('double-tap', 'start'))
            async with factory() as session:
                assert await session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == 'shooting_start')) == 1
                role = await session.scalar(select(UserRole).where(UserRole.user_id == 2))
                role.role = 'MANAGER'
                await session.commit()
            with pytest.raises(AccessError, match='Нет доступа'):
                await work.execute(actor, packet('finish', 'finish'))
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('role', ['OWNER', 'ADMIN'])
def test_management_can_transition_and_unassigned_photographer_cannot(role):
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                actor = await session.get(User, 1)
                session.add(UserRole(user_id=1, role=role))
                booking = await create_booking_record(session, actor, booking_data())
                shooting = await session.scalar(select(Shooting))
                shooting.status = booking.status = 'ASSIGNED'
                await session.flush()
                with pytest.raises(ValueError, match='Нет доступа'):
                    await transition_shooting(session, await session.get(User, 2), shooting.id, 'start')
                assert (await transition_shooting(session, actor, shooting.id, 'start')).status == 'SHOOTING'
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_listing_filters_by_current_role_and_preserves_overdue_upload():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.workflow import Workflow

    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                booking = await create_booking_record(session, await session.get(User, 1), booking_data())
                # Former booking manager now has only the PHOTOGRAPHER role.
                booking.manager_id = 2
                booking.photographer_id = 1
                booking.status = 'READY_FOR_SALE'
                shooting = await session.scalar(select(Shooting))
                shooting.status = 'READY_FOR_SALE'
                session.add(Sale(booking_id=booking.id, created_by_id=1, credited_user_id=1,
                    sold_photos=1, amount=400, created_at=datetime.now(UTC).replace(tzinfo=None)-timedelta(hours=49)))
                await session.commit()
            workflow = Workflow(SimpleNamespace(engine=engine, today=lambda: datetime.now(UTC).date()))
            data = json.loads((await workflow.listing({'miniapp_actor': {'id': 2, 'roles': ['PHOTOGRAPHER']}})).text)
            assert data['bookings'] == []
            data = json.loads((await workflow.listing({'miniapp_actor': {'id': 1, 'roles': ['OWNER']}})).text)
            assert data['bookings'][0]['uploadOverdue']
            assert data['bookings'][0]['canUpload']
            assert data['bookings'][0]['saleCompleted']
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_viewing_time_postponement_reason_replay_and_access():
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from app.models import Booking
    from app.workflow import Workflow

    async def run():
        engine, factory, work = await setup()
        tz = ZoneInfo('Europe/Moscow')
        first = (datetime.now(tz) + timedelta(hours=2)).replace(second=0, microsecond=0)
        second = first + timedelta(days=1)
        try:
            async with factory() as session:
                booking = await create_booking_record(session, await session.get(User, 1), booking_data())
                booking.photographer_id = 2
                shooting = await session.scalar(select(Shooting))
                shooting.status = booking.status = 'SHOT'
                await session.commit()
                sid, bid = shooting.id, booking.id
            actor = {'id': 2, 'roles': ['PHOTOGRAPHER']}
            def packet(key, action, when, expected=None, reason=''):
                return {'actorId': 2, 'key': 'viewing-request-' + key, 'kind': 'shoot_transition',
                        'date': str(work.api.today()), 'data': {'shooting': {'id': sid}, 'action': action,
                        'reason': reason, 'viewingAt': when.replace(tzinfo=None).isoformat(), 'expectedViewingAt': expected}}
            initial = packet('initial', 'schedule_viewing', first)
            response = json.loads((await work.execute(actor, initial)).text)
            assert json.loads((await work.execute(actor, initial)).text) == response
            expected = first.astimezone(UTC).replace(tzinfo=None).isoformat() + 'Z'
            with pytest.raises(AccessError, match='изменилось'):
                await work.execute(actor, packet('double-tap', 'schedule_viewing', first))
            for action in ['postpone_sale', 'schedule_viewing']:
                with pytest.raises(AccessError, match='причину'):
                    await work.execute(actor, packet('no-reason-'+action, action, second, expected, '  '))
            deferred = packet('defer', 'postpone_sale', second, expected, 'Гость попросил прийти завтра')
            saved = json.loads((await work.execute(actor, deferred)).text)
            assert json.loads((await work.execute(actor, deferred)).text) == saved
            listing = json.loads((await Workflow(work.api).listing({'miniapp_actor': actor})).text)
            assert listing['bookings'][0]['viewingAt'] == second.astimezone(UTC).replace(tzinfo=None).isoformat()+'Z'
            assert listing['bookings'][0]['saleSchedule']['reason'] == 'Гость попросил прийти завтра'
            async with factory() as session:
                assert (await session.get(Booking, bid)).status == 'SHOT'
                assert await session.scalar(select(func.count(Sale.id))) == 0
                assert await session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == 'sale_postponed')) == 1
                with pytest.raises(ValueError, match='Нет доступа'):
                    await transition_shooting(session, await session.get(User, 1), sid, 'schedule_viewing')
                shooting = await session.get(Shooting, sid)
                shooting.viewing_at = datetime.now(UTC).replace(tzinfo=None)-timedelta(minutes=1)
                await session.commit()
            listing = json.loads((await Workflow(work.api).listing({'miniapp_actor': actor})).text)
            assert listing['bookings'][0]['viewingOverdue']
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_viewing_migration_is_additive_and_repeatable():
    from sqlalchemy import text

    from app.schema_updates import upgrade

    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                await create_booking_record(session, await session.get(User, 1), booking_data())
                await session.commit()
            async with engine.begin() as conn:
                await conn.execute(text('ALTER TABLE shootings DROP COLUMN viewing_at'))
                await upgrade(conn)
                await upgrade(conn)
                assert (await conn.execute(text('SELECT viewing_at FROM shootings'))).one() == (None,)
        finally:
            await engine.dispose()
    asyncio.run(run())

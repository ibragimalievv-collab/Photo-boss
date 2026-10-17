import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_team import OWNER, Request, setup
from test_workflow_services import booking_data, fixture

from app import booking_control, development, voice_training
from app.audit_labels import presentation
from app.booking_control import (
    BookingControl,
    activity,
    activity_tick,
    advisor,
    telegram_text,
)
from app.booking_management import BookingManagement, snapshot, version
from app.miniapp_security import AccessError
from app.models import (
    Booking,
    Client,
    Hotel,
    HotelEmployee,
    Notification,
    Setting,
    Shift,
    ShiftCheckIn,
    ShiftCheckOut,
    Shooting,
    User,
    UserRole,
)
from app.services.bookings import create_booking_record


def test_booking_edit_conflict_permissions_trash_and_restore():
    async def run():
        engine, factory = await fixture()
        api = SimpleNamespace(engine=engine, today=lambda: date(2026, 9, 21), body=AsyncMock(side_effect=lambda r: r.body))
        service = BookingManagement(api)
        manager = {'id': 1, 'roles': ['MANAGER']}
        async with factory() as session:
            booking = await create_booking_record(session, await session.get(User, 1), booking_data())
            await session.commit()
            bid = booking.id
        async def revision():
            async with factory() as session:
                b = await session.get(Booking, bid)
                return version(snapshot(b, await session.get(Client, b.client_id),
                    await session.scalar(select(Shooting).where(Shooting.booking_id == bid))))
        try:
            before = await revision()
            edit = {'mode': 'edit', 'revision': before, 'reason': 'Гость попросил перенос',
                    'client': 'Guest Updated', 'phone': '+79991234567', 'room': '202', 'guestCount': 3,
                    'date': '2026-09-23', 'time': '14:30'}
            with pytest.raises(AccessError):
                await service.manage(Request({'id': 2, 'roles': ['PHOTOGRAPHER']}, edit, bid))
            await service.manage(Request(manager, edit, bid))
            with pytest.raises(AccessError) as stale:
                await service.manage(Request(manager, edit, bid))
            assert stale.value.status == 409
            await service.manage(Request(manager, {'mode': 'trash', 'revision': await revision(), 'reason': 'Гость отменил'}, bid))
            async with factory() as session:
                assert (await session.get(Booking, bid)).status == 'CANCELLED'
                assert await session.get(Setting, f'booking:trash:{bid}')
            await service.manage(Request(manager, {'mode': 'restore', 'revision': await revision(), 'reason': 'Гость вернулся'}, bid))
            async with factory() as session:
                assert await session.get(Setting, f'booking:trash:{bid}') is None
                b = await session.get(Booking, bid)
                assert b.shoot_date == date(2026, 9, 23) and (await session.get(Client, b.client_id)).phone == '+79991234567'
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_idle_reminder_requires_confirmed_shift_is_throttled_and_resolves():
    async def run():
        engine, factory = await fixture()
        now = datetime(2026, 9, 22, 8, tzinfo=UTC)
        bot = SimpleNamespace(send_message=AsyncMock())
        try:
            async with factory() as session:
                session.add(Shift(user_id=1, hotel_id=1, start_at=now.replace(tzinfo=None)-timedelta(hours=2),
                                  end_at=now.replace(tzinfo=None)+timedelta(hours=2)))
                await session.commit()
                assert await activity_tick(session, bot, now) == 0
                session.add(ShiftCheckIn(user_id=1, shift_date=now.date(), status='STARTED',
                                         started_at=now.replace(tzinfo=None)-timedelta(hours=2)))
                await session.commit()
                assert await activity_tick(session, bot, now) == 1
                await session.commit()
                assert await activity_tick(session, bot, now) == 0
                assert bot.send_message.await_count == 1
                scoped = await activity(session, now=now, hotel_ids=set())
                assert scoped['hotels'] == [] and scoped['managers'] == []
                session.add(ShiftCheckOut(user_id=1, shift_date=now.date(), status='FINISHED', ended_at=now.replace(tzinfo=None)))
                await session.flush()
                await activity_tick(session, bot, now)
                await session.commit()
                rows = list(await session.scalars(select(Notification).where(Notification.kind == 'manager_idle')))
                assert rows and all(n.resolved_at for n in rows)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_booking_reminders_show_scoped_hotels_and_confirmed_manager_names(monkeypatch):
    monkeypatch.setattr(booking_control, 'config', SimpleNamespace(training_timezone='Europe/Moscow', openai_api_key=None))
    async def run():
        engine, factory = await fixture()
        now = datetime(2026, 9, 22, 8, tzinfo=UTC)
        bot = SimpleNamespace(send_message=AsyncMock())
        async def current_activity(session, **kwargs):
            kwargs.setdefault('now', now)
            return await activity(session, **kwargs)
        monkeypatch.setattr(booking_control, 'activity', current_activity)
        try:
            async with factory() as session:
                (await session.get(Hotel, 1)).name = 'Бетон'
                (await session.get(User, 1)).name = 'Алина'
                session.add_all([
                    Hotel(id=2, name='Другой отель'), Hotel(id=3, name='Без менеджера'),
                    User(id=3, tg_id=3, name='Владелец'), UserRole(user_id=3, role='OWNER'),
                    User(id=4, tg_id=4, name='Администратор'), UserRole(user_id=4, role='ADMIN'),
                    HotelEmployee(user_id=4, hotel_id=1),
                    User(id=5, tg_id=5, name='Варвара'), UserRole(user_id=5, role='MANAGER'),
                    User(id=7, tg_id=7, name='Лиза'), UserRole(user_id=7, role='MANAGER'),
                    User(id=8, tg_id=8, name='Не вышел'), UserRole(user_id=8, role='MANAGER'),
                    User(id=9, tg_id=9, name='Закончил'), UserRole(user_id=9, role='MANAGER'),
                ])
                for uid, hid in [(1, 1), (2, 1), (5, 2), (7, 1), (8, 1), (9, 1)]:
                    session.add(Shift(user_id=uid, hotel_id=hid, start_at=now.replace(tzinfo=None)-timedelta(hours=2),
                                      end_at=now.replace(tzinfo=None)+timedelta(hours=3)))
                    if uid != 8:
                        session.add(ShiftCheckIn(user_id=uid, shift_date=now.date(), status='STARTED',
                                                started_at=now.replace(tzinfo=None)-timedelta(hours=2)))
                session.add(ShiftCheckOut(user_id=9, shift_date=now.date(), status='FINISHED', ended_at=now.replace(tzinfo=None)))
                await session.commit()
                assert await activity_tick(session, bot, now) == 5
                await session.commit()
                messages = {call.args[0]: call.args[1] for call in bot.send_message.await_args_list}
                assert 'Бетон' in messages[1] and 'Алина, Лиза' in messages[1] and 'Пройдите по отелю' in messages[1]
                assert 'Другой отель' not in messages[1] and 'Варвара' not in messages[1]
                assert 'Бетон' in messages[4] and 'Алина, Лиза' in messages[4]
                assert 'Другой отель' not in messages[4] and 'Варвара' not in messages[4] and 'Без менеджера' not in messages[4]
                assert all(name in messages[3] for name in ['Бетон', 'Другой отель', 'Варвара', 'Без менеджера', 'нет менеджеров'])
                assert 'Не вышел' not in messages[3] and 'Закончил' not in messages[3]
                assert all(call.kwargs['parse_mode'] is None for call in bot.send_message.await_args_list)

                service = BookingControl(SimpleNamespace(engine=engine))
                admin_data = json.loads((await service.listing(Request({'id': 4, 'roles': ['ADMIN']}))).text)
                assert [h['name'] for h in admin_data['zeroBookingHotels']] == ['Бетон']
                assert admin_data['hotels'][0]['managerNames'] == ['Алина', 'Лиза']
                manager_data = json.loads((await service.listing(Request({'id': 1, 'roles': ['MANAGER']}))).text)
                assert 'hotels' not in manager_data and 'zeroBookingHotels' not in manager_data

                # The live app and hourly advice cache must reflect the booking immediately,
                # while Telegram remains throttled despite changed statistics.
                cached = await session.get(Setting, 'booking-control:advice:4')
                before = json.loads(cached.value)['fingerprint']
                booking = await create_booking_record(session, await session.get(User, 1), booking_data())
                booking.created_at = now.replace(tzinfo=None)
                await session.commit()
                later = now + timedelta(minutes=1)
                assert await activity_tick(session, bot, later) == 0
                await session.commit()
                cached = await session.get(Setting, 'booking-control:advice:4')
                assert json.loads(cached.value)['fingerprint'] != before
                current = await session.scalar(select(Notification).where(Notification.user_id == 4,
                    Notification.kind == 'manager_advice', Notification.resolved_at.is_(None)))
                assert 'Во всех доступных отелях сегодня есть новые записи.' in current.text
                active = list(await session.scalars(select(Notification).where(Notification.user_id == 1,
                    Notification.kind == 'manager_idle', Notification.resolved_at.is_(None))))
                assert active == []
                assert bot.send_message.await_count == 5

                # Ending the remaining managers' shifts clears supervisor alerts too.
                for uid in (1, 5, 7):
                    session.add(ShiftCheckOut(user_id=uid, shift_date=now.date(), status='FINISHED', ended_at=later.replace(tzinfo=None)))
                await session.flush()
                assert await activity_tick(session, bot, later) == 0
                await session.commit()
                assert not list(await session.scalars(select(Notification).where(Notification.kind.in_(['manager_idle', 'manager_advice']),
                    Notification.resolved_at.is_(None))))
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_reminders_throttle_from_successful_delivery_and_disable_clears_alerts():
    async def run():
        engine, factory = await fixture()
        now = datetime(2026, 9, 22, 8, tzinfo=UTC)
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=[TimeoutError(), None, None]))
        try:
            async with factory() as session:
                # First tick just before an old slot boundary; retry succeeds much later.
                session.add(Shift(user_id=1, hotel_id=1, start_at=now.replace(tzinfo=None)-timedelta(minutes=119),
                                  end_at=now.replace(tzinfo=None)+timedelta(hours=5)))
                session.add(ShiftCheckIn(user_id=1, shift_date=now.date(), status='STARTED',
                                         started_at=now.replace(tzinfo=None)-timedelta(minutes=119)))
                await session.commit()
                assert await activity_tick(session, bot, now) == 0
                await session.commit()
                assert await activity_tick(session, bot, now+timedelta(minutes=55)) == 1
                await session.commit()
                assert await activity_tick(session, bot, now+timedelta(minutes=60)) == 0
                assert await activity_tick(session, bot, now+timedelta(minutes=114)) == 0
                assert await activity_tick(session, bot, now+timedelta(minutes=115)) == 1
                await session.commit()
                assert bot.send_message.await_count == 3
                session.add(Setting(key='booking-control:settings', value=json.dumps({'enabled': False, 'idleMinutes': 60})))
                await session.commit()
                assert await activity_tick(session, bot, now+timedelta(minutes=116)) == 0
                await session.commit()
                assert not list(await session.scalars(select(Notification).where(Notification.kind == 'manager_idle', Notification.resolved_at.is_(None))))
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_advisor_refreshes_team_changes_and_telegram_text_is_bounded(monkeypatch):
    monkeypatch.setattr(booking_control, 'config', SimpleNamespace(openai_api_key=None))
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                now = datetime(2026, 9, 22, 8, tzinfo=UTC)
                first = {'date': '2026-09-22', 'hotels': [{'id': 1, 'managerNames': ['Алина']}], 'advice': 'Первый совет', 'choices': []}
                saved = await advisor(session, 1, first, now)
                changed = {**first, 'hotels': [{'id': 1, 'managerNames': ['Лиза']}], 'advice': 'Обновлённый состав'}
                fresh = await advisor(session, 1, changed, now+timedelta(minutes=1))
                assert fresh['text'] == 'Обновлённый состав' and fresh['fingerprint'] != saved['fingerprint']
        finally:
            await engine.dispose()
    asyncio.run(run())
    message = 'Отель 🌴 и менеджеры\n' * 800
    limited = telegram_text(message)
    assert len(limited.encode('utf-16-le')) <= 8192
    assert limited.endswith('«Записи и помощь менеджерам».')


def test_voice_ownership_cache_and_replayed_audio_never_duplicates_a_turn(monkeypatch):
    class Part:
        def __init__(self, name, value):
            self.name, self.value = name, value
            self.headers = {'Content-Type': 'audio/webm'}
        async def read_chunk(self, _):
            value, self.value = self.value, b''
            return value
    class Reader:
        def __aiter__(self):
            async def parts():
                yield Part('revision', b'1')
                yield Part('audio', b'fixture-audio')
            return parts()
    async def run():
        engine, _, team = await setup()
        monkeypatch.setattr(development, 'config', SimpleNamespace(openai_api_key='test-only'))
        service = development.Development(team.api)
        voice = voice_training.VoiceTraining(service)
        transcribe = AsyncMock(return_value='Один кадр стоит четыреста рублей')
        speak = AsyncMock(return_value=b'fixture-mp3')
        monkeypatch.setattr(voice_training, 'transcribe', transcribe)
        monkeypatch.setattr(voice_training, 'synthesize', speak)
        monkeypatch.setattr(development.ai, 'roleplay', AsyncMock(return_value={
            'status': 'completed', 'data': {'reply': 'Хорошо, покажите фотографии.', 'score': None, 'errors': [], 'recommendations': []}}))
        try:
            sid = json.loads((await service.start(Request(OWNER, {'clientType': 'budget'}))).text)['id']
            with pytest.raises(AccessError):
                await voice.speech(Request({'id': 2, 'roles': ['PHOTOGRAPHER']}, {}, sid))
            assert speak.await_count == 0
            await voice.speech(Request(OWNER, {}, sid))
            await voice.speech(Request(OWNER, {}, sid))
            assert speak.await_count == 1
            r = Request(OWNER, {}, sid)
            r.multipart = AsyncMock(return_value=Reader())
            await voice.voice(r)
            replay = json.loads((await voice.voice(r)).text)
            assert replay['replayed'] and transcribe.await_count == 1
            assert development.ai.roleplay.await_count == 1
            current = await voice.session(OWNER, sid)
            assert current['revision'] == 2 and len(json.loads(current['transcript'])) == 3
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_audit_russian_fields_and_secrets_are_not_disclosed():
    data = presentation('row.update', 'bookings', json.dumps({'before': {'status': 'ASSIGNED', 'password_hash': 'sensitive'},
        'after': {'status': 'CANCELLED', 'password_hash': 'other-sensitive'}, 'reason': 'Гость отказался'}), {})
    assert data['changes'] == [{'label': 'Состояние', 'field': 'status', 'before': 'Назначено', 'after': 'Отменено'}]
    assert 'sensitive' not in json.dumps(data)
    assert data['facts'] == [{'label': 'Причина', 'value': 'Гость отказался'}]

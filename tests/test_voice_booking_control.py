import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_team import OWNER, Request, setup
from test_workflow_services import booking_data, fixture

from app import development, voice_training
from app.audit_labels import presentation
from app.booking_control import activity, activity_tick
from app.booking_management import BookingManagement, snapshot, version
from app.miniapp_security import AccessError
from app.models import (
    Booking,
    Client,
    Notification,
    Setting,
    Shift,
    ShiftCheckIn,
    ShiftCheckOut,
    Shooting,
    User,
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

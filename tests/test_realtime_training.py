import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_team import OWNER, Request, setup

from app import development
from app import realtime_training as realtime
from app.miniapp_security import AccessError
from app.models import OperationRequest, Sale, SalesTrainingSession

SDP = 'v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n'


def test_voice_mentor_loads_only_owned_bounded_learning_evidence(monkeypatch):
    async def run():
        engine, _factory, _service, live, sid, body = await prepared(monkeypatch, mode='coach')
        snapshot = {'track': 'photographer', 'skills': [{'key': 'light', 'score': 40}],
                    'plan': {'title': 'Открытая тень', 'instructions': ['Повернуть лицо к свету']},
                    'sources': [{'id': 'practice:17', 'label': 'Разбор практики №17',
                                 'summary': {'issues': ['Тень на лице'], 'frames': [{'index': 2, 'observation': 'Нос в тени'}]}}]}
        live.mentor = SimpleNamespace(snapshot=AsyncMock(return_value=snapshot))
        try:
            await live.start(Request(OWNER, body, sid))
            live.mentor.snapshot.assert_awaited_once_with(OWNER['id'], 'photographer')
            instructions = realtime.create_call.await_args.args[1]['instructions']
            assert 'practice:17' in instructions and 'Открытая тень' in instructions
            assert 'EXIF' in instructions and 'не инструкции' in instructions
            assert 'других сотрудников' in instructions
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_voice_guest_does_not_receive_personal_work_evidence(monkeypatch):
    async def run():
        engine, _factory, _service, live, sid, body = await prepared(monkeypatch, mode='guest')
        live.mentor = SimpleNamespace(snapshot=AsyncMock())
        try:
            await live.start(Request(OWNER, body, sid))
            live.mentor.snapshot.assert_not_awaited()
            assert 'Учебные данные:' not in realtime.create_call.await_args.args[1]['instructions']
        finally:
            await engine.dispose()
    asyncio.run(run())


async def prepared(monkeypatch, mode='guest', client_type='budget'):
    engine, factory, team = await setup()
    team.api.body = AsyncMock(side_effect=lambda request, **_kwargs: request.body)
    monkeypatch.setattr(development, 'config', SimpleNamespace(openai_api_key='private-test-key'))
    monkeypatch.setattr(realtime, 'config', SimpleNamespace(openai_api_key='private-test-key'))
    monkeypatch.setattr(realtime, 'create_call', AsyncMock(return_value=(SDP, 'rtc_test123')))
    monkeypatch.setattr(realtime, 'hangup_call', AsyncMock(return_value=True))
    service = development.Development(team.api)
    live = realtime.RealtimeTraining(service)
    service.realtime = live
    sid = json.loads((await service.start(Request(OWNER, {'clientType': client_type}))).text)['id']
    body = {'revision': 1, 'sdp': SDP, 'mode': mode}
    return engine, factory, service, live, sid, body


def test_realtime_start_owns_session_has_rules_and_never_returns_server_key(monkeypatch):
    async def run():
        engine, _factory, _service, live, sid, body = await prepared(monkeypatch)
        try:
            with pytest.raises(AccessError) as wrong_owner:
                await live.start(Request({'id': 2, 'roles': ['PHOTOGRAPHER']}, body, sid))
            assert wrong_owner.value.status == 404
            with pytest.raises(AccessError):
                await live.start(Request({'id': 1, 'roles': ['GUEST']}, body, sid))
            result = json.loads((await live.start(Request(OWNER, body, sid))).text)
            assert live.api.body.await_args.kwargs['max_bytes'] == 256 * 1024
            assert result['maxSeconds'] == 600 and result['revision'] == 1
            assert result['model'] == 'gpt-realtime-2.1'
            assert 'private-test-key' not in json.dumps(result)
            settings = realtime.create_call.await_args.args[1]
            assert settings['tools'] == [] and settings['max_output_tokens'] == 700
            assert all(price in settings['instructions'] for price in ('300 ₽', '400 ₽', '500 ₽'))
            assert 'после подтверждения владельцем или администратором' in settings['instructions']
            assert 'сами скидку не утверждают' in settings['instructions']
            assert settings['audio']['input']['turn_detection']['interrupt_response'] is True
            assert settings['audio']['input']['transcription']['language'] == 'ru'
            with pytest.raises(AccessError) as opened:
                await live.start(Request(OWNER, body, sid))
            assert opened.value.status == 409 and realtime.create_call.await_count == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_realtime_lease_blocks_existing_text_or_recorded_turns(monkeypatch):
    async def run():
        engine, _factory, service, live, sid, body = await prepared(monkeypatch)
        try:
            lease = json.loads((await live.start(Request(OWNER, body, sid))).text)
            monkeypatch.setattr(development.ai, 'roleplay', AsyncMock())
            with pytest.raises(AccessError) as active:
                await service.save_turn(OWNER, sid, {'revision': 1, 'text': 'Здравствуйте', 'finish': False})
            assert active.value.status == 409
            development.ai.roleplay.assert_not_awaited()
            await live.stop(Request(OWNER, {'leaseId': lease['leaseId']}, sid))
            await live.assert_idle(OWNER, sid)
            assert json.loads((await live.stop(Request(OWNER, {'leaseId': lease['leaseId']}, sid))).text)['replayed']
            assert realtime.hangup_call.await_args.args == ('rtc_test123',)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_status_recovers_owned_call_after_lost_start_response_and_hides_other_users(monkeypatch):
    async def run():
        engine, _factory, _service, live, sid, body = await prepared(monkeypatch)
        try:
            before = json.loads((await live.status(Request(OWNER, cid=sid))).text)
            assert before == {'active': False, 'revision': 1}
            started = json.loads((await live.start(Request(OWNER, body, sid))).text)
            recovered = json.loads((await live.status(Request(OWNER, cid=sid))).text)
            assert recovered == {'active': True, 'revision': 1, 'leaseId': started['leaseId'],
                                 'mode': 'guest', 'expiresAt': started['expiresAt']}
            with pytest.raises(AccessError) as forbidden:
                await live.status(Request({'id': 2, 'roles': ['PHOTOGRAPHER']}, cid=sid))
            assert forbidden.value.status == 404
            await live.stop(Request(OWNER, {'leaseId': recovered['leaseId']}, sid))
            assert json.loads((await live.status(Request(OWNER, cid=sid))).text)['active'] is False
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_realtime_events_are_owned_idempotent_revisioned_and_do_not_change_sales(monkeypatch):
    async def run():
        engine, factory, _service, live, sid, body = await prepared(monkeypatch)
        try:
            lease = json.loads((await live.start(Request(OWNER, body, sid))).text)
            event = {'id': 'item_abc', 'role': 'employee', 'text': 'Один кадр стоит 400 рублей.'}
            batch = {'leaseId': lease['leaseId'], 'revision': 1, 'events': [event]}
            with pytest.raises(AccessError) as forbidden:
                await live.events(Request({'id': 2, 'roles': ['PHOTOGRAPHER']}, batch, sid))
            assert forbidden.value.status == 404
            result = json.loads((await live.events(Request(OWNER, batch, sid))).text)
            assert live.api.body.await_args.kwargs['max_bytes'] == 128 * 1024
            assert result == {'ok': True, 'revision': 2, 'saved': 1}
            retry = json.loads((await live.events(Request(OWNER, batch, sid))).text)
            assert retry == {'ok': True, 'replayed': True, 'revision': 2}
            for changed in [dict(event, text='Другой текст'), dict(event, role='coach')]:
                with pytest.raises(AccessError):
                    await live.events(Request(OWNER, dict(batch, events=[changed]), sid))
            with pytest.raises(AccessError) as stale:
                await live.events(Request(OWNER, dict(batch, events=[dict(event, id='item_new')]), sid))
            assert stale.value.status == 409
            async with factory() as session:
                row = await session.get(SalesTrainingSession, sid)
                assert row.revision == 2 and row.status == 'ACTIVE'
                transcript = json.loads(row.transcript)
                assert len(transcript) == 2 and transcript[-1]['source'] == 'live_voice'
                sale = await session.scalar(select(Sale))
                assert sale.amount == 800 and sale.sold_photos == 2
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_realtime_event_validation_and_resend_after_stop(monkeypatch):
    async def run():
        engine, _factory, _service, live, sid, body = await prepared(monkeypatch, mode='coach')
        try:
            lease = json.loads((await live.start(Request(OWNER, body, sid))).text)
            batch = {'leaseId': lease['leaseId'], 'revision': 1,
                     'events': [{'id': 'item_1', 'role': 'employee', 'text': 'Как помочь гостю выбрать фотографии?'}]}
            for bad in [None, {}, {'id': 'item_1', 'role': [], 'text': 'Текст'},
                        {'id': 'item_1', 'role': 'employee', 'text': 'x' * 2001},
                        {'id': 'item_1', 'role': 'client', 'text': 'Не тот режим'},
                        {'id': 'item_1', 'role': 'employee', 'text': 'x', 'score': 100}]:
                with pytest.raises(AccessError) as error:
                    await live.events(Request(OWNER, dict(batch, events=[bad]), sid))
                assert error.value.status == 400
            await live.stop(Request(OWNER, {'leaseId': lease['leaseId']}, sid))
            assert json.loads((await live.events(Request(OWNER, batch, sid))).text)['revision'] == 2
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_realtime_failed_connection_releases_reservation_and_limits_reconnect(monkeypatch):
    async def run():
        engine, factory, _service, live, sid, body = await prepared(monkeypatch)
        try:
            realtime.create_call.side_effect = AccessError('Сервис временно недоступен.', 503)
            with pytest.raises(AccessError) as failed:
                await live.start(Request(OWNER, body, sid))
            assert failed.value.status == 503
            async with factory() as session:
                row = await session.scalar(select(OperationRequest).where(OperationRequest.request_key == realtime.LEASE_KEY))
                assert json.loads(row.result)['status'] == 'closed'
            await live.assert_idle(OWNER, sid)
            with pytest.raises(AccessError) as throttled:
                await live.start(Request(OWNER, body, sid))
            assert throttled.value.status == 429
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_expiry_hangup_retries_after_network_failure_and_recovers_after_restart(monkeypatch):
    async def run():
        engine, _factory, service, live, sid, body = await prepared(monkeypatch)
        clock = [1000000000.0]
        monkeypatch.setattr(realtime, 'time', SimpleNamespace(time=lambda: clock[0]))
        try:
            lease = json.loads((await live.start(Request(OWNER, body, sid))).text)
            clock[0] += realtime.MAX_SECONDS + 1
            restarted = realtime.RealtimeTraining(service)
            realtime.hangup_call.return_value = False
            await restarted.sweep()
            with pytest.raises(AccessError) as closing:
                await restarted.assert_idle(OWNER, sid)
            assert closing.value.status == 409
            realtime.hangup_call.return_value = True
            await restarted.sweep()
            await restarted.assert_idle(OWNER, sid)
            batch = {'leaseId': lease['leaseId'], 'revision': 1,
                     'events': [{'id': 'item_recovered', 'role': 'employee', 'text': 'Текст восстановлен.'}]}
            assert json.loads((await restarted.events(Request(OWNER, batch, sid))).text)['revision'] == 2
            clock[0] += 24 * 3600
            with pytest.raises(AccessError):
                await restarted.events(Request(OWNER, batch, sid))
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_realtime_global_call_cap_is_enforced_before_provider(monkeypatch):
    async def run():
        engine, _factory, _service, live, sid, body = await prepared(monkeypatch)
        try:
            monkeypatch.setattr(realtime, 'MAX_CALLS', 0)
            with pytest.raises(AccessError) as limited:
                await live.start(Request(OWNER, body, sid))
            assert limited.value.status == 429
            realtime.create_call.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_live_coach_finish_summarizes_without_exam_score_and_is_repeatable(monkeypatch):
    async def run():
        engine, factory, _service, live, sid, body = await prepared(monkeypatch, mode='coach')
        try:
            lease = json.loads((await live.start(Request(OWNER, body, sid))).text)
            with pytest.raises(AccessError):
                await live.finish(Request(OWNER, {'leaseId': lease['leaseId'], 'revision': 1}, sid))
            await live.events(Request(OWNER, {'leaseId': lease['leaseId'], 'revision': 1,
                'events': [{'id': 'item_1', 'role': 'employee', 'text': 'Как объяснить цену?'},
                           {'id': 'item_2', 'role': 'coach', 'text': 'Спокойно назовите 400 ₽ за выбранный кадр.'}]}, sid))
            await live.stop(Request(OWNER, {'leaseId': lease['leaseId']}, sid))
            summary = {'reply': 'Обсудили честную цену.', 'score': None, 'errors': [], 'recommendations': ['Потренируйте объяснение цены.']}
            monkeypatch.setattr(development.ai, 'structured', AsyncMock(return_value={'status': 'completed', 'data': summary}))
            monkeypatch.setattr(development.ai, 'roleplay', AsyncMock())
            finish = {'leaseId': lease['leaseId'], 'revision': 2}
            result = json.loads((await live.finish(Request(OWNER, finish, sid))).text)
            assert result == {'ok': True, 'revision': 3}
            assert json.loads((await live.finish(Request(OWNER, finish, sid))).text)['replayed']
            development.ai.roleplay.assert_not_awaited()
            assert development.ai.structured.await_count == 1
            async with factory() as session:
                current = await session.get(SalesTrainingSession, sid)
                assert current.status == 'COMPLETED'
                assert json.loads(current.evaluation)['score'] is None
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_live_guest_finish_uses_existing_server_evaluator(monkeypatch):
    async def run():
        engine, factory, _service, live, sid, body = await prepared(monkeypatch)
        try:
            lease = json.loads((await live.start(Request(OWNER, body, sid))).text)
            await live.events(Request(OWNER, {'leaseId': lease['leaseId'], 'revision': 1,
                'events': [{'id': 'item_1', 'role': 'employee', 'text': 'Кадр стоит 400 рублей.'}]}, sid))
            await live.stop(Request(OWNER, {'leaseId': lease['leaseId']}, sid))
            grade = {'reply': 'Объяснили цену.', 'score': 80, 'errors': [], 'recommendations': ['Уточните пожелания.']}
            monkeypatch.setattr(development.ai, 'roleplay', AsyncMock(return_value={'status': 'completed', 'data': grade}))
            await live.finish(Request(OWNER, {'leaseId': lease['leaseId'], 'revision': 2}, sid))
            assert development.ai.roleplay.await_args.args[2] is True
            async with factory() as session:
                assert json.loads((await session.get(SalesTrainingSession, sid)).evaluation)['score'] == 80
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_provider_sdp_is_read_to_eof_and_api_key_is_server_only(monkeypatch):
    posted = {}

    class Response:
        status = 201

        def __init__(self):
            self.headers = {'Location': 'https://api.openai.com/v1/realtime/calls/rtc_chunked123'}

        @property
        def content(self):
            return self

        async def iter_chunked(self, _size):
            for part in [b'v=', b'0\r\n', b'm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n']:
                yield part

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def post(self, url, **kwargs):
            posted.update(url=url, **kwargs)
            return Response()

    monkeypatch.setattr(realtime, 'config', SimpleNamespace(openai_api_key='private-test-key'))
    monkeypatch.setattr(realtime.aiohttp, 'ClientSession', Client)
    answer, call_id = asyncio.run(realtime.create_call(SDP, {'type': 'realtime', 'model': 'gpt-realtime-2.1'}))
    assert answer == 'v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n' and call_id == 'rtc_chunked123'
    assert posted['url'] == 'https://api.openai.com/v1/realtime/calls'
    assert posted['headers']['Authorization'] == 'Bearer private-test-key'


def test_stopping_a_connection_while_provider_is_pending_closes_late_call(monkeypatch):
    async def run():
        engine, _factory, _service, live, sid, body = await prepared(monkeypatch)
        connected = asyncio.Event()
        release = asyncio.Event()

        async def delayed(_sdp, _config):
            connected.set()
            await release.wait()
            return SDP, 'rtc_late'

        realtime.create_call.side_effect = delayed
        try:
            task = asyncio.create_task(live.start(Request(OWNER, body, sid)))
            await connected.wait()
            lease = json.loads((await live.status(Request(OWNER, cid=sid))).text)
            await live.stop(Request(OWNER, {'leaseId': lease['leaseId']}, sid))
            release.set()
            with pytest.raises(AccessError) as closed:
                await task
            assert closed.value.status == 409
            assert realtime.hangup_call.await_args.args == ('rtc_late',)
            assert json.loads((await live.status(Request(OWNER, cid=sid))).text)['active'] is False
        finally:
            release.set()
            await engine.dispose()
    asyncio.run(run())


def provider_response(monkeypatch, chunks):
    class Response:
        status = 201

        def __init__(self):
            self.headers = {'Location': 'https://api.openai.com/v1/realtime/calls/rtc_cleanup'}

        @property
        def content(self):
            return self

        def iter_chunked(self, _size):
            return chunks()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def post(self, _url, **_kwargs):
            return Response()

    monkeypatch.setattr(realtime.aiohttp, 'ClientSession', Client)


def test_provider_sdp_cancellation_closes_known_call_and_is_reraised(monkeypatch):
    async def run():
        reading = asyncio.Event()

        async def chunks():
            reading.set()
            await asyncio.Event().wait()
            yield b'v=0'

        provider_response(monkeypatch, chunks)
        monkeypatch.setattr(realtime, 'config', SimpleNamespace(openai_api_key='private-test-key'))
        monkeypatch.setattr(realtime, 'hangup_call', AsyncMock(return_value=True))
        task = asyncio.create_task(realtime.create_call(SDP, {'model': 'gpt-realtime-2.1'}))
        await reading.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        realtime.hangup_call.assert_awaited_once_with('rtc_cleanup')

    asyncio.run(run())


@pytest.mark.parametrize('failure', ['cancelled', 'network', 'invalid_sdp'])
def test_failed_provider_cleanup_keeps_call_id_for_sweeper_retry(monkeypatch, failure):
    async def run():
        create_call = realtime.create_call
        engine, factory, _service, live, sid, body = await prepared(monkeypatch)
        reading = asyncio.Event()

        async def chunks():
            reading.set()
            if failure == 'cancelled':
                await asyncio.Event().wait()
            if failure == 'network':
                raise realtime.aiohttp.ClientConnectionError('Connection lost')
            yield b'not an SDP answer'

        provider_response(monkeypatch, chunks)
        monkeypatch.setattr(realtime, 'create_call', create_call)
        realtime.hangup_call.return_value = False
        try:
            task = asyncio.create_task(live.start(Request(OWNER, body, sid)))
            await reading.wait()
            if failure == 'cancelled':
                task.cancel()
            with pytest.raises(asyncio.CancelledError if failure == 'cancelled' else AccessError):
                await task
            realtime.hangup_call.assert_awaited_once_with('rtc_cleanup')
            async with factory() as session:
                row = await session.scalar(select(OperationRequest).where(OperationRequest.request_key == realtime.LEASE_KEY))
                stored = json.loads(row.result)
                assert stored['status'] == 'closing' and stored['callId'] == 'rtc_cleanup'
            realtime.hangup_call.return_value = True
            await live.sweep()
            assert json.loads((await live.status(Request(OWNER, cid=sid))).text)['active'] is False
            assert realtime.hangup_call.await_count == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_start_cancellation_before_provider_id_releases_reservation(monkeypatch):
    async def run():
        engine, factory, _service, live, sid, body = await prepared(monkeypatch)
        connecting = asyncio.Event()

        async def delayed(_sdp, _config):
            connecting.set()
            await asyncio.Event().wait()

        realtime.create_call.side_effect = delayed
        try:
            task = asyncio.create_task(live.start(Request(OWNER, body, sid)))
            await connecting.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with factory() as session:
                row = await session.scalar(select(OperationRequest).where(OperationRequest.request_key == realtime.LEASE_KEY))
                stored = json.loads(row.result)
                assert stored['status'] == 'closed' and stored['callId'] is None
            realtime.hangup_call.assert_not_awaited()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_cancellation_during_activation_closes_provider_call(monkeypatch):
    async def run():
        engine, factory, _service, live, sid, body = await prepared(monkeypatch)
        monkeypatch.setattr(live.api, 'audit_write', AsyncMock(side_effect=asyncio.CancelledError))
        try:
            with pytest.raises(asyncio.CancelledError):
                await live.start(Request(OWNER, body, sid))
            realtime.hangup_call.assert_awaited_once_with('rtc_test123')
            async with factory() as session:
                row = await session.scalar(select(OperationRequest).where(OperationRequest.request_key == realtime.LEASE_KEY))
                assert json.loads(row.result)['status'] == 'closed'
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_closing_stale_connecting_lease_preserves_late_provider_id(monkeypatch):
    async def run():
        engine, _factory, _service, live, sid, body = await prepared(monkeypatch)
        try:
            await live.start(Request(OWNER, body, sid))
            async with live.api.engine.connect() as conn:
                stored = await live.record(conn, OWNER['id'], realtime.LEASE_KEY)
            stale = dict(stored, status='connecting', callId=None)
            assert await live.close(OWNER['id'], stale)
            realtime.hangup_call.assert_awaited_once_with('rtc_test123')
            assert json.loads((await live.status(Request(OWNER, cid=sid))).text)['active'] is False
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_cancelled_stop_persists_closing_for_sweeper_retry(monkeypatch):
    async def run():
        engine, factory, _service, live, sid, body = await prepared(monkeypatch)
        hanging_up = asyncio.Event()

        async def delayed_hangup(_call_id):
            hanging_up.set()
            await asyncio.Event().wait()

        try:
            lease = json.loads((await live.start(Request(OWNER, body, sid))).text)
            realtime.hangup_call.side_effect = delayed_hangup
            task = asyncio.create_task(live.stop(Request(OWNER, {'leaseId': lease['leaseId']}, sid)))
            await hanging_up.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with factory() as session:
                row = await session.scalar(select(OperationRequest).where(OperationRequest.request_key == realtime.LEASE_KEY))
                stored = json.loads(row.result)
                assert stored['status'] == 'closing' and stored['callId'] == 'rtc_test123'
            realtime.hangup_call.side_effect = None
            realtime.hangup_call.return_value = True
            await live.sweep()
            assert json.loads((await live.status(Request(OWNER, cid=sid))).text)['active'] is False
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize('changes', [{'revision': True}, {'mode': 'admin'}, {'sdp': 'x' * 50}, {'sdp': 'v=0' + 'x' * (128 * 1024)}])
def test_start_rejects_invalid_session_request_before_provider(monkeypatch, changes):
    async def run():
        engine, _factory, _service, live, sid, body = await prepared(monkeypatch)
        try:
            with pytest.raises(AccessError) as error:
                await live.start(Request(OWNER, body | changes, sid))
            assert error.value.status == 400
            realtime.create_call.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(run())

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# app.avito_hr_console was not part of the 3.62 source imported from Timeweb; the tests
# run again as soon as the module is restored.
pytest.importorskip('app.avito_hr_console', reason='модуль Авито отсутствует в версии 3.62 с Timeweb')

from app.avito_hr_console import Console
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.avito_hr import Worker, prepare
from app.miniapp_security import AccessError


class Request(dict):
    def __init__(self, roles=('OWNER',), **match):
        super().__init__(miniapp_actor={'id': 1, 'roles': list(roles)})
        self.match_info = match


async def setup():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    await prepare(engine)
    await prepare(engine)  # Additive startup is idempotent.
    async with engine.begin() as conn:
        await conn.execute(text('CREATE TABLE users(id INTEGER PRIMARY KEY,active BOOLEAN)'))
        await conn.execute(text('CREATE TABLE user_roles(user_id INTEGER,role TEXT)'))
        await conn.execute(text('''CREATE TABLE notifications(user_id INTEGER,text TEXT,sent BOOLEAN,
            created_at TIMESTAMP,event_key TEXT,priority TEXT,kind TEXT,payload TEXT,UNIQUE(user_id,event_key))'''))
        await conn.execute(text('INSERT INTO users VALUES (1,TRUE)'))
        await conn.execute(text("INSERT INTO user_roles VALUES (1,'OWNER')"))
        await conn.execute(text("INSERT INTO hr_avito_vacancies(code,role,payload_hash,status,item_id) VALUES ('anapa','PHOTOGRAPHER','test','activated',100)"))
    worker = Worker(engine)
    worker.uid = 1
    worker.api.request = AsyncMock(return_value={'id': 'sent-1'})
    return engine, worker


def msg(value, mid='in-1', direction='in', created=1):
    return {'id': mid, 'type': 'text', 'direction': direction, 'created': created, 'content': {'text': value}}


def test_same_event_is_sent_only_once(monkeypatch):
    async def run():
        engine, worker = await setup()
        worker.read_messages = AsyncMock(return_value=[msg('Здравствуйте')])
        monkeypatch.setattr('app.avito_hr.asyncio.sleep', AsyncMock())
        await worker.process_chat('chat', 'PHOTOGRAPHER', 'anapa', 100)
        await worker.process_chat('chat', 'PHOTOGRAPHER', 'anapa', 100)
        assert worker.api.request.await_count == 1
        session = await worker.session('chat')
        assert session['vacancy_code'] == 'anapa'
        await engine.dispose()
    asyncio.run(run())


def test_unknown_question_creates_private_owner_notice_without_duplicate(monkeypatch):
    async def run():
        engine, worker = await setup()
        worker.read_messages = AsyncMock(return_value=[msg('Какая зарплата?')])
        monkeypatch.setattr('app.avito_hr.asyncio.sleep', AsyncMock())
        await worker.process_chat('chat', 'PHOTOGRAPHER', 'anapa', 100)
        await worker.process_chat('chat', 'PHOTOGRAPHER', 'anapa', 100)
        async with engine.connect() as conn:
            rows = (await conn.execute(text('SELECT * FROM notifications'))).mappings().all()
        assert len(rows) == 1 and rows[0]['user_id'] == 1 and rows[0]['kind'] == 'service_status'
        assert 'Какая зарплата' not in rows[0]['text']
        assert (await worker.session('chat'))['status'] == 'REVIEW'
        await engine.dispose()
    asyncio.run(run())


def test_human_outgoing_message_pauses_automation():
    async def run():
        engine, worker = await setup()
        worker.read_messages = AsyncMock(return_value=[msg('Ответ человека', 'human', 'out', 1), msg('Да', created=2)])
        await worker.process_chat('chat', 'PHOTOGRAPHER', 'anapa', 100)
        assert not worker.api.request.await_count
        assert (await worker.session('chat'))['status'] == 'HUMAN'
        await engine.dispose()
    asyncio.run(run())


def test_requested_handoff_and_stop_prevent_further_replies():
    async def run():
        for value, state in [('Соедините с менеджером', 'HUMAN'), ('Не пишите больше', 'STOPPED')]:
            engine, worker = await setup()
            worker.read_messages = AsyncMock(return_value=[msg(value)])
            await worker.process_chat('chat', 'PHOTOGRAPHER', 'anapa', 100)
            await worker.process_chat('chat', 'PHOTOGRAPHER', 'anapa', 100)
            assert not worker.api.request.await_count
            assert (await worker.session('chat'))['status'] == state
            await engine.dispose()
    asyncio.run(run())


def test_new_message_or_human_answer_abandons_stale_response():
    async def run():
        engine, worker = await setup()
        worker.read_messages = AsyncMock(side_effect=[[msg('Привет')], [msg('Привет'), msg('Другой вопрос', 'in-2', created=2)]])
        await worker.process_chat('chat', 'PHOTOGRAPHER', 'anapa', 100)
        assert not worker.api.request.await_count
        await engine.dispose()
    asyncio.run(run())


def test_uncertain_delivery_is_never_replayed(monkeypatch):
    async def run():
        engine, worker = await setup()
        worker.read_messages = AsyncMock(return_value=[msg('Привет')])
        worker.api.request = AsyncMock(side_effect=TimeoutError())
        monkeypatch.setattr('app.avito_hr.asyncio.sleep', AsyncMock())
        with pytest.raises(TimeoutError):
            await worker.process_chat('chat', 'PHOTOGRAPHER', 'anapa', 100)
        await worker.process_chat('chat', 'PHOTOGRAPHER', 'anapa', 100)
        assert worker.api.request.await_count == 1
        await engine.dispose()
    asyncio.run(run())


def test_console_role_access_and_optimistic_terms_revision():
    async def run():
        engine, worker = await setup()
        body = {'revision': 0, 'confirmed': True, 'payload': {'title': 'Фотограф', 'pay': '15%', 'schedule': '3/1', 'location': 'Анапа'}}
        api = SimpleNamespace(engine=engine, body=AsyncMock(return_value=body), audit_write=AsyncMock())
        console = Console(api, worker, True)
        for role in ('PHOTOGRAPHER', 'MANAGER'):
            with pytest.raises(AccessError) as error:
                await console.listing(Request((role,)))
            assert error.value.status == 403
        await console.terms(Request(code='anapa'))
        assert (await worker.terms('anapa'))['pay'] == '15%'
        assert await worker.terms('other') == {}
        with pytest.raises(AccessError) as error:
            await console.terms(Request(code='anapa'))
        assert error.value.status == 409
        result = await console.listing(Request())
        assert len(json.loads(result.text)['vacancies']) == 1
        await engine.dispose()
    asyncio.run(run())


def test_only_matching_approved_vacancy_is_seeded_and_owner_edits_survive_restart():
    async def run():
        engine, worker = await setup()
        payload = json.loads(Path('app/avito_vacancies_20261020.json').read_text())[0]
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE hr_avito_vacancies SET code=:code,payload_hash=:hash WHERE code='anapa'"), {'code': payload['vacancy_code'], 'hash': digest})
        await prepare(engine)
        terms = await worker.terms(payload['vacancy_code'])
        assert '15%' in terms['pay'] and '3/1' in terms['schedule']
        assert '5 000' in terms['housing'] and '10 000' not in terms['housing']
        async with engine.begin() as conn:
            await conn.execute(text('UPDATE hr_avito_terms SET payload=:payload WHERE vacancy_code=:code'), {'code': payload['vacancy_code'], 'payload': json.dumps({'pay': 'Новое условие владельца'})})
        await prepare(engine)
        assert (await worker.terms(payload['vacancy_code']))['pay'] == 'Новое условие владельца'
        assert await worker.terms('unrelated') == {}
        await engine.dispose()
    asyncio.run(run())


def test_new_incoming_in_the_same_second_is_not_lost_or_repeated(monkeypatch):
    async def run():
        engine, worker = await setup()
        monkeypatch.setattr('app.avito_hr.asyncio.sleep', AsyncMock())
        first = msg('Здравствуйте', 'in-1', created=1)
        worker.read_messages = AsyncMock(return_value=[first])
        await worker.process_chat('chat', 'PHOTOGRAPHER', 'anapa', 100)
        answer = worker.api.request.await_args.args[2]['message']['text']
        # Avito may return newest first, including equal-second messages.
        worker.read_messages = AsyncMock(return_value=[msg('Какая зарплата?', 'in-2', created=1), msg(answer, 'sent-1', 'out', 1), first])
        worker.api.request.return_value = {'id': 'sent-2'}
        await worker.process_chat('chat', 'PHOTOGRAPHER', 'anapa', 100)
        await worker.process_chat('chat', 'PHOTOGRAPHER', 'anapa', 100)
        assert worker.api.request.await_count == 1
        assert (await worker.session('chat'))['status'] == 'REVIEW'
        await engine.dispose()
    asyncio.run(run())


def test_manual_resume_includes_messages_sent_after_takeover():
    async def run():
        engine, worker = await setup()
        worker.read_messages = AsyncMock(return_value=[msg('Ответ человека', 'h1', 'out')])
        await worker.process_chat('chat', 'PHOTOGRAPHER', 'anapa', 100)
        worker.read_messages = AsyncMock(return_value=[msg('Ответ человека', 'h1', 'out'), msg('Дополнительный ответ', 'h2', 'out', 2)])
        api = SimpleNamespace(engine=engine, body=AsyncMock(return_value={'action': 'resume'}), audit_write=AsyncMock())
        await Console(api, worker, True).control(Request(chat='chat'))
        session = await worker.session('chat')
        assert session['status'] == 'ACTIVE' and set(json.loads(session['ignored_outgoing'])) == {'h1', 'h2'}
        await engine.dispose()
    asyncio.run(run())


def test_existing_vacancy_binding_checks_ownership_and_active_status(monkeypatch):
    async def run():
        engine, worker = await setup()
        api = SimpleNamespace(engine=engine, body=AsyncMock(return_value={'itemId': 200, 'role': 'MANAGER'}), audit_write=AsyncMock())
        console = Console(api, worker, True)
        lookup = AsyncMock(return_value=[{'id': 200, 'status': 'active'}])
        monkeypatch.setattr('app.avito_hr.own_items', lookup)
        result = await console.bind(Request())
        assert result.status == 201 and not worker.api.request.await_count
        with pytest.raises(AccessError) as error:
            await console.bind(Request())
        assert error.value.status == 409
        lookup.return_value = []
        api.body.return_value = {'itemId': 201, 'role': 'MANAGER'}
        with pytest.raises(AccessError) as error:
            await console.bind(Request())
        assert error.value.status == 400
        await engine.dispose()
    asyncio.run(run())


def test_preview_does_not_send_and_bad_or_revoked_terms_are_not_used():
    async def run():
        engine, worker = await setup()
        api = SimpleNamespace(engine=engine, body=AsyncMock(return_value={'code': 'anapa', 'text': 'Какая зарплата?'}), audit_write=AsyncMock())
        response = await Console(api, worker, True).preview(Request())
        assert json.loads(response.text)['sent'] is False and not worker.api.request.await_count
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO hr_avito_terms(vacancy_code,payload,confirmed) VALUES ('anapa','{\"pay\":\"15%\"}',0)"))
        assert await worker.terms('anapa') == {}
        await engine.dispose()
    asyncio.run(run())

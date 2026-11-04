"""Owner scenarios: assert decisions and facts, not the parser's keyword list."""
import asyncio
import hashlib
import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.avito_dialogue import respond, validate_terms
from app.avito_hr import prepare

ROWS = json.loads(Path('app/avito_hr_terms_20261020.json').read_text())
PHOTO, MANAGER = [r['terms'] for r in ROWS]


def history(value):
    return [{'type': 'text', 'direction': 'out', 'created': 1, 'content': {'text': value}}]


def test_requested_detail_is_selected_and_role_specific():
    payment = respond('От личных продаж или команды считают процент?', 'MANAGER', terms=MANAGER)
    assert 'лично записал' in payment.answer and 'бонус' not in payment.answer.lower()
    night = respond('Ночью тоже надо работать?', 'MANAGER', terms=MANAGER)
    assert 'Ночных смен нет' in night.answer and '9:00' not in night.answer
    late = respond('Нужно задерживаться после 20:00?', 'PHOTOGRAPHER', terms=PHOTO)
    assert 'обработке' in late.answer and 'поздней продаже' in late.answer
    assert 'после 20:00 не предусмотрена' in respond('Работа после 20:00 бывает?', 'MANAGER', terms=MANAGER).answer
    assert 'личных продаж' in respond('Сколько платите?', 'PHOTOGRAPHER', terms=PHOTO).answer
    assert 'записал' not in respond('Сколько платите?', 'PHOTOGRAPHER', terms=PHOTO).answer


def test_context_followup_and_composite_question():
    result = respond('А сколько?', 'MANAGER', history(MANAGER['housing']), MANAGER)
    assert '5 000' in result.answer and not result.escalate
    result = respond('Спасибо! Есть питание и какой график?', 'MANAGER', terms=MANAGER)
    assert 'самостоятельно' in result.answer and '3/1' in result.answer
    result = respond('Какая зарплата и можно с кошкой?', 'MANAGER', terms=MANAGER)
    assert '15%' in result.answer and result.escalate and 'можно с кошкой' not in result.answer
    result = respond('Готов выйти завтра, но можно без опыта?', 'MANAGER', terms=MANAGER)
    assert 'без опыта' in result.answer and result.status == 'READY'


def test_unknown_details_are_not_invented_and_confirmed_edits_are_used():
    for question in ('Сколько длится обучение?', 'Какой залог за жильё?', 'Сколько компенсируют за проезд?'):
        result = respond(question, 'MANAGER', terms=MANAGER)
        assert result.escalate and not result.answer
    changed = dict(MANAGER, meals='Питание организуется самостоятельно согласно новому условию владельца.')
    assert 'новому условию' in respond('Питание предоставляется?', 'MANAGER', terms=changed).answer
    assert not respond('Спасибо, до свидания', 'MANAGER', terms=MANAGER).answer
    assert not respond('Какая погода?', 'MANAGER', terms=MANAGER).escalate


def test_terms_upgrade_is_idempotent_and_preserves_owner_changes():
    async def run():
        engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        await prepare(engine)
        payloads = json.loads(Path('app/avito_vacancies_20261020.json').read_text())
        previous = json.loads(Path('app/avito_hr_terms_previous.json').read_text())
        async with engine.begin() as c:
            for i, payload in enumerate(payloads):
                code = payload['vacancy_code']
                digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                await c.execute(text("INSERT INTO hr_avito_vacancies(code,role,payload_hash,status,item_id) VALUES (:code,'MANAGER',:hash,'activated',:item)"), {'code': code, 'hash': digest, 'item': i + 1})
                old = next(r['terms'] for r in previous if r['vacancy_code'] == code)
                # The previously deployed validator included all old fields.
                old = {k: v for k, v in validate_terms(old).items() if k in set(old) | {'contract'}}
                if i: old['housing'] = 'Условие изменено владельцем'
                await c.execute(text('INSERT INTO hr_avito_terms(vacancy_code,payload,revision,confirmed) VALUES (:code,:payload,1,1)'), {'code': code, 'payload': json.dumps(old, ensure_ascii=False)})
        await prepare(engine)
        await prepare(engine)
        async with engine.connect() as c:
            rows = (await c.execute(text('SELECT payload,revision FROM hr_avito_terms ORDER BY vacancy_code'))).mappings().all()
        assert sorted(r['revision'] for r in rows) == [1, 2]
        values = [json.loads(r['payload']) for r in rows]
        assert any(v.get('meals') for v in values)
        assert any(v['housing'] == 'Условие изменено владельцем' for v in values)
        await engine.dispose()
    asyncio.run(run())


def test_cycle_audits_human_chats_read_only(monkeypatch):
    from unittest.mock import AsyncMock

    from app.avito_hr import Worker

    async def run():
        engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        await prepare(engine)
        async with engine.begin() as c:
            await c.execute(text("INSERT INTO hr_avito_vacancies(code,role,payload_hash,status,item_id) VALUES ('test','MANAGER','hash','activated',8485338251)"))
        worker = Worker(engine)
        worker.api.request = AsyncMock(side_effect=[{'id': 1}, {'chats': [{'id': 'private-chat', 'context': {'value': {'id': 8485338251}}}]}])
        worker.read_messages = AsyncMock(return_value=[{'type': 'text', 'direction': 'in', 'created': 123}])
        worker.session = AsyncMock(return_value={'status': 'HUMAN'})
        await worker.cycle()
        assert worker.dialogue_audit == {'8485338251': {'chats': 1, 'lastIncoming': 123, 'lastOutgoing': 0}}
        assert worker.api.request.await_count == 2
        assert all(call.args[0] == 'GET' for call in worker.api.request.await_args_list)
        await engine.dispose()
    asyncio.run(run())


def test_acceptance_moves_to_interview_and_paid_training_is_not_confused():
    result = respond('Условия подходят, хочу работать', 'MANAGER', history(MANAGER['pay']), MANAGER)
    assert 'номер телефона' in result.answer
    result = respond('Обучение платное?', 'MANAGER', terms=MANAGER)
    assert not result.answer and result.escalate
    assert '15%' not in respond('Когда первая зарплата?', 'MANAGER', terms=MANAGER).answer
    assert '15%' not in respond('Оплата обучения предусмотрена?', 'MANAGER', terms=MANAGER).answer
    assert 'записывает гостей' in respond('Что делать', 'MANAGER', terms=MANAGER).answer

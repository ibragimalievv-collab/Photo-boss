import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Update
from aiohttp import web
from sqlalchemy import text
from test_insights import fixture

from app.hr_bot import SOURCE, HRBot, install_hr_bot, secret_for


def update(number, value, uid=9001, chat_type='private'):
    return Update.model_validate({'update_id': number, 'message': {'message_id': number, 'date': 1700000000,
        'chat': {'id': uid, 'type': chat_type}, 'from': {'id': uid, 'is_bot': False, 'first_name': 'Candidate'}, 'text': value}})


async def setup():
    engine, _factory, _api = await fixture()
    service = HRBot(engine, AsyncMock(), 'hr-secret')
    await service.prepare()
    await service.prepare()
    return engine, service


ANSWERS = ['/start', 'Ищу работу', 'Согласен', 'Анна', 'Фотограф', 'Казань, готова в Сочи',
           'Без опыта', '+79991234567', 'Через две недели, нужен переезд', 'Отправить анкету']


def test_questionnaire_restart_replay_and_existing_hr_card():
    async def run():
        engine, service = await setup()
        for i, value in enumerate(ANSWERS):
            result = await service.receive(update(i, value))
            if i == 5:
                service = HRBot(engine, AsyncMock(), 'hr-secret')
                assert 'Продолжим' in (await service.receive(update(i + 100, '/start')))[0]
                # Use the new stream sequence after the restart.
                break
        for i, value in enumerate(ANSWERS[6:], 106):
            result = await service.receive(update(i, value))
        assert result[1] == 'done'
        assert await service.receive(update(109, 'Отправить анкету')) == result
        assert await service.receive(update(108, 'old')) is None
        await service.receive(update(110, '/start'))
        async with engine.connect() as conn:
            rows = (await conn.execute(text('SELECT * FROM hr_candidates'))).mappings().all()
            assert len(rows) == 1
            row = rows[0]
            assert row['name'] == 'Анна' and row['source'] == SOURCE and row['stage'] == 'NEW'
            assert row['role'] == 'PHOTOGRAPHER'
            assert '9001' in row['contact'] and 'Казань' in row['notes'] and 'consent_at' in row['notes']
            assert await conn.scalar(text("SELECT COUNT(*) FROM audit_logs WHERE action='hr_bot_candidate_created'")) == 1
            assert await conn.scalar(text('SELECT COUNT(*) FROM users')) == 2  # No employee auto-created.
        await engine.dispose()
    asyncio.run(run())


def test_validation_consent_cancel_and_no_group_data():
    async def run():
        engine, service = await setup()
        assert await service.receive(update(1, '/start', chat_type='group')) is None
        assert (await service.receive(update(2, 'Ищу работу')))[1] == 'consent'
        assert (await service.receive(update(3, 'Нет')))[1] == 'consent'
        for i, value in enumerate(['Согласен', 'Анна', 'ADMIN', 'Менеджер по записи', 'Анапа', 'Нет'], 4):
            result = await service.receive(update(i, value))
            if value == 'ADMIN':
                assert result[1] == 'role'
        assert (await service.receive(update(10, 'not a phone')))[1] == 'contact'
        assert (await service.receive(update(11, '/cancel')))[1] == 'welcome'
        async with engine.connect() as conn:
            assert await conn.scalar(text('SELECT answers FROM hr_bot_sessions')) == '{}'
            assert await conn.scalar(text('SELECT COUNT(*) FROM hr_candidates')) == 0
        await engine.dispose()
    asyncio.run(run())


def test_missing_owner_rolls_back_submission():
    async def run():
        engine, service = await setup()
        for i, value in enumerate(ANSWERS[:-1]):
            await service.receive(update(i, value))
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM user_roles WHERE role='OWNER'"))
        with pytest.raises(RuntimeError):
            await service.receive(update(9, ANSWERS[-1]))
        async with engine.connect() as conn:
            assert await conn.scalar(text('SELECT step FROM hr_bot_sessions')) == 'confirm'
            assert await conn.scalar(text('SELECT COUNT(*) FROM hr_candidates')) == 0
        await engine.dispose()
    asyncio.run(run())


def test_webhook_authentication_and_send_failure_retry():
    async def run():
        engine, service = await setup()
        service.ready = True
        req = SimpleNamespace(headers={}, content_length=1, json=AsyncMock(return_value=update(0, '/start').model_dump(mode='json')))
        with pytest.raises(web.HTTPForbidden):
            await service.webhook(req)
        req.headers['X-Telegram-Bot-Api-Secret-Token'] = 'hr-secret'
        service.bot.send_message.side_effect = RuntimeError('network')
        with pytest.raises(web.HTTPServiceUnavailable):
            await service.webhook(req)
        service.bot.send_message.side_effect = None
        assert (await service.webhook(req)).status == 200
        assert service.bot.send_message.await_count == 2
        await engine.dispose()
    asyncio.run(run())


def test_disabled_and_main_identity_never_construct_client(monkeypatch):
    monkeypatch.setenv('HR_BOT_ENABLED', '0')
    assert install_hr_bot(web.Application(), None, '111:main') is None
    monkeypatch.setenv('HR_BOT_ENABLED', '1')
    monkeypatch.setenv('HR_BOT_TOKEN', '111:other')
    assert install_hr_bot(web.Application(), None, '111:main') is None
    monkeypatch.setenv('HR_BOT_TOKEN', '')
    assert install_hr_bot(web.Application(), None, '111:main') is None
    assert secret_for('222:hr') != secret_for('111:main')


@pytest.mark.parametrize('username,works', [('Really_Boss_bot', False), ('Really_boss_HR_bot', True)])
def test_startup_identity_and_separate_webhook(monkeypatch, username, works):
    async def run():
        engine, _ = await setup()
        bot = AsyncMock()
        bot.get_me.return_value = SimpleNamespace(username=username)
        monkeypatch.setattr('app.hr_bot.Bot', lambda **kw: bot)
        monkeypatch.setattr('app.launch_policy.app_url', lambda: 'https://example.org/app/')
        monkeypatch.setenv('HR_BOT_ENABLED', '1')
        monkeypatch.setenv('HR_BOT_TOKEN', '222:hr')
        app = web.Application()
        service = install_hr_bot(app, engine, '111:main')
        await app.on_startup[-1](app)
        assert service.ready is works
        if works:
            assert bot.set_webhook.call_args.args[0] == 'https://example.org/telegram/hr/webhook'
            assert bot.set_webhook.call_args.kwargs['drop_pending_updates'] is False
        else:
            bot.set_webhook.assert_not_called()
        await app.on_cleanup[-1](app)
        bot.session.close.assert_awaited_once()
        await engine.dispose()
    asyncio.run(run())

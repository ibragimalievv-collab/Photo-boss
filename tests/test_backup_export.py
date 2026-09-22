import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendDocument
from test_insights import fixture

from app.models import Setting
from app.services import operations


def test_business_export_serializes_booking_time_without_changing_money():
    async def run():
        engine,factory,_api=await fixture()
        try:
            async with factory() as session:
                document=json.loads(await operations.backup_payload(session))
                assert document['version']==1
                assert document['bookings'][0]['shoot_date']=='2026-09-20'
                assert document['bookings'][0]['shoot_time']=='12:00:00'
                assert document['sales'][0]['amount']==800
                assert document['sales'][0]['commission']==120
                assert document['users'][0]['active'] is True
                assert document['users'][0]['tg_id']==1
                assert set(document)=={'created_at','version','users','clients','bookings','sales'}
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_daily_export_marks_delivered_only_after_success_and_retries_failure(monkeypatch):
    async def run():
        engine,factory,_api=await fixture()
        monkeypatch.setattr(operations,'config',SimpleNamespace(training_timezone='Europe/Moscow',admin_ids=[6000000001]))
        bot=SimpleNamespace(send_document=AsyncMock())
        now=datetime(2026,9,22,0,15,tzinfo=UTC)
        key='LAST_AUTOMATIC_BACKUP_DATE'
        try:
            bot.send_document.side_effect=TelegramBadRequest(method=SendDocument(chat_id=6000000001,document='fixture'),message='Fixture temporary failure')
            async with factory() as session:
                assert await operations.maybe_send_daily_backup(session,bot,now)==0
                await session.commit()
                assert await session.get(Setting,key) is None
            bot.send_document.side_effect=None
            async with factory() as session:
                assert await operations.maybe_send_daily_backup(session,bot,now)==1
                await session.commit()
                assert (await session.get(Setting,key)).value=='2026-09-22'
                assert await operations.maybe_send_daily_backup(session,bot,now)==0
                assert await operations.maybe_send_daily_backup(session,bot,now.replace(hour=5))==0
            assert bot.send_document.await_count==2
            attachment=bot.send_document.await_args.args[1]
            assert json.loads(attachment.data)['bookings'][0]['shoot_time']=='12:00:00'
        finally:
            await engine.dispose()
    asyncio.run(run())

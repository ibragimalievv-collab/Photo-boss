import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from test_operational_reset import fixture as reset_fixture
from test_workflow_replay import ACTOR, packet, setup
from test_workflow_services import booking_data

from app.attendance import Attendance
from app.miniapp_security import AccessError
from app.models import PhotoStorage, Shooting, User
from app.operational_reset import ResetError, inspect_archive
from app.sales_reset import reset_sales_fines, startup_sales_reset
from app.services.bookings import create_booking_record
from app.storage_inventory import full_folder_audit, selected_folder_audit
from app.workflow import Workflow
from app.yandex_disk import YandexDisk, YandexDiskError


@pytest.mark.parametrize('raw_request', ['{}', '[]', '{', '{"scope":"all-operational-history","requestId":"unique-id-01"}'])
def test_reset_scope_is_strict(raw_request):
    with pytest.raises(ResetError):
        asyncio.run(startup_sales_reset(None, raw_request, {1}))


def test_scoped_reset_archives_sales_and_fines_but_preserves_media_attendance_and_bank():
    async def run():
        engine = await reset_fixture()
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM settings WHERE key='operations:maintenance'"))
                await conn.execute(text('UPDATE shift_check_ins SET fine_amount=500,late=true'))
                kept = {}
                for name in ['shift_check_outs', 'work_breaks', 'delivery_photos', 'sale_draft_photos',
                             'photos', 'photo_storage', 'bank_payments', 'bank_reconciliations', 'shifts', 'work_chat_messages']:
                    kept[name] = (await conn.execute(text(f'SELECT * FROM {name}'))).all()
            result = await reset_sales_fines(engine, 'sales-fines-test-0001', {8000000001})
            assert result['row_counts']['sales'] == 1
            assert result['row_counts']['payroll_entries'] == 3
            assert (await inspect_archive(engine, 'sales-fines-test-0001'))['archive_sha256'] == result['archive_sha256']
            async with engine.connect() as conn:
                for name, rows in kept.items():
                    assert (await conn.execute(text(f'SELECT * FROM {name}'))).all() == rows
                assert await conn.scalar(text('SELECT count(*) FROM sales')) == 0
                assert await conn.scalar(text("SELECT count(*) FROM payroll_entries WHERE kind LIKE '%штраф%'")) == 0
                assert await conn.scalar(text('SELECT fine_amount FROM shift_check_ins LIMIT 1')) == 0
                assert await conn.scalar(text('SELECT late FROM shift_check_ins LIMIT 1'))
                assert await conn.scalar(text('SELECT status FROM sale_drafts WHERE id=13')) == 'CANCELLED'
                assert await conn.scalar(text('SELECT status FROM bookings WHERE id=10')) == 'READY_FOR_SALE'
                assert await conn.scalar(text('SELECT status FROM receipts WHERE id=16')) == 'APPROVED'
            async with engine.begin() as conn:
                await conn.execute(text("INSERT INTO payroll_entries(user_id,kind,amount,period,created_at) VALUES (2,'Новый штраф',-500,'2026-10',CURRENT_TIMESTAMP)"))
            again = await reset_sales_fines(engine, 'sales-fines-test-0001', set())
            assert again['replayed']
            async with engine.connect() as conn:
                assert await conn.scalar(text("SELECT count(*) FROM payroll_entries WHERE kind='Новый штраф'")) == 1
                assert not (await conn.exec_driver_sql('PRAGMA foreign_key_check')).all()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_sales_epoch_blocks_stale_sale_packets_and_keeps_other_operations():
    async def run():
        engine, _factory, work = await setup()
        try:
            async with engine.begin() as conn:
                await conn.execute(text("INSERT INTO settings(key,value) VALUES ('sales:generation','new-sale-epoch')"))
            with pytest.raises(AccessError, match='Продажи обнулены'):
                await work.execute(ACTOR, packet('old-sale-packet', 'sale_start', {'booking':{'id':1},'price':'400.00'}))
            result = await work.execute(ACTOR, packet('fresh-booking', 'booking', booking_data()|{'photographer_id':None})|{'salesGeneration':'new-sale-epoch'})
            assert json.loads(result.text)['bookingId'] > 0
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_directory_pagination_never_counts_only_first_page_or_unavailable_as_empty():
    disk = YandexDisk('fixture')
    disk._json = AsyncMock(side_effect=[{'type':'dir','_embedded':{'total':3,'items':[{'name':'a'}, {'name':'b'}]}},
                                      {'type':'dir','_embedded':{'total':3,'items':[{'name':'c'}]}}])
    assert len(asyncio.run(disk.list_directory('app:/PhotoBoss/test', page_size=2))) == 3
    assert disk._json.await_args.kwargs['params']['offset'] == 2
    disk._json = AsyncMock(return_value={'type':'dir','_embedded':{'total':1,'items':[]}})
    with pytest.raises(YandexDiskError):
        asyncio.run(disk.list_directory('app:/PhotoBoss/test'))


def test_folder_audit_discovers_unregistered_files_exact_duplicates_and_mismatches():
    a, b = 'a'*64, 'b'*64
    def item(digest, ext='jpg'):
        return {'type':'file','name':digest+'.'+ext,'size':123,'sha256':digest}
    storage = SimpleNamespace(list_directory=AsyncMock(return_value=[item(a),item(a,'png'),item(b)]))
    rows = [SimpleNamespace(sha256=a,byte_size=123)]
    result = asyncio.run(selected_folder_audit(storage, 1, rows, 3))
    assert result['diskFiles'] == 3 and result['verifiedUniqueFiles'] == 2
    assert result['exactDuplicateFiles'] == 1 and result['unregisteredFiles'] == 1
    assert result['missingSelected'] == 1 and not result['matchesDeclared']
    storage.list_directory.side_effect = YandexDiskError('HTTP 503')
    assert asyncio.run(selected_folder_audit(storage,1,rows,3))['diskFiles'] is None


def test_full_shoot_count_excludes_selected_copies():
    async def listing(path):
        if path.endswith('booking-1'):
            return [{'type':'dir','name':'shoot'}]
        if path.endswith('/shoot'):
            return [{'type':'dir','name':'Все фото'},{'type':'dir','name':'Выбранные'}]
        return [{'type':'file','name':'a.jpg','size':123,'sha256':'a'*64}]
    result = asyncio.run(full_folder_audit(SimpleNamespace(list_directory=listing),1,None,1))
    assert result['uniqueFiles'] == 1 and result['matchesDeclared']


def test_direct_shoot_upload_is_confirmed_immediately_without_telegram_and_replays_once(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_NOTIFICATION_CHANNEL','app')
    raw = b'\xff\xd8\xff'+b'x'*200
    path = 'app:/PhotoBoss/shootings/1/'+hashlib.sha256(raw).hexdigest()+'.jpg'
    saved = AsyncMock(return_value=path)
    monkeypatch.setattr(Workflow,'save_disk_original',saved)
    async def run():
        engine,factory,work = await setup()
        work.api.bot = SimpleNamespace(send_photo=AsyncMock(),send_document=AsyncMock())
        try:
            async with factory() as s:
                booking = await create_booking_record(s,await s.get(User,1),booking_data())
                booking.status='READY_FOR_SALE'
                booking.photographer_id=2
                shoot = await s.scalar(select(Shooting).where(Shooting.booking_id==booking.id))
                shoot.status='READY_FOR_SALE'
                await s.commit()
            body = packet('direct-full-original','shoot_photo',{'shooting':{'id':shoot.id}})|{'actorId':2}
            for packet_key in ['direct-full-original','duplicate-full-original']:
                result = json.loads((await work.execute({'id':2,'roles':['PHOTOGRAPHER']},body|{'key':'workflow-request-'+packet_key},raw)).text)
                assert result['storageStatus']=='STORED' and result['storageVerified']
            async with factory() as s:
                row = await s.scalar(select(PhotoStorage))
                assert row.status=='STORED' and row.disk_path==path and row.byte_size==len(raw)
                assert len(list(await s.scalars(select(PhotoStorage))))==1
            work.api.bot.send_photo.assert_not_awaited()
            work.api.bot.send_document.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_app_receipts_and_attendance_store_directly_without_telegram(monkeypatch):
    monkeypatch.setenv('PHOTO_BOSS_NOTIFICATION_CHANNEL','app')
    saved = AsyncMock(return_value=('app:/PhotoBoss/web-media/1/hash.jpg','hash'))
    monkeypatch.setattr('app.services.media_files.save_private_media', saved)
    bot=SimpleNamespace(send_photo=AsyncMock(),send_document=AsyncMock())
    assert asyncio.run(Workflow(SimpleNamespace(bot=bot)).save_telegram(SimpleNamespace(id=1),b'photo',receipt=True))[0].startswith('app:')
    assert asyncio.run(Attendance(SimpleNamespace(bot=bot)).send_photo({'id':1},b'photo','start')).startswith('app:')
    bot.send_photo.assert_not_awaited()
    bot.send_document.assert_not_awaited()


def test_existing_disk_original_is_verified_without_reupload(monkeypatch):
    monkeypatch.setenv('YANDEX_DISK_TOKEN','fixture')
    raw=b'\xff\xd8\xff'+b'original'*50
    disk=SimpleNamespace(metadata=AsyncMock(return_value={'type':'file','size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}),upload_bytes=AsyncMock())
    monkeypatch.setattr('app.workflow.YandexDisk',lambda *a:disk)
    path=asyncio.run(Workflow(None).save_disk_original('/shootings/1',raw))
    assert path.startswith('app:/PhotoBoss/shootings/1/')
    disk.upload_bytes.assert_not_awaited()


def test_lunch_return_after_midnight_can_close_own_open_interval(monkeypatch):
    from datetime import UTC, date, datetime

    from app.models import ShiftCheckIn, WorkBreak
    from app.work_breaks import WorkBreaks
    now=datetime(2026,9,22,22,tzinfo=UTC).replace(tzinfo=None)
    monkeypatch.setattr('app.work_breaks.utc_now',lambda:now)
    async def run():
        engine,factory,work=await setup()
        work.api.today=lambda:date(2026,9,23)
        try:
            async with factory() as s:
                s.add_all([ShiftCheckIn(user_id=1,shift_date=date(2026,9,22),status='STARTED',started_at=datetime(2026,9,22,18,tzinfo=UTC).replace(tzinfo=None)),
                    WorkBreak(user_id=1,hotel_id=1,shift_date=date(2026,9,22),started_at=datetime(2026,9,22,20,50,tzinfo=UTC).replace(tzinfo=None))])
                await s.commit()
            async with engine.begin() as conn:
                result=await WorkBreaks(work.api).record(conn,ACTOR,date(2026,9,23),'break_end',{'hotelId':1})
                assert result['endedAt']=='2026-09-22T22:00:00Z'
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_count_includes_purchased_originals_when_full_folder_is_missing():
    async def listing(path):
        if path.endswith('/sales/draft-8/selected'):
            return [{'type':'file','name':'a.jpg','size':123,'sha256':'a'*64},
                    {'type':'file','name':'b.jpg','size':123,'sha256':'b'*64}]
        raise YandexDiskError('HTTP 404')
    result=asyncio.run(full_folder_audit(SimpleNamespace(list_directory=listing),34,1,2,[8]))
    assert result['uniqueFiles']==2 and result['missingOriginals']==0 and result['matchesDeclared']


def test_owner_can_clear_booking_list_and_default_hotel_without_losing_files_or_attendance():
    from app.models import Hotel
    async def run():
        engine=await reset_fixture()
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM settings WHERE key='operations:maintenance'"))
                await conn.execute(Hotel.__table__.insert().values(id=2,name='Основной отель',active=True))
                await conn.execute(text('UPDATE bookings SET hotel_id=2 WHERE id=10'))
            result=await reset_sales_fines(engine,'clear-bookings-hotel-332',{8000000001},
                hide_hotel_name='Основной отель',clear_bookings=True)
            assert result['row_counts']['hidden_hotels']==1 and result['row_counts']['cleared_bookings']==1
            async with engine.connect() as conn:
                assert not await conn.scalar(text('SELECT active FROM hotels WHERE id=2'))
                assert await conn.scalar(text('SELECT active FROM hotels WHERE id=1'))
                assert await conn.scalar(text('SELECT hotel_id FROM bookings WHERE id=10'))==2
                assert await conn.scalar(text("SELECT count(*) FROM bookings b WHERE NOT EXISTS (SELECT 1 FROM settings st WHERE st.key='booking:trash:' || CAST(b.id AS VARCHAR))"))==0
                assert await conn.scalar(text('SELECT count(*) FROM photos'))==1
                assert await conn.scalar(text('SELECT count(*) FROM shift_check_ins'))==1
        finally:
            await engine.dispose()
    asyncio.run(run())

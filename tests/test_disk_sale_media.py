import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from test_disk_folder_audit import ReadDisk
from test_team import Request
from test_workflow_services import booking_data, fixture

from app.disk_sale_media import (
    BINDING,
    DiskSaleMedia,
    load_reference,
    parse_source,
    select_photo,
)
from app.miniapp_security import AccessError
from app.models import (
    Booking,
    OperationRequest,
    PayrollEntry,
    Receipt,
    Sale,
    SaleDraft,
    SaleDraftPhoto,
    Setting,
    User,
    UserRole,
)
from app.services.bookings import create_booking_record
from app.services.sale_workflow import (
    capture_draft_receipt,
    complete_sale,
    set_sale_counts,
    start_sale_draft,
)
from app.storage_inventory import selected_folder_audit
from app.workflow import Workflow
from app.yandex_disk import YandexDisk, YandexDiskError


@pytest.mark.parametrize('value,kind,root', [
    ('disk:/Hotel/Семья', 'private', 'disk:/Hotel/Семья'),
    ('https://disk.yandex.ru/client/disk/Hotel/%D0%A1%D0%B5%D0%BC%D1%8C%D1%8F', 'private', 'disk:/Hotel/Семья'),
    ('https://disk.yandex.ru/d/fixture123', 'public', '/'),
    ('https://yadi.sk/i/fixture_123', 'public', '/'),
])
def test_supported_sources(value, kind, root):
    source = parse_source(value)
    assert (source['kind'], source['root']) == (kind, root)


@pytest.mark.parametrize('value', ['http://disk.yandex.ru/d/test123', 'https://evil.example/d/test123',
    'https://disk.yandex.ru.evil.example/d/test123', 'https://name:secret@disk.yandex.ru/d/test123',
    'https://disk.yandex.ru:443/d/test123', 'https://disk.yandex.ru/d/test123?token=secret',
    'disk:/Hotel/../Other', 'https://disk.yandex.ru/client/disk/Hotel/%2e%2e/Other', [], None])
def test_links_cannot_become_arbitrary_download_urls(value):
    with pytest.raises(ValueError):
        parse_source(value)


async def prepared(monkeypatch, url='sqlite+aiosqlite:///:memory:'):
    engine, factory = await fixture(url)
    disk = ReadDisk()
    disk.disk_download_bytes = AsyncMock(side_effect=lambda path, **kwargs: disk.raw[path])
    async def metadata(path):
        if path in disk.folders:
            return {'type': 'dir', 'name': path.rsplit('/', 1)[-1]}
        raw = disk.raw[path]
        return {'type': 'file', 'name': path.rsplit('/', 1)[-1], 'size': len(raw),
                'sha256': hashlib.sha256(raw).hexdigest(), 'md5': hashlib.md5(raw, usedforsecurity=False).hexdigest()}
    disk.disk_metadata = AsyncMock(side_effect=metadata)
    monkeypatch.setattr('app.disk_sale_media.read_storage', lambda: disk)
    async with factory() as session:
        session.add_all([UserRole(user_id=1, role='OWNER'), User(id=3, tg_id=3, name='Other'), UserRole(user_id=3, role='MANAGER')])
        actor = await session.get(User, 1)
        booking = await create_booking_record(session, actor, booking_data())
        booking.status, booking.photographer_id = 'READY_FOR_SALE', 2
        await session.flush()
        draft = await start_sale_draft(session, actor, {'OWNER'}, booking.id)
        await capture_draft_receipt(session, actor, draft, 'receipt-test', 'receipt-unique-test', 'receipt-hash-test', {'status':'not_configured'})
        await set_sale_counts(session, actor, draft, 150, 2)
        await session.commit()
        bid, did = booking.id, draft.id
    api = SimpleNamespace(engine=engine, body=AsyncMock(side_effect=lambda r:r.body))
    feature = DiskSaleMedia(api)
    request = Request({'id':1, 'roles':['OWNER']}, {'source':'disk:/Hotel'}, cid=bid)
    request.method, request.query = 'POST', {}
    source = json.loads((await feature.folder(request)).text)
    return engine, factory, feature, request, source, disk, did


def test_private_folder_is_booking_scoped_and_staff_cannot_browse_other_data(monkeypatch):
    async def run():
        engine, _, feature, request, _, disk, _ = await prepared(monkeypatch)
        try:
            request.method, request.query = 'GET', {}
            data = json.loads((await feature.files(request)).text)
            assert data['root'] == 'disk:/Hotel'
            assert any(i['name'] == 'a.jpg' for i in data['items'])
            request.query = {'path':'disk:/HotelElse/secret.jpg'}
            with pytest.raises(AccessError): await feature.files(request)
            before = disk.disk_list_directory.await_count
            request['miniapp_actor'] = {'id':3,'roles':['MANAGER']}
            request.query = {}
            with pytest.raises(AccessError): await feature.files(request)
            assert disk.disk_list_directory.await_count == before
            request['miniapp_actor'] = {'id':1,'roles':['MANAGER']}
            request.method = 'POST'
            with pytest.raises(AccessError, match='владелец'): await feature.folder(request)
            disk.upload_bytes.assert_not_awaited()
            disk.delete.assert_not_awaited()
        finally: await engine.dispose()
    asyncio.run(run())


def test_read_only_selection_completes_one_sale_and_rejects_duplicate_frames(monkeypatch):
    async def run():
        engine, factory, _, _request, source, disk, did = await prepared(monkeypatch)
        try:
            async with factory() as session:
                actor, draft = await session.get(User,1), await session.get(SaleDraft,did)
                booking = await session.get(Booking,draft.booking_id)
                first = await select_photo(session,actor,booking,draft,{'revision':source['revision'],'path':'disk:/Hotel/a.jpg'})
                same = await select_photo(session,actor,booking,draft,{'revision':source['revision'],'path':'disk:/Hotel/Family/copy.jpg'})
                assert same['duplicate'] and same['photoId'] == first['photoId']
                assert await session.scalar(select(func.count(SaleDraftPhoto.id))) == 1
                await select_photo(session,actor,booking,draft,{'revision':source['revision'],'path':'disk:/Hotel/Family/b.jpg'})
                sale = await complete_sale(session,actor,draft.id)
                assert sale.sale.amount == 800 and sale.sale.sold_photos == 2
                await session.commit()
                assert await session.scalar(select(func.count(Sale.id))) == 1
                with pytest.raises(ValueError, match='завершена'): await complete_sale(session,actor,draft.id)
                refs = list(await session.scalars(select(SaleDraftPhoto)))
                assert all(p.storage_path.startswith('disk-ref:') for p in refs)
                assert await load_reference(session,refs[0].storage_path) == disk.raw['disk:/Hotel/a.jpg']
            storage = SimpleNamespace(list_directory=AsyncMock(return_value=[]))
            status = await selected_folder_audit(storage,did,refs,2,engine=engine)
            assert status['matchesDeclared'] and status['registeredVerified'] == 2
            disk.upload_bytes.assert_not_awaited()
            disk.delete.assert_not_awaited()
        finally: await engine.dispose()
    asyncio.run(run())


def test_workflow_disk_last_photo_finishes_sale_without_browser_completion(monkeypatch):
    """Closing after the last Disk frame cannot leave a fully uploaded draft."""
    from datetime import date
    from zoneinfo import ZoneInfo

    from sqlalchemy import text
    from test_workflow_replay import packet

    from app.workday import Workday

    async def run():
        engine, factory, _, _, source, disk, did = await prepared(monkeypatch)
        try:
            async def rows(conn, sql, **params):
                return list((await conn.execute(text(sql.replace(' FOR UPDATE', '')), params)).mappings())

            work = Workday(SimpleNamespace(engine=engine, rows=rows, day=date.fromisoformat,
                today=lambda: date(2026, 9, 22), tz=ZoneInfo('Europe/Moscow')))
            actor = {'id': 1, 'roles': ['OWNER']}
            first = packet('disk-first-frame', 'sale_disk_selected', {'draft': {'id': did},
                'revision': source['revision'], 'path': 'disk:/Hotel/a.jpg'})
            last = packet('disk-last-frame', 'sale_disk_selected', {'draft': {'id': did},
                'revision': source['revision'], 'path': 'disk:/Hotel/Family/b.jpg'})
            assert json.loads((await work.execute(actor, first)).text)['draftStatus'] == 'AWAITING_SELECTED'
            finished = json.loads((await work.execute(actor, last)).text)
            assert finished['draftStatus'] == 'COMPLETED'
            assert finished['selectedPhotos'] == 2 and finished['amount'] == '800.00'
            assert json.loads((await work.execute(actor, last)).text) == finished
            completion = packet('disk-browser-final-command', 'sale_complete',
                {'draft': {'id': did}, 'price': '400.00'})
            assert json.loads((await work.execute(actor, completion)).text)['saleId'] == finished['saleId']
            async with factory() as session:
                assert await session.scalar(select(func.count(Sale.id))) == 1
                assert await session.scalar(select(func.count(Receipt.id))) == 1
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
                assert await session.scalar(select(func.count(SaleDraftPhoto.id))) == 2
            disk.upload_bytes.assert_not_awaited()
            disk.delete.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_rejected_stale_folder_reference_can_reselect_under_same_key_without_changing_accepted_data(monkeypatch):
    """Only definitive pre-ledger folder validation may change its saved payload."""
    from datetime import date
    from zoneinfo import ZoneInfo

    from sqlalchemy import text
    from test_workflow_replay import packet

    from app.workday import Workday

    async def run():
        engine, factory, feature, request, source, disk, did = await prepared(monkeypatch)
        try:
            async def rows(conn, sql, **params):
                return list((await conn.execute(text(sql.replace(' FOR UPDATE', '')), params)).mappings())

            work = Workday(SimpleNamespace(engine=engine, rows=rows, day=date.fromisoformat,
                today=lambda: date(2026, 9, 22), tz=ZoneInfo('Europe/Moscow')))
            actor = {'id': 1, 'roles': ['OWNER']}
            stale = packet('stale-disk-reference', 'sale_disk_selected', {'draft': {'id': did},
                'revision': source['revision'], 'path': 'disk:/Hotel/a.jpg'})
            fresh = json.loads((await feature.folder(request)).text)
            assert fresh['revision'] != source['revision']
            with pytest.raises(AccessError, match='Папка съёмки изменилась') as error:
                await work.execute(actor, stale)
            assert error.value.status == 409
            async with factory() as session:
                assert await session.scalar(select(func.count(OperationRequest.id))) == 0
                assert await session.scalar(select(func.count(Sale.id))) == 0
                assert await session.scalar(select(func.count(SaleDraftPhoto.id))) == 0
                assert await session.scalar(select(func.count(Receipt.id))) == 0
                saved_draft = await session.get(SaleDraft, did)
                assert saved_draft.receipt_file_id == 'receipt-test'
            corrected = stale | {'data': stale['data'] | {'revision': fresh['revision']}}
            first = json.loads((await work.execute(actor, corrected)).text)
            assert json.loads((await work.execute(actor, corrected)).text) == first
            with pytest.raises(AccessError, match='Ключ уже использован'):
                await work.execute(actor, corrected | {'data': corrected['data'] | {'path': 'disk:/Hotel/Family/b.jpg'}})
            second = packet('fresh-disk-second', 'sale_disk_selected', {'draft': {'id': did},
                'revision': fresh['revision'], 'path': 'disk:/Hotel/Family/b.jpg'})
            result = json.loads((await work.execute(actor, second)).text)
            assert result['draftStatus'] == 'COMPLETED'
            assert json.loads((await work.execute(actor, second)).text) == result
            async with factory() as session:
                assert await session.scalar(select(func.count(OperationRequest.id))) == 2
                assert await session.scalar(select(func.count(Sale.id))) == 1
                assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
                assert await session.scalar(select(func.count(Receipt.id))) == 1
                assert await session.scalar(select(func.count(SaleDraftPhoto.id))) == 2
            disk.delete.assert_not_awaited()
            disk.upload_bytes.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['file','account','folder'])
def test_changes_are_rechecked_before_sale_and_never_change_money(monkeypatch,change):
    async def run():
        engine,factory,_,_,source,disk,did = await prepared(monkeypatch)
        try:
            async with factory() as session:
                actor,draft = await session.get(User,1),await session.get(SaleDraft,did)
                booking = await session.get(Booking,draft.booking_id)
                await select_photo(session,actor,booking,draft,{'revision':source['revision'],'path':'disk:/Hotel/a.jpg'})
                await select_photo(session,actor,booking,draft,{'revision':source['revision'],'path':'disk:/Hotel/Family/b.jpg'})
                if change == 'file':
                    disk.raw['disk:/Hotel/a.jpg'] = disk.raw['disk:/Hotel/Family/b.jpg']
                elif change == 'account':
                    disk.token = 'different-account-token'
                else:
                    row = await session.get(Setting,BINDING+str(booking.id))
                    binding = json.loads(row.value);binding['revision']='new-source';row.value=json.dumps(binding)
                    with pytest.raises(ValueError,match='изменилась'):
                        await select_photo(session,actor,booking,draft,{'revision':source['revision'],'path':'disk:/Hotel/a.jpg'})
                    return
                with pytest.raises(ValueError): await complete_sale(session,actor,did)
                assert await session.scalar(select(func.count(Sale.id))) == 0
        finally: await engine.dispose()
    asyncio.run(run())


def test_public_links_use_only_official_read_api_and_report_permission_refusal(monkeypatch):
    async def run():
        engine,_,feature,request,_,disk,_ = await prepared(monkeypatch)
        meta=AsyncMock(return_value={'type':'dir','name':'Guest photos'})
        monkeypatch.setattr(YandexDisk,'public_metadata',meta)
        try:
            request['miniapp_actor']={'id':1,'roles':['MANAGER']}
            request.body={'source':'https://disk.yandex.ru/d/fixture123'}
            result=json.loads((await feature.folder(request)).text)
            assert result['connected'] and result['kind']=='public'
            assert meta.await_args.args == ('https://disk.yandex.ru/d/fixture123','/')
            meta.side_effect=YandexDiskError('Yandex.Disk HTTP 403: ForbiddenError',http_status=403)
            with pytest.raises(AccessError,match='не разрешил чтение'): await feature.folder(request)
            disk.upload_bytes.assert_not_awaited()
        finally: await engine.dispose()
    asyncio.run(run())


def test_directory_count_changes_cannot_be_reported_as_a_complete_listing():
    disk=YandexDisk('synthetic')
    disk._json=AsyncMock(side_effect=[{'type':'dir','_embedded':{'total':3,'items':[{'name':'a'},{'name':'b'}]}},
        {'type':'dir','_embedded':{'total':4,'items':[{'name':'c'},{'name':'d'}]}}])
    with pytest.raises(YandexDiskError,match='изменился'):
        asyncio.run(disk.disk_list_directory('disk:/Hotel',page_size=2))


def test_only_owner_can_resume_another_employees_draft_without_losing_data(monkeypatch):
    async def run():
        engine,factory,feature,request,source,_disk,did=await prepared(monkeypatch)
        try:
            async with factory() as session:
                draft=await session.get(SaleDraft,did)
                booking=await session.get(Booking,draft.booking_id)
                await select_photo(session,await session.get(User,1),booking,draft,
                                   {'revision':source['revision'],'path':'disk:/Hotel/a.jpg'})
                draft.created_by_id=2
                original=(draft.receipt_file_id,draft.pricing_snapshot,draft.sold_photos)
                await session.commit()
            request.body={}
            request['miniapp_actor']={'id':3,'roles':['MANAGER']}
            with pytest.raises(AccessError): await feature.takeover(request)
            request['miniapp_actor']={'id':1,'roles':['OWNER']}
            result=json.loads((await feature.takeover(request)).text)
            assert result['draftId']==did and result['selectedPhotos']==1 and result['changed']
            assert not json.loads((await feature.takeover(request)).text)['changed']
            async with factory() as session:
                draft=await session.get(SaleDraft,did)
                assert draft.created_by_id==1
                assert (draft.receipt_file_id,draft.pricing_snapshot,draft.sold_photos)==original
                assert await session.scalar(select(func.count(SaleDraftPhoto.id)))==1
                assert await session.scalar(select(func.count(Sale.id)))==0
                with pytest.raises(AccessError,match='Нет доступа'):
                    await Workflow(SimpleNamespace()).sale(session,await session.get(User,2),{'PHOTOGRAPHER'},
                        'sale_counts',{'draft':{'id':did},'total':150,'sold':2,'price':'400.00'},None)
        finally: await engine.dispose()
    asyncio.run(run())


def test_reselecting_same_original_repairs_old_storage_reference_without_counting_again(monkeypatch):
    async def run():
        engine,factory,_,_,source,disk,did=await prepared(monkeypatch)
        try:
            async with factory() as session:
                raw=disk.raw['disk:/Hotel/a.jpg'];digest=hashlib.sha256(raw).hexdigest()
                photo=SaleDraftPhoto(draft_id=did,telegram_file_id='app:/PhotoBoss/old.jpg',
                    telegram_unique_id='old-same-photo',storage_path='app:/PhotoBoss/old.jpg',sha256=digest,byte_size=len(raw))
                session.add(photo);await session.flush();pid=photo.id
                draft=await session.get(SaleDraft,did)
                result=await select_photo(session,await session.get(User,1),await session.get(Booking,draft.booking_id),draft,
                                         {'revision':source['revision'],'path':'disk:/Hotel/a.jpg'})
                assert result['duplicate'] and result['photoId']==pid
                assert photo.storage_path.startswith('disk-ref:')
                assert await load_reference(session,photo.storage_path)==raw
                assert await session.scalar(select(func.count(SaleDraftPhoto.id)))==1
                assert await session.scalar(select(func.count(Sale.id)))==0
        finally: await engine.dispose()
    asyncio.run(run())

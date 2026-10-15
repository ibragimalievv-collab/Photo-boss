"""Mobile HTTPS test: batch, offline reopen, dedupe, quota, retry, processing and ACL."""
import asyncio
import hashlib
import json
import os
import ssl
import tempfile
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from sqlalchemy import func, select
from test_miniapp_release import TOKEN, signed
from test_workflow_services import booking_data, fixture
from work_calls_browser_check import wait_async

from app import photo_edits
from app.academy_practice import install_academy_practice
from app.attendance import install_attendance
from app.miniapp_api import install_miniapp
from app.models import (
    Photo,
    PhotoEdit,
    PhotoStorage,
    Shift,
    ShiftCheckIn,
    ShiftCheckOut,
    Shooting,
    User,
)
from app.people import install_people
from app.services import photo_storage
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS
from app.services.bookings import create_booking_record
from app.services.shifts import shift_now
from app.work_calls import install_work_calls
from app.work_chat import install_work_chat
from app.yandex_disk import YandexDiskError


async def main():
    with tempfile.TemporaryDirectory() as directory:
        engine, factory = await fixture('sqlite+aiosqlite:///' + directory + '/qa.db')
        async with factory() as session:
            local = shift_now()
            booking = await create_booking_record(session, await session.get(User, 1),
                booking_data() | {'shoot_date': local.date().isoformat()})
            booking.photographer_id = 2
            booking.status = 'ASSIGNED'
            (await session.scalar(select(Shooting))).status = 'ASSIGNED'
            session.add(Shift(id=1, user_id=2, hotel_id=1,
                start_at=datetime.combine(local.date(), time(9), tzinfo=local.tzinfo).astimezone(UTC).replace(tzinfo=None),
                end_at=datetime.combine(local.date(), time(23, 59), tzinfo=local.tzinfo).astimezone(UTC).replace(tzinfo=None)))
            await session.commit()
        media = {}
        async def upload(uid, file, **kwargs):
            digest = hashlib.sha256(file.data).hexdigest()
            media[digest] = file.data
            attachment = SimpleNamespace(file_id=digest, file_unique_id=digest)
            return SimpleNamespace(document=attachment, photo=[attachment])
        async def download(fid, destination, **kwargs): destination.write(media[fid])
        bot = SimpleNamespace(token=TOKEN, send_photo=upload, send_document=upload, download=download, send_message=AsyncMock(), me=AsyncMock(return_value=SimpleNamespace(username='fixture_bot')))
        class Disk:
            def __init__(self):
                self.state, self.files, self.full = {'connected':True}, {}, True
            async def ensure_dir(self, path): pass
            async def upload_bytes(self, path, data, **kwargs):
                if self.full: raise YandexDiskError('HTTP 507')
                self.files[path] = data
            async def download_bytes(self, path): return self.files[path]
        disk = Disk()
        app = web.Application(client_max_size=25*1024*1024)
        app['yandex_disk'] = disk
        api = install_miniapp(app, engine=engine, bot=bot, lessons=ACADEMY_LESSONS, blocks=ACADEMY_BLOCKS)
        original_rows = api.rows
        async def rows(conn, sql, **params): return await original_rows(conn, sql.replace(' FOR UPDATE', ''), **params)
        api.rows = rows
        install_attendance(app, api)
        install_people(app, api)
        chat = install_work_chat(app, api)
        install_work_calls(app, chat)
        install_academy_practice(app, api)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(os.environ['WORKFLOW_TEST_CERT'], os.environ['WORKFLOW_TEST_KEY'])
        server = TestServer(app)
        await server.start_server(ssl=tls)
        errors = []
        try:
            ai_check = AsyncMock(side_effect=[
                {'status': 'REJECTED', 'note': 'Fixture: стопы не видны. Переснимите в полный рост.'},
                {'status': 'ACCEPTED', 'note': 'Fixture: один человек целиком от головы до обуви.'},
            ])
            with patch.object(photo_storage, 'Session', factory), patch.object(photo_edits, 'Session', factory), patch('app.attendance.analyze_checkin', ai_check):
                async with async_playwright() as p:
                    browser = await p.chromium.launch(executable_path=os.getenv('CALLS_CHROMIUM_PATH') or None,
                        args=['--ignore-certificate-errors', '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'])
                    context = await browser.new_context(ignore_https_errors=True, viewport={'width':360,'height':800}, is_mobile=True,
                        geolocation={'latitude': 41.5, 'longitude': 48.1}, permissions=['geolocation', 'camera'])
                    await context.add_init_script('window.Telegram={WebApp:{initData:'+json.dumps(signed(2))+',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}}};')
                    await context.route('https://telegram.org/**', lambda r:r.fulfill(status=200,content_type='text/javascript',body=''))
                    lost = False
                    async def lose_upload_response(route):
                        nonlocal lost
                        if not lost:
                            response = await route.fetch()
                            assert response.status == 200
                            lost = True
                            await route.abort('connectionreset')
                        else:
                            await route.continue_()
                    await context.route('**/api/miniapp/operations/media', lose_upload_response)
                    page = await context.new_page()
                    page.on('pageerror', lambda e:errors.append(str(e)))
                    async def refresh_files():
                        old = await page.get_by_role('button',name='Обновить состояние файлов').element_handle()
                        await old.click()
                        await old.wait_for_element_state('hidden')
                    url = str(server.make_url('/app/#workflow'))
                    await page.goto(str(server.make_url('/app/#home')))
                    await page.get_by_role('heading', name='Всё под контролем', exact=True).wait_for()
                    await page.locator('[data-booking="1"]').first.click()
                    await page.locator('[data-shoot-action=pickup]').click()
                    await page.locator('[data-shoot-action=start]').click()
                    await page.get_by_text('Сначала подтвердите начало смены', exact=False).wait_for()
                    async with factory() as session:
                        assert (await session.scalar(select(Shooting))).status == 'PICKED_UP'
                    await page.locator('#closeSheet').click()
                    await page.locator('#attendanceCard button').click()
                    dialog = page.locator('#attendanceDialog')
                    await dialog.locator('[data-att=location]').click()
                    for expected in ('AWAITING_PHOTO', 'STARTED'):
                        await dialog.locator('[data-att=camera]:not([disabled])').click()
                        await wait_async(page, "()=>document.querySelector('#attCameraVideo')?.videoWidth>0")
                        await dialog.locator('[data-att=snap]').click()
                        await dialog.locator('[data-att=confirm]').click()
                        if expected == 'AWAITING_PHOTO':
                            await dialog.get_by_text('AI: Fixture: стопы не видны.', exact=False).wait_for()
                        else:
                            await dialog.get_by_role('heading', name='Вы на смене', exact=True).wait_for()
                        async with factory() as session:
                            check = await session.scalar(select(ShiftCheckIn))
                            assert check.status == expected and check.scheduled_shift_id == 1
                            if expected == 'AWAITING_PHOTO':
                                assert check.started_at is None and check.ai_status == 'REJECTED'
                    assert ai_check.await_count == 2
                    await dialog.locator('[data-att=close]').click()
                    await page.locator('[data-booking="1"]').first.click()
                    for action in ('start', 'finish', 'processing'):
                        await page.locator(f'[data-shoot-action={action}]').click()
                    assert await page.locator('[data-shoot-action=ready]').is_disabled()
                    tomorrow = shift_now() + timedelta(days=1)
                    await page.locator('#viewingAppointment [name=viewingAt]').fill(tomorrow.strftime('%Y-%m-%dT%H:%M'))
                    await page.locator('#viewingAppointment [name=comment]').fill('Fixture: гость вернётся с экскурсии завтра')
                    await page.locator('#viewingAppointment button').click()
                    await page.locator('[data-shoot-action=ready]:not([disabled])').click()
                    async with factory() as session:
                        shooting = await session.scalar(select(Shooting))
                        assert shooting.status == 'READY_FOR_SALE' and shooting.viewing_comment
                    await page.locator('#closeSheet').click()
                    await page.goto(url)
                    await page.get_by_role('heading',name='Рабочие операции',exact=True).wait_for()
                    await page.evaluate('navigator.serviceWorker.ready')
                    await wait_async(page,'()=>navigator.serviceWorker.controller!==null')
                    await context.set_offline(True)
                    await page.get_by_text('Загрузить полную съёмку',exact=True).click()
                    await page.locator('#workflowShoot [name=photos]').set_input_files(['app/webapp/assets/academy/coast.jpg','app/webapp/assets/academy/family.jpg','app/webapp/assets/academy/coast.jpg'])
                    await page.locator('#workflowShoot button').click()
                    await wait_async(page,"async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).length===3;}")
                    await page.close()
                    page = await context.new_page()
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    await page.goto(url)
                    await page.get_by_role('heading',name='Рабочие операции',exact=True).wait_for()
                    await context.set_offline(False)
                    await wait_async(page,"async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).some(r=>r.status==='local'&&r.error);}")
                    assert lost
                    await page.get_by_role('button',name='Синхронизировать сейчас').click()
                    await wait_async(page,"async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).every(r=>r.status==='synced');}")
                    async with factory() as session:
                        assert await session.scalar(select(func.count(Photo.id))) == 2
                        assert await session.scalar(select(func.count(PhotoStorage.id))) == 2
                    await photo_storage.sync_one(bot,disk)
                    await photo_storage.sync_one(bot,disk)
                    await refresh_files()
                    await page.get_by_text('Съёмка №1',exact=False).filter(has_text='сохранено').click()
                    await page.get_by_role('button',name='Повторить только неудачные загрузки').click()
                    await wait_async(page,"async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).every(r=>r.status==='synced');}")
                    disk.full = False
                    await photo_storage.sync_one(bot,disk)
                    await photo_storage.sync_one(bot,disk)
                    originals = dict(disk.files)
                    await refresh_files()
                    await page.get_by_text('Съёмка №1',exact=False).filter(has_text='сохранено').click()
                    await page.get_by_text('Обработка · оригиналы сохраняются отдельно',exact=True).click()
                    await page.locator('summary').filter(has_text='Кадр №1').click()
                    await page.locator('.photo-edit[data-photo="1"] button').click()
                    await wait_async(page,"async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).every(r=>r.status==='synced');}")
                    await photo_edits.process_one(disk)
                    await refresh_files()
                    await page.get_by_text('Съёмка №1',exact=False).filter(has_text='сохранено').click()
                    await page.get_by_text('Обработка · оригиналы сохраняются отдельно',exact=True).click()
                    await page.locator('summary').filter(has_text='Кадр №1').click()
                    await page.get_by_role('button',name='Сравнить с оригиналом').click()
                    await page.get_by_alt_text('Обработанная копия',exact=True).wait_for()
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    assert all(disk.files[path]==raw for path,raw in originals.items())
                    response = await context.request.get(str(server.make_url('/api/miniapp/photos/1/image')),headers={'X-Telegram-Init-Data':signed(1)})
                    assert response.status == 403
                    await page.get_by_role('button',name='Отменить результат').click()
                    await wait_async(page,"async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).every(r=>r.status==='synced');}")
                    async with factory() as session:
                        assert (await session.scalar(select(PhotoEdit))).status == 'CANCELLED'
                    await refresh_files()
                    await page.get_by_text('Загрузить полную съёмку', exact=True).click()
                    await page.locator('#workflowShoot [name=finish]').check()
                    await page.locator('#workflowShoot button').click()
                    await wait_async(page, "async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).every(r=>r.status==='synced');}")
                    async with factory() as session:
                        assert (await session.scalar(select(Shooting))).full_upload_completed_at is not None
                    await page.goto(str(server.make_url('/app/#home')))
                    await page.locator('#attendanceCard button').click()
                    dialog = page.locator('#attendanceDialog')
                    await dialog.locator('[data-att=location]').click()
                    await dialog.locator('[data-att=camera]:not([disabled])').click()
                    await wait_async(page, "()=>document.querySelector('#attCameraVideo')?.videoWidth>0")
                    await dialog.locator('[data-att=snap]').click()
                    await dialog.locator('[data-att=confirm]').click()
                    await page.get_by_role('heading', name='Итоги смены', exact=True).wait_for()
                    async with factory() as session:
                        end = await session.scalar(select(ShiftCheckOut))
                        assert end.status == 'FINISHED' and end.scheduled_shift_id == 1
                    denied = await context.request.post(str(server.make_url('/api/miniapp/bookings/1/shooting/viewing')),
                        headers={'X-Telegram-Init-Data': signed(2)}, data={
                            'viewingAt': tomorrow.isoformat(), 'comment': 'Fixture: действие после завершения смены',
                        })
                    assert denied.status == 409
                    assert ai_check.await_count == 2, 'Workplace close must not run the full-body AI check'
                    await page.goto(url)
                    await page.get_by_role('heading', name='Рабочие операции', exact=True).wait_for()
                    # Exercise a 300-MB queue without retaining every photo in status reads.
                    await context.set_offline(True)
                    await page.evaluate("""async()=>{
                        const m=await import('/app/js/outbox.js');m.setOutboxUser(2);
                        const bytes=new Uint8Array(2*1024*1024);bytes.fill(73);
                        await m.enqueueBatch(Array.from({length:150},(_,i)=>({
                            kind:'delivery_photo',date:'2026-10-08',data:{booking:1,filename:`frame-${i}.jpg`},
                            blob:new File([bytes],`frame-${i}.jpg`,{type:'image/jpeg'})
                        })));
                        const rows=(await m.outboxRows()).filter(r=>r.kind==='delivery_photo');
                        if(rows.length!==150||rows.some(r=>r.blob||!r.hasBlob))throw Error('Batch metadata retains files');
                        const {localAction}=await import('/app/js/localstore.js');
                        const row=await localAction('operations','readonly',s=>s.get(rows[0].key));
                        if(row.blob.size!==bytes.length)throw Error('Queued original missing');
                    }""")
                    attempts=[]
                    failed=False
                    async def bulk_upload(route):
                        nonlocal failed
                        body=route.request.post_data_buffer
                        assert 2*1024*1024 < len(body) < 2*1024*1024+4096
                        attempts.append(body.split(b'filename="',1)[1].split(b'"',1)[0])
                        if not failed:
                            failed=True
                            await route.fulfill(status=503,content_type='application/json',body='{"error":"temporary outage"}')
                        else:
                            await route.fulfill(status=200,content_type='application/json',body='{"ok":true}')
                    await context.route('**/api/miniapp/operations/media',bulk_upload)
                    await context.set_offline(False)
                    await wait_async(page,"async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).some(r=>r.error);}")
                    await page.evaluate("async()=>{const m=await import('/app/js/outbox.js');const row=(await m.outboxRows()).find(r=>r.error);await m.retryOperation(row.key);}")
                    await wait_async(page,"async()=>{const m=await import('/app/js/outbox.js');const rows=(await m.outboxRows()).filter(r=>r.kind==='delivery_photo');return rows.length===150&&rows.every(r=>r.status==='synced'&&!r.hasBlob);}")
                    assert len(attempts)==151 and len(set(attempts))==150
                    assert not errors,errors
                    await browser.close()
                    print('PASS: real browser camera/GPS with rejected then accepted AI fixture, scheduled shift, all shooting stages and viewing, offline batch/reopen, lost response replay, dedupe, quota recovery, original-preserving copy, complete upload, workplace close, post-close denial, 360px layout')
        finally:
            await server.close()
            await engine.dispose()


if __name__ == '__main__': asyncio.run(main())


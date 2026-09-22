"""Mobile HTTPS test: batch, offline reopen, dedupe, quota, retry, processing and ACL."""
import asyncio
import hashlib
import json
import os
import ssl
import tempfile
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
from app.models import Photo, PhotoEdit, PhotoStorage, Shooting, User
from app.people import install_people
from app.services import photo_storage
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS
from app.services.bookings import create_booking_record
from app.work_calls import install_work_calls
from app.work_chat import install_work_chat
from app.yandex_disk import YandexDiskError


async def main():
    with tempfile.TemporaryDirectory() as directory:
        engine, factory = await fixture('sqlite+aiosqlite:///' + directory + '/qa.db')
        async with factory() as session:
            booking = await create_booking_record(session, await session.get(User, 1), booking_data())
            booking.photographer_id = 2
            booking.status = 'READY_FOR_SALE'
            (await session.scalar(select(Shooting))).status = 'READY_FOR_SALE'
            await session.commit()
        media = {}
        async def upload(uid, file, **kwargs):
            digest = hashlib.sha256(file.data).hexdigest()
            media[digest] = file.data
            return SimpleNamespace(document=SimpleNamespace(file_id=digest, file_unique_id=digest))
        async def download(fid, destination, **kwargs): destination.write(media[fid])
        bot = SimpleNamespace(token=TOKEN, send_document=upload, download=download, send_message=AsyncMock(), me=AsyncMock(return_value=SimpleNamespace(username='fixture_bot')))
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
            with patch.object(photo_storage, 'Session', factory), patch.object(photo_edits, 'Session', factory):
                async with async_playwright() as p:
                    browser = await p.chromium.launch(args=['--ignore-certificate-errors'])
                    context = await browser.new_context(ignore_https_errors=True, viewport={'width':360,'height':800}, is_mobile=True)
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
                    assert not errors,errors
                    await browser.close()
                    print('PASS: offline batch/reopen, lost upload response replay, dedupe, quota recovery, original-preserving manual copy, comparison, cancellation, access denial, 360px layout')
        finally:
            await server.close()
            await engine.dispose()


if __name__ == '__main__': asyncio.run(main())

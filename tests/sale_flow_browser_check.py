"""Real app + SQLite fixture: two shoot steps, after-shoot actions and «Гость не купил».

Synthetic people and bookings only. Run: python tests/sale_flow_browser_check.py
Screenshots go to $SALE_FLOW_QA_DIR (default /tmp/photo-boss-sale-flow).
"""
import asyncio
import json
import os
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from playwright.async_api import async_playwright, expect
from sqlalchemy import select, text
from test_insights import fixture
from test_miniapp_release import TOKEN, signed

from app.miniapp_api import install_miniapp
from app.models import (
    Booking,
    Client,
    HotelEmployee,
    Photo,
    Sale,
    Shooting,
    UserRole,
    utc_now,
)
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS

BASE = 'https://sale-flow.invalid'


async def main():
    output = Path(os.getenv('SALE_FLOW_QA_DIR', '/tmp/photo-boss-sale-flow'))
    output.mkdir(parents=True, exist_ok=True)
    today = datetime.now(ZoneInfo('Europe/Moscow')).date()
    with tempfile.TemporaryDirectory(prefix='pb-sale-flow-') as directory:
        engine, sessions, _ = await fixture('sqlite+aiosqlite:///' + str(Path(directory) / 'fixture.db'))
        async with sessions() as session:
            await session.execute(text('DELETE FROM receipts'))
            await session.execute(text('DELETE FROM sales'))
            booking = await session.get(Booking, 1)
            booking.shoot_date, booking.shoot_time, booking.status = today, time(10, 30), 'ASSIGNED'
            shoot = await session.scalar(select(Shooting).where(Shooting.booking_id == 1))
            shoot.status, shoot.completed_at, shoot.full_upload_completed_at = 'ASSIGNED', None, None
            session.add_all([UserRole(user_id=2, role='PHOTOGRAPHER'), HotelEmployee(user_id=2, hotel_id=1), Client(id=2, name='Анна')])
            await session.flush()
            session.add(Booking(id=2, hotel_id=1, client_id=2, room='8297', shoot_date=today, shoot_time=time(9),
                                package_id=1, manager_id=1, photographer_id=2, status='READY_FOR_SALE'))
            await session.flush()
            session.add(Shooting(id=2, booking_id=2, status='READY_FOR_SALE', completed_at=utc_now() - timedelta(hours=3),
                                 ready_for_sale_at=utc_now() - timedelta(hours=3)))
            await session.commit()
        # No real Yandex Disk: originals are "stored" by a synthetic provider.
        from app.workflow import Workflow

        async def fake_disk(_self, folder, raw):
            import hashlib

            from app.yandex_disk import ROOT
            return ROOT + folder + '/' + hashlib.sha256(raw).hexdigest() + '.jpg'
        Workflow.save_disk_original = fake_disk
        from app.services import media_files
        media_files.direct_disk_mode = lambda *_args, **_kwargs: True
        app = web.Application()
        bot = SimpleNamespace(token=TOKEN, me=AsyncMock(), send_message=AsyncMock())
        api = install_miniapp(app, engine=engine, bot=bot, lessons=ACADEMY_LESSONS, blocks=ACADEMY_BLOCKS)
        raw_rows = api.rows

        async def rows(conn, sql, **params):
            return await raw_rows(conn, sql.replace(' FOR UPDATE', ''), **params)
        api.rows = rows
        client = TestClient(TestServer(app))
        await client.start_server()
        errors = []
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(executable_path=os.getenv('CALLS_CHROMIUM_PATH') or None)
                try:
                    context = await browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
                    init = signed(2)  # the photographer
                    await context.add_init_script('window.Telegram={WebApp:{initData:' + json.dumps(init) +
                                                  ',ready(){},expand(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}}};')

                    async def route(r):
                        url, method = r.request.url.removeprefix(BASE), r.request.method
                        if r.request.url.startswith('https://telegram.org/'):
                            return await r.fulfill(body='', content_type='text/javascript')
                        if url.startswith('/staff-login/') and url.split('?')[0].endswith('.js'):
                            # Served by the staff QR installer in production; account.js imports it.
                            asset = Path(__file__).resolve().parents[1] / 'app' / 'staff_qr_ui' / url.split('?')[0].rsplit('/', 1)[1]
                            return await r.fulfill(path=str(asset), content_type='text/javascript')
                        if url.endswith('/sms-reminder'):
                            return await r.fulfill(status=200, json={'available': False, 'enabled': False, 'consent': False,
                                                                       'canEdit': False, 'status': 'unavailable'})
                        try:
                            response = await client.request(method, url, data=r.request.post_data_buffer,
                                headers={'X-Telegram-Init-Data': init, 'Content-Type': r.request.headers.get('content-type', 'application/json')})
                            body = await response.read()
                        except Exception as exc:  # noqa: BLE001 -- surface any fixture fault instead of hanging the page
                            print('route failure', method, url, repr(exc))
                            return await r.fulfill(status=599, body=repr(exc))
                        await r.fulfill(status=response.status, headers={'Content-Type': response.headers.get('Content-Type', 'text/plain')},
                                        body=body)
                    await context.route('**/*', route)
                    page = await context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    if os.getenv('SALE_FLOW_DEBUG'):
                        page.on('request', lambda req: print('req', req.method, req.url.removeprefix(BASE)) if '/api/' in req.url else None)
                        page.on('console', lambda msg: print('console:', msg.type, msg.text))
                        page.on('response', lambda res: print('http', res.status, res.url.removeprefix(BASE)) if '/api/' in res.url or res.status >= 400 else None)
                    await page.goto(BASE + '/app/#home')
                    if os.getenv('SALE_FLOW_DEBUG'):
                        await page.wait_for_timeout(4000)
                        print('errors:', errors)
                        print('probe:', await page.evaluate("import('/app/js/api.js').then(m=>m.api('/me')).then(r=>JSON.stringify(r).slice(0,200)).catch(e=>'ERR '+e.status+' '+e.message)"))
                        print('appjs:', await page.evaluate("import('/app/js/app.js').then(()=>'evaluated').catch(e=>'ERR '+e.message+' '+(e.stack||'').slice(0,300))"))
                        print('state:', await page.evaluate("JSON.stringify({ready:document.readyState,tg:!!window.Telegram?.WebApp?.initData,cfg:window.PHOTO_BOSS_CONFIG})"))
                        print('text:', (await page.locator('body').inner_text())[:500])
                        print('resources:', await page.evaluate("performance.getEntriesByType('resource').map(e=>e.name.split('/').slice(3).join('/')+':'+Math.round(e.duration)).join(' ')"))
                    alert = page.locator('.home-attention', has_text='без продажи')
                    await expect(alert).to_be_visible()
                    await page.screenshot(path=str(output / '1-home-reminder.png'), full_page=False)

                    # Two steps instead of four.
                    await page.goto(BASE + '/app/#shootings')
                    await page.locator('[data-booking="1"]').first.click()
                    sheet = page.locator('#sheet')
                    begin = sheet.get_by_role('button', name='▶️ Начать съёмку')
                    await expect(begin).to_be_visible()
                    await page.screenshot(path=str(output / '2-begin.png'))
                    await begin.click()
                    complete = page.locator('#sheet').get_by_role('button', name='✅ Закончить съёмку')
                    await expect(complete).to_be_visible()
                    await complete.click()
                    sheet = page.locator('#sheet')
                    await expect(sheet.get_by_role('button', name='Оформить продажу')).to_be_visible()
                    await expect(sheet.get_by_role('button', name='Загрузить всю съёмку')).to_be_visible()
                    await expect(sheet.get_by_role('button', name='Гость не купил')).to_be_visible()
                    await page.screenshot(path=str(output / '3-after-shoot.png'))
                    async with sessions() as session:
                        shoot = await session.get(Shooting, 1)
                        assert shoot.status == 'READY_FOR_SALE' and shoot.accepted_at and shoot.started_at
                        assert shoot.completed_at and shoot.ready_for_sale_at

                    # «Гость не купил» with a reason, then undo.
                    await sheet.get_by_role('button', name='Гость не купил').click()
                    form = page.locator('#noSaleForm')
                    await expect(form).to_be_visible()
                    await form.get_by_role('radio', name='Другая причина').check()
                    await form.get_by_role('button', name='Отметить «гость не купил»').click()
                    await expect(form.locator('[data-no-sale-feedback]')).to_contain_text('Напишите')
                    await form.get_by_role('radio', name='Дорого').check()
                    await page.screenshot(path=str(output / '4-no-sale-form.png'))
                    await form.get_by_role('button', name='Отметить «гость не купил»').click()
                    sheet = page.locator('#sheet')
                    await expect(sheet.locator('.notice', has_text='Гость не купил')).to_contain_text('Дорого')
                    await expect(sheet.get_by_role('button', name='Гость вернулся — оформить продажу')).to_be_visible()
                    await page.screenshot(path=str(output / '5-no-sale-marked.png'))
                    async with sessions() as session:
                        marked = await session.get(Booking, 1)
                        assert marked.no_sale_reason == 'PRICE' and marked.status == 'READY_FOR_SALE'
                    await sheet.get_by_role('button', name='Снять отметку «не купил»').click()
                    await expect(page.locator('#sheet').get_by_role('button', name='Гость не купил')).to_be_visible()
                    async with sessions() as session:
                        assert (await session.get(Booking, 1)).no_sale_at is None

                    # Full-shoot upload straight from the card, then one tap to finish.
                    card = page.locator('#sheet [data-card-upload="1"]')
                    await expect(card).to_be_visible()
                    assert await card.locator('[data-card-fast]').is_checked()
                    await page.screenshot(path=str(output / '6-card-upload.png'))
                    photos = []
                    for index, color in enumerate(('red', 'green', 'blue')):
                        path = Path(directory) / f'DSC_{index:04d}.jpg'
                        Image.new('RGB', (64, 48), color).save(path, format='JPEG')
                        photos.append(str(path))
                    await card.locator('input[data-card-files]').first.set_input_files(photos)
                    progress = card.locator('[data-card-progress]')
                    await expect(progress).to_contain_text('Готово: загружено 3', timeout=20000)
                    finish = card.get_by_role('button', name='Это вся съёмка — завершить загрузку')
                    await expect(finish).to_be_visible()
                    await page.screenshot(path=str(output / '7-card-uploaded.png'))
                    await finish.click()
                    await expect(card).to_contain_text('Загрузка будет завершена')
                    for _ in range(60):
                        async with sessions() as session:
                            done = (await session.get(Shooting, 1)).full_upload_completed_at
                        if done:
                            break
                        await page.wait_for_timeout(250)
                    if not done:
                        print('queue:', await page.evaluate("import('/app/js/outbox.js').then(m=>m.outboxRows()).then(rows=>JSON.stringify(rows.map(r=>({k:r.kind,s:r.status,e:r.error,es:r.errorStatus,lane:r.lane}))))"))
                    assert done, 'the full upload is closed after every photo was accepted'
                    async with sessions() as session:
                        names = set(await session.scalars(select(Photo.original_name).join(Shooting).where(Shooting.booking_id == 1)))
                    assert names == {'DSC_0000.jpg', 'DSC_0001.jpg', 'DSC_0002.jpg'}, names
                finally:
                    await browser.close()
            async with sessions() as session:
                assert await session.scalar(select(Sale.id).limit(1)) is None, 'no sale is created by these steps'
        finally:
            await client.close()
            await engine.dispose()
    assert not errors, errors
    print('PASS: two shoot steps, after-shoot actions, no-sale with reason and undo, upload shortcut;',
          'screenshots in', output)


if __name__ == '__main__':
    asyncio.run(main())

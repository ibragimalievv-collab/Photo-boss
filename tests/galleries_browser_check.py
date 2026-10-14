"""Prototype disk and gallery actions against real fixture APIs, without live storage."""
import asyncio
import io
import json
import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from playwright.async_api import async_playwright, expect
from test_insights import fixture
from test_miniapp_release import TOKEN, signed

from app.miniapp_api import install_miniapp
from app.models import DeliveryGallery, DeliveryPhoto
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS

BASE = 'https://gallery-navigation.invalid'


async def main():
    engine, factory, _ = await fixture()
    bot = SimpleNamespace(token=TOKEN, send_message=AsyncMock(),
                          me=AsyncMock(return_value=SimpleNamespace(username='fixture_bot')))
    app = web.Application()
    api = install_miniapp(app, engine=engine, bot=bot, lessons=ACADEMY_LESSONS, blocks=ACADEMY_BLOCKS)
    api.today = lambda: date(2026, 9, 21)
    original = api.rows
    async def rows(conn, sql, **params):
        return await original(conn, sql.replace(' FOR UPDATE', ''), **params)
    api.rows = rows
    storage = {}
    async with factory() as session:
        g = DeliveryGallery(booking_id=1, title='Movenpick 16:00', published=True, delivery_mode='ALL',
                            created_by_id=1, access_token='g'*32)
        session.add(g)
        await session.flush()
        for i in range(1, 7):
            sample = Path(os.getenv('GALLERY_QA_PHOTOS', '/nonexistent')) / f'photo-{i:02}.jpg'
            if sample.is_file():
                raw = sample.read_bytes()
            else:
                stream = io.BytesIO()
                Image.new('RGB', (500, 700) if i % 2 else (700, 500), ['#617460', '#8c8674', '#c5b7a2'][i % 3]).save(stream, 'JPEG')
                raw = stream.getvalue()
            storage[str(i)] = raw
            session.add(DeliveryPhoto(id=i, gallery_id=g.id, uploaded_by_id=1, filename=f'family-{i}.jpg',
                disk_path=str(i), sha256=str(i)*64, byte_size=len(raw), selected=False, upload_set='ALL'))
        await session.commit()
    delivery = app['delivery']
    async def photo_bytes(photo):
        return storage[photo.disk_path]
    delivery.bytes = photo_bytes
    client = TestClient(TestServer(app))
    await client.start_server()
    errors = []
    output = Path(os.getenv('GALLERY_QA_DIR', '/tmp/photo-boss-gallery-qa'))
    output.mkdir(parents=True, exist_ok=True)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=os.getenv('CALLS_CHROMIUM_PATH') or None)
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
            init = signed(1)
            await context.add_init_script('window.Telegram={WebApp:{initData:' + json.dumps(init) + ',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}}};')
            async def route(r):
                if r.request.url.startswith('https://telegram.org/'):
                    return await r.fulfill(body='', content_type='text/javascript')
                assert r.request.url.startswith(BASE), r.request.url
                response = await client.request(r.request.method, r.request.url.removeprefix(BASE),
                    data=r.request.post_data_buffer, headers={'X-Telegram-Init-Data': init,
                    'Content-Type': r.request.headers.get('content-type', 'application/json')})
                await r.fulfill(status=response.status, body=await response.read(),
                                headers={k: v for k, v in response.headers.items() if k.lower() in {'content-type', 'content-security-policy', 'cache-control'}})
            await context.route('**/*', route)
            page = await context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            await page.goto(BASE + '/app/#galleries')
            await expect(page.locator('#app h1')).to_have_text('Диск и клиентские галереи')
            await expect(page.locator('.table [data-gp="open"]')).to_have_count(1)
            await page.locator('[data-project-search]').fill('missing')
            await expect(page.locator('.table .empty')).to_be_visible()
            await page.locator('[data-project-search]').fill('Movenpick')
            await expect(page.locator('.table [data-gp="open"]')).to_have_count(1)
            await page.screenshot(path=str(output / 'disk-desktop.png'), full_page=True)
            await page.locator('.table [data-gp="favorite"]').click()
            await expect(page.locator('.table [data-gp="favorite"]')).to_have_attribute('aria-pressed', 'true')
            await page.reload()
            await expect(page.locator('.table [data-gp="favorite"]')).to_have_attribute('aria-pressed', 'true')
            await page.locator('.table [data-gp="open"]').click()
            await expect(page.locator('.hero h1')).to_have_text('Movenpick 16:00')
            await expect(page.locator('.grid .photo')).to_have_count(6)
            await expect(page.locator('.hero img')).to_have_attribute('src', __import__('re').compile(r'^blob:'))
            await page.screenshot(path=str(output / 'gallery-desktop.png'), full_page=True)
            await page.locator('[data-gv-favorite="2"]').click()
            await expect(page.locator('[data-fav-count]')).to_have_text('1')
            await page.locator('[data-gv-filter="fav"]').click()
            await expect(page.locator('.grid .photo')).to_have_count(1)
            await page.locator('[data-gv-photo="2"]').click()
            await expect(page.locator('dialog.viewer')).to_be_visible()
            await page.locator('[data-v="cover"]').click()
            await expect(page.locator('.hero img')).to_have_attribute('data-gallery-image', '2')
            async with page.expect_download() as download:
                await page.locator('[data-v="download"]').click()
            assert (await download.value).suggested_filename == 'family-2.jpg'
            await page.locator('[data-v="close"]').click()
            await page.locator('[data-gv="settings"]').click()
            await page.locator('.modal [name=title]').fill('Семья в Анапе')
            await page.locator('.modal [name=days]').select_option('30')
            await page.locator('.modal [type=submit]').click()
            await expect(page.locator('.hero h1')).to_have_text('Семья в Анапе')
            await page.locator('[data-gv="back"]').click()
            await expect(page.locator('#app h1')).to_have_text('Диск и клиентские галереи')
            await page.set_viewport_size({'width': 390, 'height': 844})
            await expect(page.locator('.cards .card')).to_be_visible()
            assert await page.locator('#app').evaluate('el => el.scrollWidth <= el.clientWidth + 2')
            await page.screenshot(path=str(output / 'disk-mobile.png'), full_page=True)
            await page.locator('.cards [data-gp="menu"]').click()
            await page.locator('[data-menu="trash"]').click()
            await expect(page.locator('.cards .card')).to_have_count(0)
            await page.locator('[data-gp-mode="trash"]').click()
            await expect(page.locator('.cards .card')).to_have_count(1)
            assert (await client.get('/g/' + 'g'*32)).status == 404
            await page.locator('.cards [data-gp="menu"]').click()
            await page.locator('[data-menu="trash"]').click()
            await page.locator('[data-gp-mode="all"]').click()
            await page.locator('.cards [data-gp="open"]').click()
            await expect(page.locator('.hero h1')).to_have_text('Семья в Анапе')
            await page.screenshot(path=str(output / 'gallery-mobile.png'), full_page=True)
            assert await page.locator('#app').evaluate('el => el.scrollWidth <= el.clientWidth + 2')
            # Guest uses the identical shared layout and persisted favorites, never the staff shell.
            guest = await context.new_page()
            await guest.set_viewport_size({'width': 390, 'height': 844})
            guest.on('pageerror', lambda e: errors.append(str(e)))
            await guest.goto(BASE + '/g/' + 'g'*32)
            await expect(guest.locator('.hero h1')).to_have_text('Семья в Анапе')
            await expect(guest.locator('[data-fav-count]')).to_have_text('1')
            assert await guest.locator('[data-gv="settings"], [data-gv="upload"]').count() == 0
            await guest.locator('[data-gv-filter="fav"]').click()
            await expect(guest.locator('.grid .photo')).to_have_count(1)
            await guest.screenshot(path=str(output / 'guest-mobile.png'), full_page=True)
            assert not errors, errors
            assert not bot.send_message.called
            await browser.close()
            print('PASS: prototype disk/gallery, desktop/mobile, search, saved favorites, original download, cover, settings, trash/restore and shared guest layout')
    finally:
        await client.close()
        await engine.dispose()


asyncio.run(main())

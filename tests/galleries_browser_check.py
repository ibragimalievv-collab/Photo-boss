"""Integrated mobile gallery navigation against the real fixture server."""
import asyncio
import json
import os
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from playwright.async_api import async_playwright, expect
from test_insights import fixture
from test_miniapp_release import TOKEN, signed

from app.miniapp_api import install_miniapp
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS

BASE = 'https://gallery-navigation.invalid'


async def main():
    engine, _, _ = await fixture()
    bot = SimpleNamespace(token=TOKEN, send_message=AsyncMock(),
                          me=AsyncMock(return_value=SimpleNamespace(username='fixture_bot')))
    app = web.Application()
    api = install_miniapp(app, engine=engine, bot=bot, lessons=ACADEMY_LESSONS, blocks=ACADEMY_BLOCKS)
    api.today = lambda: date(2026, 9, 21)
    original = api.rows
    async def rows(conn, sql, **params):
        return await original(conn, sql.replace(' FOR UPDATE', ''), **params)
    api.rows = rows
    client = TestClient(TestServer(app))
    await client.start_server()
    errors = []
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    delay = False
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=os.getenv('CALLS_CHROMIUM_PATH') or None)
            context = await browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
            init = signed(1)
            await context.add_init_script('window.Telegram={WebApp:{initData:' + json.dumps(init) + ',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}}};')
            async def route(r):
                if r.request.url.startswith('https://telegram.org/'):
                    return await r.fulfill(body='', content_type='text/javascript')
                path = r.request.url.removeprefix(BASE)
                assert r.request.url.startswith(BASE), r.request.url
                delayed = delay and path.startswith('/api/miniapp/shoot-control?')
                if delayed:
                    started.set()
                    await release.wait()
                response = await client.request(r.request.method, path, data=r.request.post_data_buffer,
                                                headers={'X-Telegram-Init-Data': init, 'Content-Type': r.request.headers.get('content-type', 'application/json')})
                await r.fulfill(status=response.status, body=await response.read(),
                                headers={'Content-Type': response.headers.get('Content-Type', 'text/plain')})
                if delayed:
                    finished.set()
            await context.route('**/*', route)
            page = await context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            await page.goto(BASE + '/app/#more')
            await page.get_by_role('button', name='Клиентские галереи', exact=False).last.click()
            await expect(page.locator('#app h1')).to_have_text('Клиентские галереи')
            await expect(page.locator('#app h1')).to_have_count(1)
            await expect(page.locator('[data-gallery-booking="1"]')).to_be_visible()
            await page.locator('[data-gallery-booking="1"]').click()
            await expect(page.locator('dialog.delivery-sheet h2')).to_have_text('Галерея съёмки №1')
            await expect(page.locator('#deliveryAllUploadForm')).to_be_visible()
            await page.locator('[data-delivery="close"]').click()
            await page.locator('#shootControlPeriod button').click()
            await expect(page.locator('#shootControlPeriod button')).to_be_enabled()
            await expect(page.locator('#app h1')).to_have_text('Клиентские галереи')
            await expect(page.locator('[data-gallery-booking="1"]')).to_be_visible()
            assert await page.locator('#app').evaluate('el => el.scrollWidth <= el.clientWidth + 2')
            delay = True
            await page.locator('#shootControlPeriod button').click()
            await asyncio.wait_for(started.wait(), 10)
            await page.locator('#bottomNav [data-go="more"]').click()
            await expect(page.locator('#app h1')).to_have_text('Ваше пространство')
            release.set()
            await asyncio.wait_for(finished.wait(), 10)
            # Complete the browser's response/DOM task without a timed sleep.
            await page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')
            await expect(page.locator('#app h1')).to_have_text('Ваше пространство')
            await page.locator('#app [data-go="control"]').click()
            await expect(page.locator('#app h1')).to_have_text('Контроль съёмок')
            assert not errors, errors
            assert not bot.send_message.called, 'No live guest messages in navigation checks'
            await browser.close()
            print('PASS: real shared shooting API, mobile gallery entry, album opening, period title, stale-response navigation and control title')
    finally:
        release.set()
        await client.close()
        await engine.dispose()


asyncio.run(main())

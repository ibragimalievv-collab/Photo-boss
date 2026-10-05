"""Real desktop/mobile forms: Telegram bootstrap, passwords, switch and period editor."""
import asyncio
import json
import os
import ssl
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_miniapp_release as fixtures
from aiohttp import web
from playwright.async_api import async_playwright
from sqlalchemy import text

from app.browser_login import BrowserLogin
from app.password_login import PasswordLogin

PASSWORD = 'browser-test-password-2026'


async def run():
    fixture = fixtures.MiniAppTests()
    await fixture.asyncSetUp()
    app = web.Application()
    fixture.service.register(app)
    runner = web.AppRunner(app)
    await runner.setup()
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(os.environ['WORKFLOW_TEST_CERT'], os.environ['WORKFLOW_TEST_KEY'])
    # Bind first to discover the port, register against that exact origin before startup.
    import socket
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    origin = f'https://127.0.0.1:{sock.getsockname()[1]}'
    sock.close()
    # AppRunner freezes the router, so construct the complete app with the confirmed port.
    await runner.cleanup()
    app = web.Application()
    fixture.service.register(app)
    browser_login = BrowserLogin(fixture.engine, fixture.bot, origin)
    passwords = PasswordLogin(fixture.service, origin)
    for name in ['start', 'poll']:
        app.router.add_post('/auth/browser/' + name, browser_login.endpoint)
    for name in ['login', 'logout']:
        app.router.add_post('/auth/password/' + name, passwords.endpoint)
    app.router.add_put('/api/miniapp/account/password', passwords.setup)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', int(origin.rsplit(':',1)[1]), ssl_context=tls)
    await site.start()
    staff = fixtures.Request('/api/miniapp/account/password',1003,'PUT',{'login':'staff','password':PASSWORD},None)
    staff.headers.update({'Origin':origin,'X-PhotoBoss-Session':'1'})
    staff.cookies = {}
    staff.remote = '127.0.0.1'
    assert (await fixture.service.middleware(staff,passwords.setup)).status == 200
    qa = Path(os.environ.get('CALLS_QA_DIR','/tmp/photo-boss-account-qa'))
    qa.mkdir(parents=True, exist_ok=True)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=['--no-sandbox'])
            for mobile in [False, True]:
                context = await browser.new_context(ignore_https_errors=True,
                    viewport={'width':390 if mobile else 1366,'height':844 if mobile else 900},
                    is_mobile=mobile,has_touch=mobile,service_workers='block',locale='ru-RU')
                # Unrelated extension entrypoints are excluded; app/account/editor modules are real.
                for path in ['/shift/attendance.js','/people/people.js','/work-chat/chat.js',
                             'https://telegram.org/js/telegram-web-app.js']:
                    await context.route('**'+path if path.startswith('/') else path,
                                        lambda route: route.fulfill(status=200,body='',content_type='text/javascript'))
                page = await context.new_page()
                errors = []
                page.on('pageerror', lambda error, errors=errors: errors.append(str(error)))
                await page.goto(origin+'/app/')
                await page.locator('#passwordLoginForm').wait_for()
                await page.screenshot(path=str(qa / f'login-{mobile}.png'),full_page=True)
                await page.locator('[data-action=browser-password]' if mobile else '[data-action=browser-login]').click()
                link = await page.locator('a[href*="?start=web_"]').get_attribute('href')
                identifier = link.rsplit('web_',1)[1]
                user = SimpleNamespace(id=1001,is_bot=False)
                message = SimpleNamespace(chat=SimpleNamespace(type='private'),from_user=user,answer=AsyncMock())
                callback = SimpleNamespace(message=message,from_user=user,answer=AsyncMock())
                await browser_login.prompt(message,identifier)
                await browser_login.approve(callback,identifier,True)
                await page.locator('[data-action=browser-login-check]').click()
                await page.locator('#passwordSetupForm').wait_for()
                await page.locator('#passwordSetupForm [name=login]').fill('owner')
                await page.locator('#passwordSetupForm [name=password]').fill(PASSWORD)
                await page.locator('#passwordSetupForm [name=repeatPassword]').fill(PASSWORD)
                await page.locator('#passwordSetupForm [type=submit]').click()
                await page.locator('#sheet[open]').wait_for(state='hidden')
                await page.locator('#sidebar [data-go=profile]').wait_for(state='attached')
                await page.locator('#bottomNav [data-go=schedule]' if mobile else '#sidebar [data-go=schedule]').click()
                await page.locator('[data-action=new-shift]').click()
                await page.locator('#shiftForm').wait_for()
                await page.locator('#shiftForm [name=userId]').select_option('3')
                tomorrow = fixture.service.today()+timedelta(days=4 if mobile else 1)
                await page.locator('#shiftForm [name=from]').fill(str(tomorrow))
                await page.locator('#shiftForm [name=to]').fill(str(tomorrow+timedelta(days=2)))
                await page.locator('#shiftForm summary').click()
                await page.locator('[name=templateHotel]').select_option('1')
                await page.locator('[name=templateEnd]').fill('12:00')
                await page.locator('[data-action=fill-shift-days]').click()
                await page.locator('[name=templateHotel]').select_option('2')
                await page.locator('[name=templateStart]').fill('12:00')
                await page.locator('[name=templateEnd]').fill('18:00')
                await page.locator('[data-action=append-shift-days]').click()
                assert await page.locator('.shift-slot').count() == 6
                assert await page.locator('#shiftForm [name=userId]').count() == 1
                await page.screenshot(path=str(qa/f'schedule-period-{mobile}.png'),full_page=True)
                await page.locator('#shiftForm [type=submit]').click()
                await page.locator('#sheet[open]').wait_for(state='hidden')
                await page.locator('.schedule-person').first.wait_for()
                assert await page.locator('.schedule-person').count() == 2
                with fixture.engine.inner.connect() as conn:
                    assert conn.execute(text('SELECT COUNT(*) FROM shifts')).scalar_one() == (12 if mobile else 6)
                # Open the profile on either layout and switch away from the owner account.
                await page.locator('#topbar [data-go=profile]').click()
                await page.locator('#app [data-action=switch-account]').click()
                await page.locator('#passwordLoginForm').wait_for()
                await page.locator('#passwordLoginForm [name=login]').fill('staff')
                await page.locator('#passwordLoginForm [name=password]').fill('wrong-test-password')
                await page.locator('#passwordLoginForm [type=submit]').click()
                await page.locator('#formError:not([hidden])').wait_for()
                await page.locator('#passwordLoginForm [name=password]').fill(PASSWORD)
                await page.locator('#passwordLoginForm [type=submit]').click()
                await page.locator('#topbar [data-go=profile]').wait_for()
                me = await (await context.request.get(origin+'/api/miniapp/me',headers={'X-PhotoBoss-Session':'1'})).json()
                assert me['user']['telegramId'] == 1003 and me['passwordLogin'] == 'staff'
                assert not me['permissions']['audit']
                assert (await context.request.get(origin+'/api/miniapp/audit',headers={'X-PhotoBoss-Session':'1'})).status == 403
                assert not errors, errors
                await context.close()
            await browser.close()
        print(json.dumps({'status':'PASS','forms':'desktop and mobile','checks':
            ['Telegram bootstrap','personal password setup','password login','wrong password',
             'switch account','one employee for three days and two hotels','no page errors']},ensure_ascii=False))
    finally:
        await runner.cleanup()
        await fixture.asyncTearDown()


if __name__ == '__main__':
    asyncio.run(run())

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
    # More than a normal staff page: the '+' and team dialogs must expose the
    # last employee too, including people without hotel assignments or shifts.
    with fixture.engine.inner.begin() as conn:
        for uid in range(8, 113):
            conn.execute(text('INSERT INTO users VALUES (:id,:tg,:name,TRUE)'),
                         {'id': uid, 'tg': 1000+uid,
                          'name': f'Сотрудник №{uid} с длинным именем для проверки переноса текста'})
            conn.execute(text('INSERT INTO user_roles VALUES (:id,:role)'),
                         {'id': uid, 'role': 'PHOTOGRAPHER' if uid == 112 else 'MANAGER'})
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
                    viewport={'width':320 if mobile else 1366,'height':844 if mobile else 900},
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
                assert await page.locator('#shiftForm [name=userId] option').count() == 108
                assert 'Фотограф' in await page.locator('#shiftForm [name=userId] option[value="3"]').inner_text()
                assert '№3' in await page.locator('#shiftForm [name=userId] option[value="3"]').inner_text()
                roster_search = page.locator('#shiftForm [data-roster-search]')
                previous = await page.locator('#shiftForm [name=userId]').input_value()
                await roster_search.fill('№112')
                await roster_search.press('Enter')
                assert await page.locator('#shiftForm').is_visible()
                with fixture.engine.inner.connect() as conn:
                    assert conn.execute(text('SELECT COUNT(*) FROM shifts')).scalar_one() == (24 if mobile else 0)
                assert 'Найдено: 1 из 108' in await page.locator('#scheduleRosterCount').inner_text()
                assert await page.locator('#shiftForm [name=userId]').input_value() == previous
                await page.locator('#shiftForm [name=userId]').select_option('112')
                await roster_search.fill('несуществующее имя')
                assert 'Найдено: 0 из 108' in await page.locator('#scheduleRosterCount').inner_text()
                assert await page.locator('#shiftForm [name=userId]').input_value() == '112'
                await roster_search.fill('')
                assert await page.locator('#shiftForm [name=userId] option').count() == 108
                assert await page.locator('#shiftForm [name=userId]').input_value() == '112'
                await page.get_by_text('Не видите нужного сотрудника?', exact=True).click()
                assert await page.get_by_text('Inactive · №6: в архиве', exact=False).count() == 1
                assert await page.get_by_text('No role · №7: назначьте роль', exact=False).count() == 1
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
                await page.locator('[data-action=add-shift-slot]').click()
                assert await page.locator('.shift-slot').count() == 7
                await page.locator('.shift-slot').last.locator('[data-action=remove-shift-slot]').click()
                assert await page.locator('.shift-slot').count() == 6
                await page.screenshot(path=str(qa/f'schedule-period-{mobile}.png'),full_page=True)
                await page.locator('#shiftForm [type=submit]').click()
                await page.locator('#sheet[open]').wait_for(state='hidden')
                await page.locator('.schedule-person').first.wait_for()
                assert await page.locator('.schedule-person').count() == 2
                with fixture.engine.inner.connect() as conn:
                    assert conn.execute(text('SELECT COUNT(*) FROM shifts')).scalar_one() == (30 if mobile else 6)
                # Group editor: separate templates must preserve other employees' rows.
                await page.locator('[data-action=new-team-shift]').click()
                await page.locator('#shiftForm').wait_for()
                assert await page.locator('#shiftForm [name=teamUser]').count() == 108
                assert await page.locator('#shiftForm [data-roster-search]').input_value() == ''
                await page.locator('#shiftForm [data-roster-search]').fill('№112')
                assert await page.locator('[data-roster-person]:visible').count() == 1, await page.locator('[data-roster-person]').evaluate_all('(rows)=>rows.slice(0,3).map(row=>({id:row.dataset.rosterPerson,hidden:row.hidden,display:getComputedStyle(row).display}))')
                await page.locator('[data-action=select-roster-visible]').click()
                assert await page.locator('[name=teamUser][value="112"]').is_checked()
                await page.locator('#shiftForm [data-roster-search]').fill('User 3')
                assert await page.locator('[data-roster-person]:visible').count() == 1
                assert await page.locator('[name=teamUser][value="112"]').is_checked()
                await page.locator('[data-action=select-roster-visible]').click()
                assert 'Выбрано: 2' in await page.locator('#scheduleRosterCount').inner_text()
                await page.locator('[data-action=clear-roster-visible]').click()
                assert 'Выбрано: 1' in await page.locator('#scheduleRosterCount').inner_text()
                await page.locator('#shiftForm [data-roster-search]').fill('№112')
                await page.locator('[data-action=clear-roster-visible]').click()
                await page.locator('#shiftForm [data-roster-search]').fill('')
                assert await page.locator('[data-roster-person]:visible').count() == 108
                assert 'Выбрано: 0' in await page.locator('#scheduleRosterCount').inner_text()
                assert await page.evaluate('document.querySelector("#sheetContent").scrollWidth <= document.querySelector("#sheetContent").clientWidth')
                assert await page.locator('[data-roster-person]').evaluate_all('''rows=>rows.every((row,index)=>{
                    const box=row.getBoundingClientRect(),text=row.querySelector('span').getBoundingClientRect();
                    return text.top>=box.top&&text.bottom<=box.bottom+1&&(!index||rows[index-1].getBoundingClientRect().bottom<=box.top+1);
                })'''), 'Long employee names must fit their own rows, including at 320px.'
                await page.screenshot(path=str(qa/f'roster-all-108-{mobile}.png'),full_page=True)
                team_day = fixture.service.today()+timedelta(days=10 if mobile else 7)
                await page.locator('#shiftForm [name=from]').fill(str(team_day))
                await page.locator('#shiftForm [name=to]').fill(str(team_day+timedelta(days=2)))
                await page.locator('#shiftForm summary').click()
                await page.locator('[name=teamUser][value="3"]').check()
                await page.locator('[name=teamUser][value="4"]').check()
                await page.locator('[name=templateEnd]').fill('12:00')
                await page.locator('[data-action=fill-shift-days]').click()
                assert await page.locator('.shift-slot').count() == 6
                await page.locator('[name=teamUser][value="3"]').uncheck()
                await page.locator('[name=teamUser][value="4"]').uncheck()
                await page.locator('[name=teamUser][value="5"]').check()
                await page.locator('[name=templateHotel]').select_option('2')
                await page.locator('[data-action=fill-shift-days]').click()
                assert await page.locator('.shift-slot').count() == 9
                await page.locator('[name=teamUser][value="3"]').check()
                await page.locator('[name=teamUser][value="4"]').check()
                await page.locator('[name=templateStart]').fill('12:00')
                await page.locator('[name=templateEnd]').fill('18:00')
                await page.locator('[data-action=append-shift-days]').click()
                assert await page.locator('.shift-slot').count() == 18
                # Different employees can share a time; a cross-hotel overlap for one cannot save.
                await page.locator('.shift-slot').last.locator('[data-slot=start]').fill('11:00')
                await page.locator('#shiftForm [type=submit]').click()
                await page.locator('#formError:not([hidden])').wait_for()
                with fixture.engine.inner.connect() as conn:
                    assert conn.execute(text('SELECT COUNT(*) FROM shifts')).scalar_one() == (30 if mobile else 6)
                await page.locator('.shift-slot').last.locator('[data-slot=start]').fill('12:00')
                await page.screenshot(path=str(qa/f'team-schedule-{mobile}.png'),full_page=True)
                await page.locator('#shiftForm [type=submit]').click()
                await page.locator('#sheet[open]').wait_for(state='hidden')
                await page.locator('.schedule-person').first.wait_for()
                assert await page.locator('.schedule-person').count() == 6
                with fixture.engine.inner.connect() as conn:
                    assert conn.execute(text('SELECT COUNT(*) FROM shifts')).scalar_one() == (48 if mobile else 24)
                assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
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
             'switch account','all 108 employees in plus and team editors','search last employee without changing selection',
             'search keeps hidden team selections','add and remove shift buttons',
             'one employee for three days and two hotels','three employees across three days and two hotels',
             'group overlap rejected before save','320px layout without roster overflow','no page errors']},ensure_ascii=False))
    finally:
        await runner.cleanup()
        await fixture.asyncTearDown()


if __name__ == '__main__':
    asyncio.run(run())

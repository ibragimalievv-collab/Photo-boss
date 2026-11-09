"""Old-hours shifts: banner, one-tap move to 09:00 until the last sale; desktop and 320px."""
import asyncio
import json
import os
import ssl
import subprocess
import tempfile
from datetime import date, datetime, time, timezone
from pathlib import Path

import test_miniapp_release as fixtures
from aiohttp import web
from playwright.async_api import async_playwright, expect
from sqlalchemy import text

TODAY = date(2026, 11, 9)


def utc(fixture, day, hh, mm=0):
    local = datetime.combine(day, time(hh, mm), fixture.service.tz)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def plan_old_hours(fixture):
    rows = [(10, 3, 1, date(2026, 11, 10), (9, 0), (19, 0)), (11, 4, 2, date(2026, 11, 11), (10, 0), (18, 0)),
            (12, 5, 1, date(2026, 11, 10), (9, 0), (12, 0)), (13, 5, 2, date(2026, 11, 10), (12, 0), (18, 0))]
    with fixture.engine.inner.begin() as conn:
        conn.execute(text('DELETE FROM shifts'))
        for sid, uid, hid, day, start, end in rows:
            conn.execute(text("INSERT INTO shifts VALUES (:id,:uid,:hid,:st,:en,'PLANNED')"),
                         {'id': sid, 'uid': uid, 'hid': hid, 'st': utc(fixture, day, *start), 'en': utc(fixture, day, *end)})


async def main():
    fixture = fixtures.MiniAppTests()
    await fixture.asyncSetUp()
    fixture.service.today = lambda: TODAY
    app = web.Application()
    fixture.service.register(app)
    runner = web.AppRunner(app)
    await runner.setup()
    with tempfile.TemporaryDirectory(prefix='legacy-hours-tls-') as directory:
        cert, key = Path(directory) / 'cert.pem', Path(directory) / 'key.pem'
        await asyncio.to_thread(subprocess.run, ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(key), '-out', str(cert), '-days', '1',
                        '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1'],
                       check=True, capture_output=True)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
    site = web.TCPSite(runner, '127.0.0.1', 0, ssl_context=tls)
    await site.start()
    origin = f'https://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
    evidence = Path(os.environ.get('SCHEDULE_QA_DIR', '/tmp/photo-boss-schedule-qa'))
    evidence.mkdir(parents=True, exist_ok=True)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'),
                                               args=['--no-sandbox'])
            for width in (1366, 320):
                plan_old_hours(fixture)
                context = await browser.new_context(ignore_https_errors=True, viewport={'width': width, 'height': 900},
                                                    locale='ru-RU', service_workers='block')
                await context.add_init_script('window.Telegram={WebApp:{initData:'+json.dumps(fixtures.signed())+',ready(){},expand(){},onEvent(){},setHeaderColor(){},setBackgroundColor(){},BackButton:{onClick(){},hide(){},show(){}}}};')
                for path in ('/shift/attendance.js', '/people/people.js', '/work-chat/chat.js', '/staff-login/scan.js',
                             'https://telegram.org/js/telegram-web-app.js'):
                    await context.route('**'+path if path.startswith('/') else path,
                        lambda route: route.fulfill(status=200, body='', content_type='text/javascript'))
                page = await context.new_page()
                errors = []
                page.on('pageerror', lambda error, errors=errors: errors.append(str(error)))
                await page.goto(origin+'/app/#schedule')
                note = page.locator('.legacy-hours')
                await note.wait_for()
                body = await note.inner_text()
                assert '2 смены с 10 нояб. по 11 нояб.' in body, body
                assert 'с двумя сменами' in body and 'User 5 · 10 нояб.' in body, body
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                await page.screenshot(path=str(evidence / f'legacy-hours-{width}.png'), full_page=True)
                await note.locator('[data-action=normalize-hours]').click()
                await page.locator('[data-confirm-normalize-hours]').click()
                await expect(page.locator('#toast')).to_contain_text('Готово: 2 смены переведены')
                with fixture.engine.inner.connect() as conn:
                    hours = dict(conn.execute(text("SELECT id,strftime('%H:%M',start_at)||'-'||strftime('%H:%M',end_at) FROM shifts")).all())
                assert hours == {10: '06:00-20:59', 11: '06:00-20:59', 12: '06:00-09:00', 13: '09:00-15:00'}, hours
                await expect(note.locator('[data-action=normalize-hours]')).to_have_count(0)
                assert 'с двумя сменами' in await note.inner_text()
                await page.screenshot(path=str(evidence / f'legacy-hours-done-{width}.png'), full_page=True)
                assert not errors, errors
                await context.close()
            await browser.close()
        print('PASS: old-hours banner, conflicts named, one tap moves future shifts to 09:00 until the last sale; desktop/320px; no page errors')
    finally:
        await runner.cleanup()
        await fixture.asyncTearDown()


if __name__ == '__main__':
    asyncio.run(main())

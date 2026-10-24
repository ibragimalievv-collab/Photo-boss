"""Real application/importer, authenticated API and DB on desktop and 320px."""
import asyncio
import json
import os
import ssl
import subprocess
import tempfile
from datetime import date
from pathlib import Path

import test_miniapp_release as fixtures
from aiohttp import web
from playwright.async_api import async_playwright, expect
from sqlalchemy import text

SOURCE = (Path(__file__).parent / 'fixtures/lisa_schedule_20261022.txt').read_text()


async def main():
    fixture = fixtures.MiniAppTests()
    await fixture.asyncSetUp()
    fixture.service.today = lambda: date(2026, 10, 24)
    app = web.Application()
    fixture.service.register(app)
    runner = web.AppRunner(app)
    await runner.setup()
    # Exercise the production HTTPS-only transport without changing its guard.
    with tempfile.TemporaryDirectory(prefix='schedule-tls-') as directory:
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
            executable = os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
            browser = await pw.chromium.launch(headless=True, executable_path=executable, args=['--no-sandbox'])
            for width in (1366, 320):
                context = await browser.new_context(ignore_https_errors=True, viewport={'width': width, 'height': 900}, locale='ru-RU', service_workers='block')
                await context.add_init_script('window.Telegram={WebApp:{initData:'+json.dumps(fixtures.signed())+',ready(){},expand(){},onEvent(){},setHeaderColor(){},setBackgroundColor(){},BackButton:{onClick(){},hide(){},show(){}}}};')
                for path in ('/shift/attendance.js','/people/people.js','/work-chat/chat.js','https://telegram.org/js/telegram-web-app.js'):
                    await context.route('**'+path if path.startswith('/') else path,
                        lambda route: route.fulfill(status=200, body='', content_type='text/javascript'))
                page = await context.new_page()
                errors = []
                page.on('pageerror', lambda error, errors=errors: errors.append(str(error)))
                await page.goto(origin+'/app/#schedule')
                try:
                    await page.locator('[data-action=new-shift]').click()
                except Exception:
                    await page.screenshot(path=str(evidence / f'boot-failure-{width}.png'), full_page=True)
                    print('BOOT FAILURE', await page.locator('body').inner_text(), 'JS ERRORS', errors)
                    raise
                await page.locator('.schedule-text-panel > summary').click()
                form = page.locator('#scheduleTextForm')
                await form.locator('[name=employee]').select_option('3')
                await form.locator('[name=scheduleText]').fill(SOURCE)
                await form.locator('[type=submit]').click()
                await page.wait_for_function('document.querySelector("#scheduleTextForm")._preview?.days?.length === 31')
                # First pass must ask for missing settings; later runs reuse confirmed data.
                if not await form.evaluate('f=>f._preview?.canApply'):
                    assert 'Укажите один раз' in await form.locator('[data-import-message]').inner_text()
                await form.locator('[data-import-defaults]').evaluate('e=>e.open=true')
                await form.locator('[name=hotelId]').select_option('1')
                await form.locator('[name=start]').fill('09:00')
                await form.locator('[name=end]').fill('19:00')
                await form.locator('[type=submit]').click()
                apply = form.locator('[data-import-apply]')
                await apply.wait_for(state='visible')
                assert await form.locator('[data-import-preview] > .panel').count() == 31
                assert 'Всего 31 дней · Менеджер 15 · Фотограф 11 · Выходные 5' in await form.inner_text()
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert await page.locator('#sheetContent').evaluate('e=>e.scrollWidth<=e.clientWidth')
                # Editing any parameter invalidates preview, then forces one recheck.
                await form.locator('[name=end]').fill('18:00')
                assert not await apply.is_visible()
                await form.locator('[name=end]').fill('19:00')
                await form.locator('[type=submit]').click()
                await apply.wait_for(state='visible')
                await page.screenshot(path=str(evidence / f'preview-{width}.png'), full_page=True)
                lost = [False]
                async def lose_first_reply(route, _request, lost=lost):
                    if not lost[0]:
                        lost[0] = True
                        response = await route.fetch()
                        assert response.status == 200, await response.text()
                        await route.abort('failed')
                    else:
                        await route.continue_()
                await context.route('**/schedule/text/apply', lose_first_reply)
                await apply.click()
                await expect(form.locator("[type=submit]")).to_be_enabled()
                # No success after a lost response; retry uses the same request key.
                assert 'Успешное сохранение пока не подтверждено' in await form.locator('[data-import-message]').inner_text()
                with fixture.engine.inner.connect() as conn:
                    count = conn.execute(text('SELECT COUNT(*) FROM shifts')).scalar_one()
                await apply.dblclick()
                await expect(form.locator("[data-import-message]")).to_contain_text("повторно проверен")
                with fixture.engine.inner.connect() as conn:
                    assert conn.execute(text('SELECT COUNT(*) FROM shifts')).scalar_one() == count
                    assert conn.execute(text("SELECT COUNT(*) FROM settings WHERE key LIKE 'schedule.day:%'")).scalar_one() == 31
                await page.screenshot(path=str(evidence / f'verified-{width}.png'), full_page=True)
                await page.locator('#closeSheet').click()
                await page.locator('[data-day="2026-10-28"]').click()
                assert 'Выходной по графику' in await page.locator('#app').inner_text()
                assert not errors, errors
                await context.close()
            await browser.close()
        print('PASS: real desktop/320px import UI, 31-day preview, stale preview, lost response, retry, double click, persisted roles/off days; no page errors')
    finally:
        await runner.cleanup()
        await fixture.asyncTearDown()


if __name__ == '__main__':
    asyncio.run(main())
